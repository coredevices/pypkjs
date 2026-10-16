"""`Pebble.subscribeToSource` / `invokeAction` / `enumeratePlugins` for PebbleKit JS apps,
dispatched to the runner's plugin registry (the app's SourceDispatcher / ActionDispatcher).

The emulator grants every permission a source or action asks of its caller.
"""

import json
import logging

import gevent
import gevent.event

from .. import plugins
from ..plugins import manifest as manifests

logger = logging.getLogger('pypkjs.javascript.plugins')

DEFAULT_ACTION_TIMEOUT_MS = 15000
MAX_ACTION_TIMEOUT_MS = 60000
CONFIG_MESSAGE_TIMEOUT = 30

# The plugin half of the phone app's startup.js, with its `_Pebble` bridge renamed `__plugins`,
# and its config page messaging.
_PLUGINS_JS = r'''(function (global) {
    const sourceSubscriptions = new Map();
    let sourceSubscriptionIdCounter = 0;
    const actionCalls = new Map();
    let actionCallIdCounter = 0;

    global._signalSourceUpdate = (subscriptionId, payloadJson) => {
        const sub = sourceSubscriptions.get(subscriptionId);
        if (!sub) return;
        try {
            const envelope = JSON.parse(payloadJson);
            if (sub.onData) sub.onData(envelope);
        } catch (e) {
            console.error("Pebble JS Bridge: Error in source onData callback", e);
        }
    }
    global._signalSourceError = (subscriptionId, errorJson) => {
        const sub = sourceSubscriptions.get(subscriptionId);
        if (!sub) return;
        try {
            const err = JSON.parse(errorJson);
            if (sub.onError) sub.onError(err);
        } catch (e) {
            console.error("Pebble JS Bridge: Error in source onError callback", e);
        }
    }
    global._signalActionResult = (callId, resultJson) => {
        const call = actionCalls.get(callId);
        if (!call) return;
        actionCalls.delete(callId);
        try {
            call(JSON.parse(resultJson));
        } catch (e) {
            call({ ok: false, code: "UNKNOWN", message: "malformed host response" });
        }
    }

    const PluginAPI = {
        subscribeToSource: (config) => {
            if (!config || typeof config !== 'object') {
                throw new Error("Pebble.subscribeToSource: config object required");
            }
            if (!config.category || !config.item) {
                throw new Error("Pebble.subscribeToSource: category and item required");
            }
            const subscriptionId = ++sourceSubscriptionIdCounter;
            sourceSubscriptions.set(subscriptionId, {
                onData: config.onData,
                onError: config.onError,
            });
            const request = {
                category: config.category,
                item: config.item,
                properties: config.properties,
                plugin: config.plugin,
                maxStalenessMs: config.maxStalenessMs,
                iconPixelSize: config.iconPixelSize,
            };
            try {
                __plugins.subscribeToSource(subscriptionId, JSON.stringify(request));
            } catch (e) {
                sourceSubscriptions.delete(subscriptionId);
                throw e;
            }
            return {
                unsubscribe: () => {
                    if (!sourceSubscriptions.has(subscriptionId)) return;
                    sourceSubscriptions.delete(subscriptionId);
                    try {
                        __plugins.unsubscribeSource(subscriptionId);
                    } catch (e) {
                        console.error("Pebble JS Bridge: unsubscribe failed", e);
                    }
                },
            };
        },
        enumeratePlugins: () => {
            try {
                return JSON.parse(__plugins.enumeratePlugins());
            } catch (e) {
                console.error("Pebble JS Bridge: enumeratePlugins failed", e);
                return [];
            }
        },
        invokeAction: (config) => {
            if (!config || typeof config !== 'object') {
                throw new Error("Pebble.invokeAction: config object required");
            }
            if (!config.plugin || !config.action) {
                throw new Error("Pebble.invokeAction: plugin and action required");
            }
            const callId = ++actionCallIdCounter;
            const request = {
                plugin: config.plugin,
                action: config.action,
                args: config.args || {},
                timeoutMs: config.timeoutMs,
            };
            return new Promise((resolve) => {
                actionCalls.set(callId, resolve);
                try {
                    __plugins.invokeAction(callId, JSON.stringify(request));
                } catch (e) {
                    actionCalls.delete(callId);
                    resolve({ ok: false, code: "UNKNOWN", message: String(e) });
                }
            });
        },
    };
    global.Pebble.subscribeToSource = PluginAPI.subscribeToSource;
    global.Pebble.enumeratePlugins = PluginAPI.enumeratePlugins;
    global.Pebble.invokeAction = PluginAPI.invokeAction;
    /** Push at this app's config page, while it is open. No reply. */
    global.Pebble.sendConfigMessage = function (message) {
        __plugins.sendConfigMessage(JSON.stringify(message === undefined ? null : message));
    };
    // A message from this app's config page: a 'configmessage' event whose respond() answers it.
    global.__configMessageEvent = function (requestId, data) {
        var event = new Event('configmessage');
        var settled = false;
        event.data = data;
        event.respond = function (reply) {
            if (settled) return;
            settled = true;
            __plugins.configMessageReply(requestId, JSON.stringify(reply === undefined ? null : reply));
        };
        return event;
    };
})(this);
'''


