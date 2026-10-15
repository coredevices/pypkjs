"""Behavioural parity tests for the QuickJS runtime.

Two layers:

1. Golden assertions on quickjs-ng for the JavaScript semantics PebbleKit JS apps
   actually rely on (number formatting, string/array/JSON/date/typed-array
   behaviour, the marshalling boundary). These run anywhere with just quickjs-ng
   and lock the behaviour in for CI.

2. An optional differential check that runs the SAME expressions through V8
   (STPyV8) and asserts identical output -- skipped automatically when STPyV8 is
   not installed (i.e. everywhere except a dev box that still has it). This is how
   the golden values above were validated as V8-identical in the first place.

Deliberately bare `quickjs` only -- no `pypkjs` import, so no gevent monkeypatch
and no interpreter-shutdown deadlock in CI.
"""
import json

import quickjs


def _val(expr):
    """String(expr) evaluated in a fresh quickjs-ng context."""
    return quickjs.Context().eval("String(%s)" % expr)


# (expression, expected String() result) -- every value verified identical to V8.
GOLDEN = [
    ("0.1 + 0.2", "0.30000000000000004"),
    ("(255).toString(16)", "ff"),
    ("(-0).toString()", "0"),
    ("1 / 0", "Infinity"),
    ("0 / 0", "NaN"),
    ("(123.456).toFixed(2)", "123.46"),
    ("(0.3).toFixed(1)", "0.3"),
    ("(1.005).toFixed(2)", "1.00"),          # classic float-rounding quirk, same on both
    ("parseInt('08')", "8"),
    ("'caf\\u00e9'.length", "4"),
    ("'ab'.padStart(5, '*')", "***ab"),
    ("'abcabc'.replace(/a/g, 'X')", "XbcXbc"),
    ("JSON.stringify([NaN, Infinity, null])", "[null,null,null]"),
    ("JSON.stringify({b: 1, a: 2})", '{"b":1,"a":2}'),   # insertion order preserved
    ("new Date(0).toISOString()", "1970-01-01T00:00:00.000Z"),
    ("Date.parse('2020-01-01T00:00:00Z')", "1577836800000"),
    ("JSON.stringify([3, 1, 20, 2].sort())", "[1,2,20,3]"),        # default: lexicographic
    ("JSON.stringify([3, 1, 20, 2].sort((a, b) => a - b))", "[1,2,3,20]"),
    ("new Int8Array([200])[0]", "-56"),      # signed wraparound
    ("new Uint8Array([256, 257])[0]", "0"),  # unsigned wraparound
    ("JSON.stringify(Array.from(new Uint8Array([1, 2, 3])))", "[1,2,3]"),
    ("'5' * 2", "10"),
    ("[] + []", ""),
    ("typeof null", "object"),
    ("[1, 2, 3].includes(2)", "true"),
    ("Object.assign({}, {a: 1}, {b: 2}).b", "2"),
    ("`sum=${1 + 2}`", "sum=3"),
]


def test_language_semantics():
    for expr, expected in GOLDEN:
        assert _val(expr) == expected, "%s -> %r (expected %r)" % (expr, _val(expr), expected)


def _from_js(ctx, js):
    """Mirror of pypkjs.javascript.qjs.from_js without importing pypkjs (gevent)."""
    return json.loads(ctx.eval(js).json())


def test_marshalling_boundary():
    """Values that JSON cannot faithfully carry are handled predictably at the
    Python<->JS boundary. Real PebbleKit JS data payloads are plain JSON, so these
    are edge cases -- pinned here so the behaviour is intentional, not accidental."""
    c = quickjs.Context()
    assert _from_js(c, "({a: 1, b: 'x', c: [1, 2, 3]})") == {"a": 1, "b": "x", "c": [1, 2, 3]}
    assert _from_js(c, "({v: NaN})") == {"v": None}
    assert _from_js(c, "({v: Infinity})") == {"v": None}
    assert _from_js(c, "({a: 1, b: undefined})") == {"a": 1}          # undefined key dropped
    assert _from_js(c, "({d: new Date(0)})") == {"d": "1970-01-01T00:00:00.000Z"}
    assert _from_js(c, "({a: 1, f: function () {}})") == {"a": 1}     # function dropped


def test_promise_microtask():
    """Promise/async continuations only run when pending jobs are pumped."""
    c = quickjs.Context()
    seen = []
    c.add_callable("out", lambda s: seen.append(s) or None)
    c.eval("Promise.resolve('async').then(function (v) { out(v); }); out('sync');")
    assert seen == ["sync"]           # microtask not run yet
    while c.execute_pending_job():
        pass
    assert seen == ["sync", "async"]  # now it has


def test_js_throw_raises_jsexception():
    """A throwing JS callback invoked from Python raises quickjs.JSException --
    runtime.call_js translates this to qjs.JSError so the event loop survives."""
    c = quickjs.Context()
    fn = c.eval("(function() { throw new Error('kaput'); })")
    try:
        fn()
    except quickjs.JSException as e:
        assert 'kaput' in str(e)
    else:
        raise AssertionError("expected quickjs.JSException")


def test_python_exception_catchable_in_js():
    """The __pypkjs_error__ sentinel + wiring (qjs._wrap/_method_js) surfaces a
    Python exception as a normal, catchable JS Error with its real message.
    (The binding's native path is an uncatchable InternalError/SystemError.)"""
    c = quickjs.Context()
    def native():
        try:
            raise ValueError("kaput")
        except Exception as e:
            return c.parse_json(json.dumps({'__pypkjs_error__': str(e)}))
    c.add_callable("__t_boom", native)
    c.eval("var t = {}; t.boom = function() { var r = __t_boom.apply(null, arguments); "
           "if (r && r.__pypkjs_error__) { throw new Error(r.__pypkjs_error__); } return r; };")
    assert c.eval("(function() { try { t.boom(); return 'no-throw'; } "
                  "catch (e) { return e.message; } })()") == "kaput"


def test_js_function_identity_across_crossings():
    """The same JS function crossing to Python twice yields wrapper objects that
    Python == cannot match; a JS === helper can (runtime.js_same)."""
    c = quickjs.Context()
    c.eval("var f = function() {}; var g = function() {};")
    same = c.eval("(function(a, b) { return a === b; })")
    f1, f2, g1 = c.get("f"), c.get("f"), c.get("g")
    assert same(f1, f2) is True
    assert same(f1, g1) is False


def test_v8_parity():
    """Differential: identical output from V8 and quickjs-ng. Skipped without STPyV8."""
    try:
        import STPyV8
    except ImportError:
        import sys
        print("SKIP test_v8_parity: STPyV8 not installed")
        return
    for expr, _ in GOLDEN:
        with STPyV8.JSContext() as v8:
            v8_out = v8.eval("String(%s)" % expr)
        assert v8_out == _val(expr), "DIVERGENCE %s: V8=%r ngQJS=%r" % (expr, v8_out, _val(expr))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PARITY TESTS PASSED")
