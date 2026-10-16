
__author__ = 'katharine'

from gevent import monkey; monkey.patch_all()

import datetime
import json as _json
import logging
import uuid as uuid_mod
from urllib.parse import urlparse, unquote

import requests
import requests.exceptions

from .safe_requests import NonlocalHTTPAdapter
from .qjs import from_js
from ..version import __version__

logger = logging.getLogger('pypkjs.javascript.xhr')

_TIMELINE_API_HOSTS = {'timeline-api.rebble.io', 'timeline-api.getpebble.com'}

# The reactive XMLHttpRequest object lives entirely in JS -- QuickJS can't expose a
# live Python object with settable properties/handlers, so the object (its state,
# on* handlers, addEventListener listeners) is JS and only the network side-effect
# is delegated to the Python worker below via __xhr.perform(spec, done).
_XHR_JS = """
(function() {
    function XMLHttpRequest() {
        this.readyState = 0;
        this.response = null;
        this.responseText = null;
        this.responseType = "";
        this.status = null;
        this.statusText = null;
        this.timeout = 0;
        this._method = null;
        this._url = null;
        this._headers = {};
        this._respHeaders = {};
        this._listeners = {};
        this.onreadystatechange = null;
        this.ontimeout = null;
        this.onload = null;
        this.onloadstart = null;
        this.onloadend = null;
        this.onprogress = null;
        this.onerror = null;
        this.onabort = null;
    }
    XMLHttpRequest.UNSENT = 0;
    XMLHttpRequest.OPENED = 1;
    XMLHttpRequest.HEADERS_RECEIVED = 2;
    XMLHttpRequest.LOADING = 3;
    XMLHttpRequest.DONE = 4;

    XMLHttpRequest.prototype._fire = function(type, ev) {
        ev = ev || { type: type };
        var handler = this['on' + type];
        if (handler) { handler.call(this, ev); }
        var ls = this._listeners[type] || [];
        for (var i = 0; i < ls.length; i++) { ls[i].call(this, ev); }
    };
    XMLHttpRequest.prototype.open = function(method, url, async, user, password) {
        this._method = method;
        this._url = url;
        this._async = (async === undefined) ? true : !!async;
        this._user = (user === undefined) ? null : user;
        this._password = (password === undefined) ? null : password;
        this.readyState = 1;
        this._fire('readystatechange');
    };
    XMLHttpRequest.prototype.setRequestHeader = function(header, value) {
        this._headers[header] = value;
    };
    XMLHttpRequest.prototype.overrideMimeType = function(mime) { this._mime = mime; };
    XMLHttpRequest.prototype.getResponseHeader = function(h) {
        h = String(h).toLowerCase();
        for (var k in this._respHeaders) {
            if (k.toLowerCase() === h) { return this._respHeaders[k]; }
        }
        return null;
    };
    XMLHttpRequest.prototype.getAllResponseHeaders = function() {
        var out = [];
        for (var k in this._respHeaders) { out.push(k + '\\x3a\\x20' + this._respHeaders[k]); }
        return out.length ? out.join('\\x0d\\x0a') : null;
    };
    XMLHttpRequest.prototype.addEventListener = function(type, fn) {
        (this._listeners[type] = this._listeners[type] || []).push(fn);
    };
    XMLHttpRequest.prototype.removeEventListener = function(type, fn) {
        var ls = this._listeners[type];
        if (!ls) return;
        this._listeners[type] = ls.filter(function(f) { return f !== fn; });
    };
    function applyResult(self, res) {
        if (res.error) {
            self.status = res.status || 0;
            self.statusText = res.statusText || "";
            self.readyState = 4;
            self._fire(res.timeout ? 'timeout' : 'error');
            self._fire('loadend');
            self._fire('readystatechange');
            return;
        }
        self.status = res.status;
        self.statusText = res.statusText;
        self.responseText = res.responseText;
        self._respHeaders = res.headers || {};
        if (self.responseType === 'json') { self.response = res.response; }
        else if (self.responseType === 'arraybuffer') { self.response = (new Uint8Array(res.responseBytes || [])).buffer; }
        else { self.response = res.responseText; }
        self.readyState = 4;
        self._fire('load');
        self._fire('loadend');
        self._fire('readystatechange');
    }

    XMLHttpRequest.prototype.send = function(body) {
        var self = this;
        var bodyBytes = null;
        if (body instanceof ArrayBuffer) {
            bodyBytes = Array.prototype.slice.call(new Uint8Array(body));
            body = null;
        } else if (ArrayBuffer.isView(body)) {
            bodyBytes = Array.prototype.slice.call(new Uint8Array(body.buffer, body.byteOffset, body.byteLength));
            body = null;
        } else if (body !== undefined && body !== null && typeof body !== 'string') {
            body = String(body);  // per spec: ToString, e.g. '[object Object]'
        }
        var spec = {
            method: this._method, url: this._url, headers: this._headers,
            body: (body === undefined ? null : body), bodyBytes: bodyBytes,
            user: this._user, password: this._password,
            timeout: this.timeout || 0, responseType: this.responseType || ""
        };
        this._aborted = false;
        this._fire('loadstart');
        if (this._async === false) {
            // Sync XHR: the Python side blocks and RETURNS the result (no
            // re-entrant callback), so events have fired before send() returns.
            applyResult(self, __xhr.performSync(spec));
            return;
        }
        this._req = __xhr.perform(spec, function(res) {
            if (self._aborted) { return; }
            applyResult(self, res);
        });
    };
    XMLHttpRequest.prototype.abort = function() {
        this._aborted = true;
        if (this._req) { __xhr.abort(this._req); this._req = null; }
        if (this.readyState !== 0 && this.readyState !== 4) {
            this.readyState = 4;
            this._fire('readystatechange');
            this._fire('abort');
            this._fire('loadend');
            this.readyState = 0;
        }
    };

    window.XMLHttpRequest = XMLHttpRequest;
})();
"""


