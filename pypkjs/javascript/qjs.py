"""QuickJS integration for the PebbleKit JS runtime.

This replaces the old STPyV8/V8 dependency. STPyV8 is archived upstream and ships
no arm64/musl wheels; QuickJS is tiny and builds from source everywhere.

We depend on the ``quickjs-ng`` binding (github.com/genotrance/quickjs-ng), a
maintained wrapper of the actively-developed quickjs-ng engine. It installs as the
top-level ``quickjs`` module (a drop-in for the now-archived PetterS/quickjs
binding) and ships wheels for linux aarch64 + musllinux, macOS arm64, and Windows.

Boundary model (deliberately simple, and idiomatic to the `quickjs` binding):

  * Data crosses as JSON.  A JS value received from the engine is a
    ``quickjs.Object``; ``from_js`` turns it into a plain Python dict/list via its
    ``.json()`` serialisation.  ``JSContext.to_js`` goes the other way via
    ``parse_json``.  Module code therefore deals in ordinary Python containers.
  * Callbacks cross as opaque callables.  A JS function received by Python stays a
    ``quickjs.Object`` that is directly callable; we hold onto it and invoke it
    later (this is how ``addEventListener`` / success-failure callbacks work).

Because callbacks and data always arrive in *known argument positions* (e.g.
``sendAppMessage(message, success, failure)``), a ported method converts the data
positions with ``from_js`` and keeps the callback positions as-is -- no run-time
type sniffing is required (and none is possible: every quickjs value is
``callable``).

Two binding constraints the rest of pypkjs works around:

  * Only the runtime's event loop greenlet may call into the engine. A JS call
    can yield to gevent (sync XHR blocks on the network), and entering QuickJS
    from another greenlet while one is suspended mid-call corrupts its frame
    stack and can segfault. Other greenlets hand work over via
    ``JSRuntime.enqueue``.
  * The extension deadlocks against gevent's hub during interpreter shutdown, so
    the ``pypkjs`` entry point shuts the runtime down explicitly and then
    ``os._exit``s.
"""

__author__ = 'rhaist'

import json

import quickjs


class JSError(Exception):
    """A JS-side error surfaced into Python.

    Named-compatible with the small slice of the old ``STPyV8.JSError`` surface
    the runtime relied on (``.stackTrace``).
    """
    def __init__(self, message):
        super(JSError, self).__init__(message)
        self.stackTrace = message


def from_js(value):
    """Convert a value received from JS into plain Python.

    Scalars pass through untouched; JS objects/arrays come back as
    dicts/lists. Callback positions must NOT be passed through here -- keep them
    opaque and callable.
    """
    if isinstance(value, quickjs.Object):
        return json.loads(value.json())
    return value


class JSContext(object):
    def __init__(self):
        self._ctx = quickjs.Context()

    def eval(self, src):
        # NB: this is the QuickJS *JavaScript* engine evaluating the app's JS --
        # running JS is the entire purpose of pypkjs, sandboxed inside QuickJS.
        # It is not Python eval().
        try:
            result = self._ctx.eval(src)
        except quickjs.JSException as e:
            raise JSError(str(e))
        self.pump()
        return result

    def pump(self):
        """Drain the QuickJS microtask queue (Promise callbacks, async/await).
        QuickJS does not run these automatically -- they only fire when we execute
        pending jobs, so this must run after evaluating code or invoking callbacks."""
        while self._ctx.execute_pending_job():
            pass

    def get(self, name):
        return self._ctx.get(name)

    def to_js(self, value):
        """Plain Python (dict/list/scalar) -> a live JS value."""
        return self._ctx.parse_json(json.dumps(value))

    def expose(self, js_name, obj, methods):
        """Expose selected Python methods of ``obj`` as JS global ``js_name``.

        Each JS call ``js_name.method(...)`` lands on ``obj.method(...)`` with the
        JS arguments delivered per-parameter (data as quickjs objects, callbacks
        as callables). The ported method body converts its data arguments with
        ``from_js`` as needed.
        """
        for m in methods:
            self._ctx.add_callable("__{0}_{1}".format(js_name, m), self._wrap(getattr(obj, m)))
        # wiring is composed only from code-controlled identifiers (js_name and
        # the method names), never from JS/app input.
        wiring = "var {0} = {0} || {{}};".format(js_name) + "".join(
            self._method_js(js_name + "." + m, "__{0}_{1}".format(js_name, m))
            for m in methods)
        self._ctx.eval(wiring)

    @staticmethod
    def _method_js(target, native_name):
        """JS for `target = function(){...}` delegating to a wrapped native.

        The error check pairs with _wrap: a Python exception crosses as the
        __pypkjs_error__ sentinel and is re-thrown here as a normal, catchable
        JS Error (the binding's own path is an uncatchable InternalError that
        escapes the whole eval as SystemError)."""
        return ("{0} = function() {{ var r = {1}.apply(null, arguments); "
                "if (r && r.__pypkjs_error__) {{ throw new Error(r.__pypkjs_error__); }} "
                "return r; }};").format(target, native_name)

    def _wrap(self, fn):
        """Auto-convert Python container return values into JS values, and turn
        Python exceptions into the sentinel _method_js re-throws JS-side.

        Return values are always data (a callback is never *returned* to JS), so
        the container conversion is safe and spares every method an explicit
        ``to_js``.
        """
        def native(*args):
            try:
                result = fn(*args)
            except Exception as e:
                return self.to_js({'__pypkjs_error__': str(e) or e.__class__.__name__})
            if isinstance(result, (dict, list)):
                return self.to_js(result)
            return result
        return native
