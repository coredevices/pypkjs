"""A third-party plugin: a pbw's plugin script, run the way the phone app runs it (JsPlugin.kt).

Every request cold-starts a session (a fresh JS context holding the host shims and then the
plugin's script), dispatches one request into it and tears it down.
"""

import json
import logging
import os
import os.path

import gevent
import gevent.event
import gevent.lock

from . import TIMEOUT, UNKNOWN, action_error
from . import host_js
from . import hosted_oauth
from .base import Plugin
from ..javascript.events import EventExtension
from ..javascript.localstorage import LocalStorage
from ..javascript.navigator import Navigator
from ..javascript.performance import Performance
from ..javascript.runtime import JSRuntime
from ..javascript.timers import Timers
from ..javascript.ws import WSExtension
from ..javascript.xhr import XHRExtension

logger = logging.getLogger('pypkjs.plugins')

REQUEST_TIMEOUT = 20
START_TIMEOUT = 10
# A config page's request can wait on the user (an interactive sign-in, say).
CONFIG_REQUEST_TIMEOUT = 300


class JsPlugin(Plugin):
    def __init__(self, uuid, name, manifest, script, runner):
        super(JsPlugin, self).__init__(uuid, name, manifest['description'], manifest['sources'], manifest['actions'])
        self.manifest = manifest
        self.script = script
        self.runner = runner
        self._lock = gevent.lock.Semaphore()
        self._secure_storage = SecureStorage(runner.persist_dir, uuid)
        # Shared by its sessions (one per request), so each refusal is warned about once.
        self.network_warnings = set()
        self._config_lock = gevent.lock.Semaphore()
        self._config_session = None
        self._config_greenlet = None
        self._config_push = None

    def read(self, category, item, properties, icon_pixel_size):
        request = {'kind': 'source', 'category': category, 'item': item}
        if properties is not None:
            request['properties'] = properties
        if icon_pixel_size is not None:
            request['iconPixelSize'] = icon_pixel_size
        return self._parse_envelope(self.request(request))

    def invoke(self, action, args):
        raw = self.request({'kind': 'action', 'action': action, 'args': args})
        if raw is None:
            return action_error(TIMEOUT, 'plugin did not respond')
        try:
            result = json.loads(raw)
            ok = bool(result['ok'])
        except (ValueError, TypeError, KeyError):
            self.log('warn', 'bad action response: %s' % raw)
            return action_error(UNKNOWN, 'malformed plugin response')
        if ok:
            self.refresh(result.get('refreshed') or [])
        return {k: result.get(k) for k in ('ok', 'text', 'refreshed', 'code', 'message')}

    def request(self, request):
        """Cold-start a session, dispatch one request, tear the session down. None on failure."""
        with self._lock:
            if self.retired:
                return None
            session = PluginSession(self)
            greenlet = gevent.spawn(session.run)
            try:
                if not session.started.wait(START_TIMEOUT):
                    return None
                response = session.dispatch(json.dumps(request), REQUEST_TIMEOUT)
                if response is None:
                    self.log('warn', '%s request got no response in %ds' % (request['kind'], REQUEST_TIMEOUT))
                return response
            finally:
                session.stop()
                greenlet.join(timeout=2)
                greenlet.kill(block=False)

    # ------------------------------------------------------------------ config page
    #
    # While its config page is open a plugin keeps one session running, so the page talks to
    # the same script throughout.

    def open_config_session(self, push):
        """Start the config session; push(json) gets the plugin's Pebble.sendConfigMessage calls."""
        with self._config_lock:
            self._config_push = push
            self._close_config_session_locked()
            self._open_config_session_locked()

    def close_config_session(self):
        with self._config_lock:
            self._config_push = None
            self._close_config_session_locked()

    def config_message(self, message_json):
        """The plugin's reply (JSON) to a message from its config page; None if it gives none."""
        with self._config_lock:
            session = self._open_config_session_locked()
        if session is None:
            return None
        return session.dispatch_config(message_json, CONFIG_REQUEST_TIMEOUT)

    def push_config_message(self, message_json):
        if self._config_push is None:
            self.log('warn', 'sendConfigMessage: no config page is open')
        else:
            self._config_push(message_json)

    def retire(self):
        super(JsPlugin, self).retire()
        self.close_config_session()

    def _open_config_session_locked(self):
        if self.retired:
            return None
        if self._config_session is not None:
            return self._config_session
        session = PluginSession(self)
        greenlet = gevent.spawn(session.run)
        if not session.started.wait(START_TIMEOUT):
            session.stop()
            greenlet.join(timeout=2)
            greenlet.kill(block=False)
            return None
        self._config_session, self._config_greenlet = session, greenlet
        return session

    def _close_config_session_locked(self):
        session, greenlet = self._config_session, self._config_greenlet
        self._config_session = self._config_greenlet = None
        if session is not None:
            session.stop()
            greenlet.join(timeout=2)
            greenlet.kill(block=False)

    def log(self, level, message):
        prefix = '' if level == 'info' else '%s: ' % level.upper()
        self.runner.log_output('[%s] %s%s' % (self.name, prefix, message))

    def _parse_envelope(self, raw):
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            self.log('warn', 'bad source response: %s' % raw)
            return None
        if not isinstance(parsed, dict):
            self.log('warn', 'bad source response: %s' % raw)
            return None
        if parsed.get('error') is not None:
            self.log('warn', 'source error: %s' % parsed['error'])
            return None
        envelope = {'pluginUuid': str(self.uuid), 'instances': parsed.get('instances') or []}
        if parsed.get('validUntilMs') is not None:
            envelope['validUntilMs'] = parsed['validUntilMs']
        return envelope