class XHRWorker(object):
    def __init__(self, runtime, session):
        self.runtime = runtime
        self.session = session
        self._live = {}   # request id -> greenlet, for abort()
        self._counter = 0

    def perform(self, spec, done):
        # spec is a JS object; done is a JS callback. Do the network off the JS
        # thread, then hand the result back for the JS object to apply. Returns a
        # request id that abort() accepts.
        spec = from_js(spec)
        self._counter += 1
        rid = self._counter
        self._live[rid] = self.runtime.group.spawn(self._run, rid, spec, done)
        return rid

    def performSync(self, spec):
        # Sync XHR (open(..., false)): block this greenlet on the network and
        # return the result directly -- gevent keeps the rest of the world alive.
        return self._do(from_js(spec))

    def abort(self, rid):
        g = self._live.pop(rid, None)
        if g is not None:
            g.kill(block=False)

    def _run(self, rid, spec, done):
        # A result that races abort() is dropped by the JS-side _aborted guard.
        try:
            self.runtime.enqueue(done, self._do(spec))
        finally:
            self._live.pop(rid, None)

    def _do(self, spec):
        method = (spec.get('method') or 'GET')
        url = spec.get('url')
        headers = spec.get('headers') or {}
        body = spec.get('body')
        if spec.get('bodyBytes') is not None:
            body = bytes(bytearray(spec['bodyBytes']))
        user = spec.get('user')
        auth = (user, spec.get('password') or '') if user else None
        timeout = spec.get('timeout') or 0
        response_type = spec.get('responseType') or ""

        if self._is_timeline_api_request(method, url):
            return self._handle_timeline_api_request(method, url, body)

        try:
            req = requests.Request(method, url, headers=headers, auth=auth,
                                   data=(body if body is not None else None))
            prepared = self.session.prepare_request(req)
            resp = self.session.send(prepared, timeout=(timeout / 1000.0 if timeout else None), verify=True)
            result = {
                'status': resp.status_code,
                'statusText': resp.reason,
                'responseText': resp.text,
                'headers': dict(resp.headers),
            }
            if response_type == 'json':
                try:
                    result['response'] = resp.json()
                except ValueError:
                    result['response'] = None
            elif response_type == 'arraybuffer':
                result['responseBytes'] = list(resp.content)
        except requests.exceptions.Timeout:
            result = {'error': True, 'timeout': True, 'status': 0, 'statusText': 'timeout'}
        except Exception as e:
            # Broad on purpose: an uncaught exception here would kill the worker
            # greenlet and the request would hang at readyState 1 forever.
            result = {'error': True, 'status': 0, 'statusText': str(e)}
        return result

    def _is_timeline_api_request(self, method, url):
        if not url:
            return False
        parsed = urlparse(url)
        return (parsed.hostname in _TIMELINE_API_HOSTS
                and parsed.path is not None
                and parsed.path.lower().startswith('/v1/user/pins'))

    def _handle_timeline_api_request(self, method, url, body):
        """Handle a timeline pin API request locally via BlobDB (the servers are gone)."""
        tl_logger = logging.getLogger('pypkjs.javascript.xhr.timeline')
        method = method.upper()
        parsed = urlparse(url)
        path = parsed.path
        timeline = self.runtime.runner.timeline
        app_uuid = str(self.runtime.runner.running_uuid)

        try:
            if method in ('PUT', 'POST'):
                if body is None:
                    raise Exception("No body provided for timeline pin insert")
                pin = _json.loads(body)  # str, or bytes from an ArrayBuffer body
                path_after_pins = path.split('/v1/user/pins/')[-1].strip('/')
                if path_after_pins:
                    pin_id = unquote(path_after_pins)
                elif 'id' in pin:
                    pin_id = str(pin['id'])
                else:
                    raise Exception("No pin id found in URL or body")

                guid = uuid_mod.uuid5(uuid_mod.NAMESPACE_DNS, '%s.pin.developer.getpebble.com' % pin_id)
                now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                pin['guid'] = str(guid)
                pin['createTime'] = now
                pin['updateTime'] = now
                pin['topicKeys'] = pin.get('topicKeys', [])
                pin['source'] = 'sdk'
                pin['dataSource'] = 'sandbox-uuid:%s' % app_uuid
                tl_logger.info("Inserting local timeline pin '%s' (guid=%s) for app %s", pin_id, guid, app_uuid)
                timeline.handle_pin_create(pin, manual=True)
            elif method == 'DELETE':
                path_after_pins = path.split('/v1/user/pins/')[-1].strip('/')
                if not path_after_pins:
                    raise Exception("No pin id in DELETE URL")
                pin_id = unquote(path_after_pins)
                guid = uuid_mod.uuid5(uuid_mod.NAMESPACE_DNS, '%s.pin.developer.getpebble.com' % pin_id)
                tl_logger.info("Deleting local timeline pin '%s' (guid=%s)", pin_id, guid)
                try:
                    timeline.handle_pin_delete({'guid': str(guid)})
                except Exception:
                    pass
            else:
                raise Exception("Unsupported method %s for timeline API" % method)
        except Exception as e:
            return {'error': True, 'status': 500, 'statusText': str(e)}

        return {'status': 200, 'statusText': 'OK', 'responseText': '{}',
                'response': {} , 'headers': {}}


class XHRExtension:
    def __init__(self, runtime):
        session = requests.Session()
        # Some APIs (Yahoo Finance, for one) refuse python-requests' default User-Agent.
        session.headers['User-Agent'] = 'pypkjs/%s' % __version__
        if runtime.block_private_addresses:
            adapter = NonlocalHTTPAdapter()
            session.mount('http://', adapter)
            session.mount('https://', adapter)
        runtime.context.expose("__xhr", XHRWorker(runtime, session), ['perform', 'performSync', 'abort'])
        runtime.run_js(_XHR_JS)


def prepare_xhr(runtime):
    # XHR is fully set up in XHRExtension.__init__; nothing deferred.
    pass