class PluginsExtension(object):
    def __init__(self, runtime):
        self.runtime = runtime
        self.registry = runtime.runner.plugins
        self.subscriptions = {}
        self._config_replies = {}
        self._next_config_id = 0
        runtime.context.expose('__plugins', self,
                               ['subscribeToSource', 'unsubscribeSource', 'invokeAction', 'enumeratePlugins',
                                'sendConfigMessage', 'configMessageReply'])
        runtime.run_js(_PLUGINS_JS)

    @property
    def _app_name(self):
        return self.runtime.pbw.manifest.get('shortName') or str(self.runtime.pbw.uuid)

    def subscribeToSource(self, subscription_id, request_json):
        subscription_id = int(subscription_id)
        if subscription_id in self.subscriptions:
            return
        try:
            request = json.loads(request_json)
            category, item = request['category'], request['item']
        except (ValueError, TypeError, KeyError) as e:
            self._emit_error(subscription_id, plugins.INVALID_REQUEST, str(e) or 'bad json')
            return
        plugin = self.registry.find_source_plugin(category, item, request.get('plugin'))
        if plugin is None:
            self._emit_error(subscription_id, plugins.PLUGIN_UNAVAILABLE, 'No plugin for %s/%s' % (category, item))
            return
        self._log_grants(manifests.caller_permissions(plugin.source(category, item)),
                         '%s/%s from %s' % (category, item, plugin.name))
        self.subscriptions[subscription_id] = self.runtime.group.spawn(
            self._serve, subscription_id, plugin, category, item,
            request.get('properties'), request.get('iconPixelSize'))

    def _serve(self, subscription_id, plugin, category, item, properties, icon_pixel_size):
        def emit(envelope):
            self._signal('_signalSourceUpdate', subscription_id, envelope)
        try:
            plugin.observe(category, item, properties, icon_pixel_size, emit)
        except gevent.GreenletExit:
            raise
        except Exception as e:
            logger.warning("subscription %d failed", subscription_id, exc_info=True)
            self._emit_error(subscription_id, plugins.UNKNOWN, str(e) or type(e).__name__)

    def unsubscribeSource(self, subscription_id):
        greenlet = self.subscriptions.pop(int(subscription_id), None)
        if greenlet is not None:
            greenlet.kill(block=False)

    def invokeAction(self, call_id, request_json):
        call_id = int(call_id)

        def go():
            try:
                request = json.loads(request_json)
                plugin_uuid, action = request['plugin'], request['action']
            except (ValueError, TypeError, KeyError) as e:
                result = plugins.action_error(plugins.INVALID_REQUEST, str(e) or 'bad json')
            else:
                result = self._run_action(plugin_uuid, action, request.get('args') or {}, request.get('timeoutMs'))
                args = request.get('args') or {}
                logger.info("%s invoked %s/%s with %s: %s", self._app_name, plugin_uuid, action, list(args),
                            'ok' if result['ok'] else '%s %s' % (result.get('code'), result.get('message')))
            self._signal('_signalActionResult', call_id, _without_defaults(result))
        self.runtime.group.spawn(go)

    def _run_action(self, plugin_uuid, action, args, timeout_ms):
        plugin = self.registry.find_plugin(plugin_uuid)
        if plugin is None:
            return plugins.action_error(plugins.PLUGIN_UNAVAILABLE, 'No plugin %s' % plugin_uuid)
        declaration = plugin.action(action)
        if declaration is None:
            return plugins.action_error(plugins.PLUGIN_UNAVAILABLE, '%s has no action %s' % (plugin.name, action))
        self._log_grants(manifests.caller_permissions(declaration), '%s/%s' % (plugin.name, action))
        missing = [p for p in manifests.required_params(declaration) if p not in args]
        if missing:
            return plugins.action_error(plugins.INVALID_ARGS, 'missing required: %s' % ', '.join(missing))
        timeout_ms = min(max(int(timeout_ms), 1), MAX_ACTION_TIMEOUT_MS) if timeout_ms else DEFAULT_ACTION_TIMEOUT_MS
        try:
            with gevent.Timeout(timeout_ms / 1000.0):
                return plugin.invoke(action, args)
        except gevent.Timeout:
            return plugins.action_error(plugins.TIMEOUT, 'no response in %dms' % timeout_ms)
        except Exception as e:
            logger.warning("action %s threw", action, exc_info=True)
            return plugins.action_error(plugins.UNKNOWN, str(e) or 'error')

    def enumeratePlugins(self):
        return json.dumps(self.registry.summaries())

    def sendConfigMessage(self, message_json):
        self.runtime.runner.config_pages.push(self.runtime.pbw.uuid, 'pkjs', message_json)

    def config_message(self, message_json):
        """The app's answer (JSON) to a message from its config page; None if it gives none."""
        self._next_config_id += 1
        request_id = self._next_config_id
        reply = self._config_replies[request_id] = gevent.event.AsyncResult()

        def go():
            event = self.runtime.context.eval('globalThis.__configMessageEvent(%d, %s)' % (request_id, message_json))
            if not self.runtime.pjs.pebble.dispatch_event('configmessage', event):
                self.configMessageReply(request_id, json.dumps({'error': 'no configmessage listener registered'}))
        self.runtime.enqueue(go)
        try:
            return reply.wait(CONFIG_MESSAGE_TIMEOUT)
        finally:
            self._config_replies.pop(request_id, None)

    def configMessageReply(self, request_id, reply_json):
        reply = self._config_replies.get(int(request_id))
        if reply is not None and not reply.ready():
            reply.set(reply_json)

    def _log_grants(self, permissions, what):
        if permissions:
            logger.info("Granting %s %s for %s", self._app_name, ', '.join(permissions), what)

    def _emit_error(self, subscription_id, code, message):
        self._signal('_signalSourceError', subscription_id, {'code': code, 'message': message})

    def _signal(self, function, request_id, payload):
        # The payload goes in as a JSON string the shim parses, as the app's dispatchers do it.
        src = 'globalThis.%s(%d, %s)' % (function, request_id, json.dumps(json.dumps(payload)))
        self.runtime.enqueue(self.runtime.context.eval, src)


def _without_defaults(result):
    """As the app serialises an ActionResult: null fields and an empty `refreshed` are left out."""
    return {k: v for k, v in result.items() if v is not None and not (k == 'refreshed' and not v)}
