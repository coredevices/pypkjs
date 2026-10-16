"""Config pages that talk to their pbw's scripts, as the Core phone app hosts them
(coreapp's ConfigPageBridge / ConfigPageSession).

pypkjs serves the page itself, at /config/<uuid>/ on its websocket port, with the app's page-side
`Pebble` shim injected ahead of the page's own scripts. The shim reaches back over a WebSocket at
/config/<uuid>/bridge instead of the app's native bridge; everything else about it is the app's.

While a page is open its bridge connection is a session: the pbw's running PKJS (target "pkjs")
and its plugin (target "plugin", with a config session kept running until the page goes) answer
`Pebble.sendMessage`, and push to the page with `Pebble.sendConfigMessage`.
"""

import json
import logging
import re
import urllib.request

import gevent
import gevent.lock
import requests

from pypkjs.plugins.js_plugin import JsPlugin

logger = logging.getLogger('pypkjs.config_pages')

PKJS_TIMEOUT = 30

# The app's CONFIG_PAGE_SHIM_JS, with post() sending over the bridge WebSocket and the bridge's
# messages driving __pebbleConfig. __BRIDGE_PATH__ is replaced with the page's bridge path.
SHIM_JS = r'''
(function (global) {
  var nextId = 1;
  var pending = {};
  var listeners = { message: [], ready: [] };
  var readyTargets = [];

  var socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host +
                             __BRIDGE_PATH__);
  var queued = [];
  socket.onopen = function () {
    queued.forEach(function (payload) { socket.send(payload); });
    queued = [];
  };
  socket.onmessage = function (event) {
    var m = JSON.parse(event.data);
    if (m.kind === 'reply') global.__pebbleConfig.reply(m.id, m.json);
    else if (m.kind === 'message') global.__pebbleConfig.message(m.target, m.json);
    else if (m.kind === 'ready') global.__pebbleConfig.ready(m.target);
  };

  function post(payload) {
    if (socket.readyState === 1) socket.send(payload);
    else queued.push(payload);
  }

  function call(method, target, message) {
    var id = nextId++;
    return new Promise(function (resolve) {
      pending[id] = resolve;
      post(JSON.stringify({
        id: id,
        method: method,
        target: target === undefined ? '' : target,
        message: message === undefined ? null : message,
      }));
    });
  }

  function emit(type, event) {
    listeners[type].forEach(function (callback) { callback(event); });
  }

  global.Pebble = global.Pebble || {};

  /** Send to one of this pbw's scripts: 'pkjs' or 'plugin'. */
  global.Pebble.sendMessage = function (target, message) {
    if (typeof target !== 'string') {
      return Promise.reject(new Error('sendMessage(target, message): target is required'));
    }
    return call('sendMessage', target, message);
  };

  /** [{ id, name }] — what this page is allowed to talk to. */
  global.Pebble.targets = function () { return call('targets'); };

  global.Pebble.webSignIn = function (options) {
    if (!options || typeof options.url !== 'string' || !Array.isArray(options.cookies) ||
        typeof options.domain !== 'string') {
      return Promise.reject(new Error('webSignIn({ url, cookies, domain })'));
    }
    return call('webSignIn', '', options).then(function (result) {
      if (result && result.error) throw new Error(result.error);
      return result ? result.cookies : null;
    });
  };

  /** Scripts that have already reported ready, for a page that listens late. */
  global.Pebble.readyTargets = function () { return readyTargets.slice(); };

  global.Pebble.addEventListener = function (type, callback) {
    if (!listeners[type] || typeof callback !== 'function') return;
    listeners[type].push(callback);
    if (type === 'ready') {
      readyTargets.forEach(function (target) { callback({ type: 'ready', target: target }); });
    }
  };

  global.Pebble.removeEventListener = function (type, callback) {
    if (!listeners[type]) return;
    var index = listeners[type].indexOf(callback);
    if (index !== -1) listeners[type].splice(index, 1);
  };

  global.__pebbleConfig = {
    reply: function (id, json) {
      var resolve = pending[id];
      if (!resolve) return;
      delete pending[id];
      try { resolve(JSON.parse(json)); } catch (e) { resolve({ error: 'bad reply' }); }
    },
    message: function (target, json) {
      var data = null;
      try { data = JSON.parse(json); } catch (e) { /* leave null */ }
      emit('message', { type: 'message', target: target, data: data });
    },
    ready: function (target) {
      if (readyTargets.indexOf(target) === -1) readyTargets.push(target);
      emit('ready', { type: 'ready', target: target });
    },
  };
  call('hello').then(function (hello) {
    (hello && hello.ready ? hello.ready : []).forEach(function (target) {
      global.__pebbleConfig.ready(target);
    });
  });

})(window);
'''

_HEAD = re.compile(r'<head(\s[^>]*)?>', re.IGNORECASE)
_HTML = re.compile(r'<html(\s[^>]*)?>', re.IGNORECASE)


def inject(html, bridge_path, base_url=None):
    """The page with the shim (and a <base> for a page fetched from elsewhere) ahead of its own
    scripts."""
    tags = ''
    if base_url is not None:
        tags += '<base href="%s">' % base_url.replace('"', '&quot;')
    tags += '<script>%s</script>' % SHIM_JS.replace('__BRIDGE_PATH__', json.dumps(bridge_path))
    for pattern in (_HEAD, _HTML):
        match = pattern.search(html)
        if match:
            return html[:match.end()] + tags + html[match.end():]
    return tags + html