class PluginSession(JSRuntime):
    """One run of a plugin's script. All JS runs on the greenlet running run()."""

    def __init__(self, plugin):
        runner = plugin.runner
        # LocalStorage keys its store off pbw.uuid: a plugin shares its pbw's store.
        super(PluginSession, self).__init__(None, plugin, runner, persist_dir=runner.persist_dir,
                                            block_private_addresses=runner.block_private_addresses)
        self.plugin = plugin
        self._network_warnings = plugin.network_warnings
        self.started = gevent.event.AsyncResult()
        self._pending = {}
        self._next_id = 0
        self.local_storage = None

    def install_apis(self):
        self.local_storage = LocalStorage(self, self.persist_dir)
        for extension in (Performance, Navigator, Timers, EventExtension, XHRExtension, WSExtension):
            extension(self)
        self.context.expose('_JsBridge', self, ['respond', 'log', 'configMessage', 'refreshSources'])
        self.context.expose(host_js.SECURE_STORAGE_BRIDGE, self.plugin._secure_storage.bridge(self), ['request'])
        self.run_js('var Pebble = {};')
        self.run_js(host_js.BASE64_JS)
        self.run_js(host_js.FETCH_JS)
        self.run_js(host_js.PLUGIN_HOST_JS)
        if self.plugin.manifest['oauth']:
            self.context.expose(host_js.OAUTH_BRIDGE, OAuthBridge(self), ['request'])
            self.run_js(host_js.OAUTH_JS)
        self.run_js(host_js.SECURE_STORAGE_JS)

    def run(self, src=None):
        try:
            self.setup()
            self.context.eval(self.plugin.script)
        except Exception as e:
            self.log('error', 'failed to start: %s' % getattr(e, 'stackTrace', e))
            self.started.set(False)
        else:
            self.started.set(True)
            self.event_loop()
        finally:
            if self.local_storage is not None:
                self.local_storage._shutdown()
            self.group.kill(timeout=2)

    def dispatch(self, request_json, timeout):
        return self._call('__pluginDispatch', request_json, timeout)

    def dispatch_config(self, message_json, timeout):
        return self._call('__pluginConfigMessage', message_json, timeout)

    def _call(self, function, json_argument, timeout):
        """Call a host-shim function with a request id and a JSON argument; wait for its respond()."""
        self._next_id += 1
        request_id = self._next_id
        result = self._pending[request_id] = gevent.event.AsyncResult()
        self.enqueue(self.context.eval, 'globalThis.%s(%d, %s)' % (function, request_id, json_argument))
        try:
            return result.wait(timeout)
        finally:
            self._pending.pop(request_id, None)

    def log_output(self, message):
        self.plugin.log('info', message)

    def warn(self, message):
        self.plugin.log('warn', message)

    def appinfo(self):
        pbw = next((p for u, p in self.runner.pbws.items() if str(u) == str(self.plugin.uuid)), None)
        return pbw.manifest if pbw is not None else {}

    # _JsBridge

    def respond(self, request_id, payload):
        pending = self._pending.get(int(request_id))
        if pending is not None:
            pending.set(payload)

    def log(self, level, message):
        self.plugin.log(level, message)

    def configMessage(self, message):
        self.plugin.push_config_message(message)

    def refreshSources(self, keys_json):
        try:
            keys = json.loads(keys_json)
        except ValueError:
            return
        self.plugin.refresh([str(k) for k in keys])


