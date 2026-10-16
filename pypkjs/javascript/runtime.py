
__author__ = 'katharine'

import gevent
import gevent.pool
import gevent.queue
import gevent.hub
import logging

import quickjs

from . import PebbleKitJS
from .exceptions import JSRuntimeException
from .qjs import JSContext, JSError
from ..plugins import network

logger = logging.getLogger('pypkjs.javascript.pebble')


class JSRuntime(object):
    def __init__(self, qemu, pbw, runner, persist_dir=None, block_private_addresses=False):
        self.group = gevent.pool.Group()
        self.queue = gevent.queue.Queue()
        self.qemu = qemu
        self.pbw = pbw
        self.runner = runner
        self.runtime_id = JSRuntime.runtimeCount
        self.persist_dir = persist_dir
        self.block_private_addresses = block_private_addresses
        self._network_warnings = set()
        JSRuntime.runtimeCount += 1

    def setup(self):
        self.context = JSContext()
        # Minimal browser-ish global aliasing.
        self.context.eval("this.toString = function() { return '[object Window]'; }")
        self.context.eval("var window = this; var globalThis = this;")
        # A base Event constructor + a builder the Python side uses to mint event
        # objects that still carry their methods (to_js can't emit functions, so we
        # construct the object in JS and copy the data properties onto it).
        self.context.eval("""
            var Event = function(type, init) {
                var self = this;
                init = init || {};
                this.type = type;
                this.bubbles = !!init.bubbles;
                this.cancelable = !!init.cancelable;
                this.defaultPrevented = false;
                this.target = null;
                this.currentTarget = null;
                this.eventPhase = 2;
                this._aborted = false;
                this.stopPropagation = function() {};
                this.stopImmediatePropagation = function() { self._aborted = true; };
                this.preventDefault = function() { self.defaultPrevented = true; };
                this.initEvent = function(type, bubbles, cancelable) {
                    self.type = type;
                    self.bubbles = !!bubbles;
                    self.cancelable = !!cancelable;
                };
            };
            Event.NONE = 0;
            Event.CAPTURING_PHASE = 1;
            Event.AT_TARGET = 2;
            Event.BUBBLING_PHASE = 3;
            var _build_event = function(type, data) {
                var e = new Event(type);
                if (data) { for (var k in data) { e[k] = data[k]; } }
                return e;
            };
            var _event_aborted = function(e) { return !!(e && e._aborted); };
        """)
        self._build_event = self.context.get("_build_event")
        self._event_aborted = self.context.get("_event_aborted")
        self.context.eval("var __js_identity = function(a, b) { return a === b; };")
        self._js_identity = self.context.get("__js_identity")
        self.install_apis()

    def install_apis(self):
        self.pjs = PebbleKitJS(self, self.qemu, persist=self.persist_dir)
        self.pjs.do_post_setup()

    def build_event(self, event_type, data=None):
        """Create a JS Event object (carrying its methods) with optional data props.
        Like every other JS call, this must only run on the event loop greenlet."""
        return self._build_event(event_type, self.context.to_js(data) if data else None)

    def event_aborted(self, event):
        """Whether stopImmediatePropagation() was called on a JS event."""
        return bool(self._event_aborted(event))

    def run_js(self, src):
        self.context.eval(src)

    def run(self, src):
        self.setup()
        logger.info("JS starting")
        try:
            self.context.eval(src)
        except (JSError, JSRuntimeException) as e:
            self.log_output(getattr(e, 'stackTrace', str(e)))
            self.log_output("JS failed.")
        except Exception as e:
            self.log_output(str(e))
            raise
        else:
            self.enqueue(self.pjs.pebble._connect)
            self.event_loop()
        finally:
            self.pjs.shutdown()
            self.group.kill(timeout=2)
            logger.info("JS finished")

    def stop(self):
        self.queue.put(StopIteration)

    def enqueue(self, fn, *args, **kwargs):
        self.queue.put((fn, args, kwargs))

    def js_same(self, a, b):
        """JS === identity. Every boundary crossing mints a fresh quickjs.Object
        wrapper for the same JS function, so Python ==/is can never match two
        crossings of one function."""
        if isinstance(a, quickjs.Object) and isinstance(b, quickjs.Object):
            return bool(self._js_identity(a, b))
        return a == b

    def call_js(self, fn, *args):
        """Invoke fn with args, converting Python containers to JS values when fn is
        a JS callback (a plain Python callable takes the args as-is). A JS throw is
        translated to JSError so every caller's except clause catches it."""
        if isinstance(fn, quickjs.Object):
            args = [self.context.to_js(a) if isinstance(a, (dict, list)) else a for a in args]
        try:
            result = fn(*args)
            # A callback may have scheduled Promise microtasks; drain them.
            self.context.pump()
        except quickjs.JSException as e:
            raise JSError(str(e))
        return result

    def event_loop(self):
        try:
            for fn, args, kwargs in self.queue:
                try:
                    self.call_js(fn, *args) if not kwargs else fn(*args, **kwargs)
                except (JSError, JSRuntimeException, quickjs.JSException) as e:
                    self.log_output("Error running asynchronous JavaScript:")
                    self.log_output(getattr(e, 'stackTrace', str(e)))
        except gevent.hub.LoopExit:
            logger.warning("Runtime ran out of events; terminating.")

    def log_output(self, message):
        raise NotImplemented

    def warn(self, message):
        self.log_output('WARN: %s' % message)

    def appinfo(self):
        return self.pbw.manifest

    def check_network(self, url):
        """Warn, once for each reason, about a request the phone app would refuse."""
        reason = network.refusal(url, self.appinfo())
        if reason is not None and reason not in self._network_warnings:
            self._network_warnings.add(reason)
            self.warn(reason)

    def ext_name(self, name):
        return "instance/%d/runtime/%s" % (self.runtime_id, name)

    def is_configurable(self):
        return 'configurable' in self.pbw.manifest['capabilities']

    def do_config(self):
        self.enqueue(self.pjs.pebble._configure)

JSRuntime.runtimeCount = 0