class ConfigPages(object):
    def __init__(self, runner):
        self.runner = runner
        self._sessions = []
        # Pages a running app opened with Pebble.openURL('data:...'), by app uuid.
        self._opened_pages = {}

    @staticmethod
    def path_for(uuid):
        return '/config/%s/' % uuid

    def open_data_page(self, uuid, url):
        """Serve the page in a data: URL (as Pebble.openURL got it) at uuid's path."""
        with urllib.request.urlopen(url) as response:
            self._opened_pages[str(uuid)] = response.read().decode('utf-8', 'replace')

    def has_page(self, uuid):
        return str(uuid) in self._opened_pages or self._config_page(uuid) is not None

    def push(self, uuid, target, message_json):
        """A script's Pebble.sendConfigMessage: to every open page of its pbw, or nowhere."""
        sessions = [s for s in self._sessions if s.uuid == str(uuid)]
        if not sessions:
            logger.info("%s sendConfigMessage: no config page is open", target)
        for session in sessions:
            session.send({'kind': 'message', 'target': target, 'json': message_json})

    def handle(self, environ, start_response):
        """WSGI for everything under /config/."""
        parts = environ['PATH_INFO'].split('/')  # ['', 'config', uuid, rest...]
        uuid = parts[2] if len(parts) > 2 else ''
        rest = '/'.join(parts[3:])
        if rest == 'bridge' and environ.get('wsgi.websocket') is not None:
            ConfigPageSession(self, uuid, environ['wsgi.websocket']).run()
            return []
        if rest == '':
            html = self._html(uuid)
            if html is not None:
                start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8'),
                                          ('Cache-Control', 'no-store')])
                return [html.encode('utf-8')]
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'No config page here.']

    def _config_page(self, uuid):
        pbw = next((p for u, p in self.runner.pbws.items() if str(u) == str(uuid)), None)
        return pbw.manifest.get('configPage') if pbw is not None else None

    def _html(self, uuid):
        bridge = self.path_for(uuid) + 'bridge'
        if uuid in self._opened_pages:
            return inject(self._opened_pages[uuid], bridge)
        page = self._config_page(uuid)
        if page is None:
            return None
        if page.startswith(('http://', 'https://')):
            try:
                response = requests.get(page, timeout=20)
                response.raise_for_status()
            except requests.RequestException as e:
                return '<p>Could not fetch %s: %s</p>' % (page, e)
            return inject(response.text, bridge, base_url=page)
        html = self.runner.config_page_files.get(str(uuid))
        return inject(html, bridge) if html is not None else None


class ConfigPageSession(object):
    """One open page, for as long as its bridge connection lasts."""

    def __init__(self, pages, uuid, ws):
        self.pages = pages
        self.runner = pages.runner
        self.uuid = uuid
        self.ws = ws
        self.title = self.runner.app_title(next((u for u in self.runner.pbws if str(u) == uuid), uuid))
        self.ready_targets = []
        self.scripts = self._scripts()
        self._send_lock = gevent.lock.Semaphore()

    def _scripts(self):
        """What this page may talk to, in the order the app opens them: id -> answerer."""
        scripts = {}
        if self.runner.js is not None and str(self.runner.running_uuid) == self.uuid:
            scripts['pkjs'] = self.runner.js.pjs.plugins
        plugin = self.runner.plugins.find_plugin(self.uuid)
        if isinstance(plugin, JsPlugin):
            scripts['plugin'] = plugin
        return scripts

    def run(self):
        self.pages._sessions.append(self)
        opener = gevent.spawn(self._open)
        calls = []
        try:
            while True:
                raw = self.ws.receive()
                if raw is None:
                    break
                calls.append(gevent.spawn(self._on_call, raw))
        except Exception:
            logger.debug("config page bridge closed", exc_info=True)
        finally:
            self.pages._sessions.remove(self)
            opener.kill(block=False)
            gevent.killall(calls, block=False)
            plugin = self.scripts.get('plugin')
            if plugin is not None:
                plugin.close_config_session()

    def _open(self):
        for target, script in self.scripts.items():
            if target == 'plugin':
                # Ready even if it didn't start: a message retries, and answers with an error.
                script.open_config_session(lambda message: self.send(
                    {'kind': 'message', 'target': 'plugin', 'json': message}))
            self.ready_targets.append(target)
            self.send({'kind': 'ready', 'target': target})

    def _on_call(self, raw):
        try:
            call = json.loads(raw)
            call_id, method = int(call['id']), call['method']
        except (ValueError, TypeError, KeyError):
            logger.warning("malformed call from config page")
            return
        target = call.get('target', '')
        if method == 'hello':
            self._reply(call_id, json.dumps({'ready': list(self.ready_targets)}))
        elif method == 'targets':
            self._reply(call_id, json.dumps([{'id': t, 'name': self.title} for t in self.scripts]))
        elif method == 'sendMessage':
            script = self.scripts.get(target)
            if script is None:
                self._reply(call_id, _error("no script called '%s' (have: %s)" % (target, ', '.join(self.scripts))))
                return
            answer = script.config_message(json.dumps(call.get('message')))
            self._reply(call_id, answer if answer is not None else _error('%s did not answer' % self.title))
        elif method == 'webSignIn':
            self._reply(call_id, _error('web sign-in is not available for this app'))
        else:
            self._reply(call_id, _error("unknown method '%s'" % method))

    def _reply(self, call_id, answer_json):
        self.send({'kind': 'reply', 'id': call_id, 'json': answer_json})

    def send(self, message):
        with self._send_lock:
            try:
                self.ws.send(json.dumps(message))
            except Exception:
                logger.debug("config page went away", exc_info=True)


def _error(message):
    return json.dumps({'error': message})