class OAuthBridge(object):
    """`Pebble.oauth` requests: each runs in its own greenlet (a sign-in waits on the browser), in the
    session's group so tearing the session down cancels it and closes its loopback listener."""

    def __init__(self, session):
        self.session = session

    def request(self, request_id, request_json):
        self.session.group.spawn(self._run, int(request_id), request_json)

    def _run(self, request_id, request_json):
        plugin = self.session.plugin
        try:
            request = json.loads(request_json)
            slug = request.get('slug')
            if slug not in plugin.manifest['oauth']:
                raise hosted_oauth.OAuthError("'%s' isn't an OAuth connector this plugin declares" % slug,
                                              'INVALID_ARGS')
            if request.get('operation') == 'authorize':
                result = hosted_oauth.authorize(plugin.uuid, slug, log=lambda m: plugin.log('info', m))
            elif request.get('operation') == 'refresh':
                result = hosted_oauth.refresh(request.get('brokerRefresh'))
            else:
                raise hosted_oauth.OAuthError('unknown OAuth operation', 'INVALID_ARGS')
            payload = {'result': result}
        except hosted_oauth.OAuthError as e:
            plugin.log('warn', 'OAuth: %s' % e)
            payload = {'error': {'code': e.code, 'message': str(e)}}
        except Exception as e:
            logger.exception('OAuth request failed')
            payload = {'error': {'code': 'AUTH_REQUIRED', 'message': str(e)}}
        self.session.enqueue(self.session.context.eval, 'globalThis.%s(%d, %s)'
                             % (host_js.OAUTH_REPLY, request_id, json.dumps(payload)))


class SecureStorage(object):
    """`Pebble.secureStorage`: a plugin's private key/value store (plain JSON on disk here)."""

    def __init__(self, persist_dir, uuid):
        self._path = None
        self._values = {}
        if persist_dir is not None:
            directory = os.path.join(persist_dir, 'plugin-secure-storage')
            os.makedirs(directory, exist_ok=True)
            self._path = os.path.join(directory, '%s.json' % uuid)
            try:
                with open(self._path) as f:
                    self._values = json.load(f)
            except (IOError, ValueError):
                pass

    def bridge(self, session):
        storage = self

        class Bridge(object):
            def request(self, request_id, request_json):
                payload = storage._handle(request_json)
                session.enqueue(session.context.eval, 'globalThis.%s(%d, %s)'
                                % (host_js.SECURE_STORAGE_REPLY, int(request_id), json.dumps(payload)))

        return Bridge()

    def _handle(self, request_json):
        try:
            request = json.loads(request_json)
            operation, key = request.get('operation'), request.get('key')
            if not key:
                raise ValueError('key is required')
            if operation == 'getItem':
                return {'result': self._values.get(key)}
            if operation == 'setItem':
                if not isinstance(request.get('value'), str):
                    raise ValueError('value is required')
                self._values[key] = request['value']
            elif operation == 'removeItem':
                self._values.pop(key, None)
            else:
                raise ValueError("unknown secure storage operation '%s'" % operation)
            self._save()
            return {'result': None}
        except ValueError as e:
            return {'error': {'code': 'INVALID_ARGS', 'message': str(e)}}

    def _save(self):
        if self._path is not None:
            with open(self._path, 'w') as f:
                json.dump(self._values, f)
