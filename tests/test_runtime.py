"""Integration tests for the PebbleKit JS runtime (console, timers, events,
localStorage, XHR) running on quickjs-ng.

Each test runs JS through tests/runtime_harness.py in a subprocess -- see that
file for why it can't run in-process.
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest

HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runtime_harness.py')


def run_js(src, secs=0.5, persist=None, glance_delay=None, webview_response=None):
    config = {'src': src, 'secs': secs, 'persist': persist, 'glance_delay': glance_delay,
              'webview_response': webview_response}
    proc = subprocess.run([sys.executable, HARNESS], input=json.dumps(config),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, "harness exited %d\n%s" % (proc.returncode, proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


class _Handler(http.server.BaseHTTPRequestHandler):
    def _respond(self):
        if self.path.startswith('/slow'):
            time.sleep(0.5)
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length).decode('utf-8')
        out = json.dumps({'path': self.path, 'body': body,
                          'ua': self.headers.get('User-Agent')}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('X-Foo', 'bar')
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _respond

    def log_message(self, *args):
        pass


@pytest.fixture(scope='module')
def server():
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield 'http://127.0.0.1:%d' % httpd.server_address[1]
    httpd.shutdown()


def test_console_formats_any_value_without_throwing():
    logs = run_js("""
        var circular = {}; circular.self = circular;
        console.log('s', 1, 2.5, 1e21, true, null, undefined);
        console.log(new Error('boom'));
        console.log({a: [1, 2]}, [3]);
        console.log(circular);
        console.log(function f() {});
        console.log(Symbol('x'));
        console.log(Object.create(null));
        console.warn('w'); console.info('i'); console.error('e');
    """)
    assert logs == [
        's 1 2.5 1e+21 true null undefined',
        'Error: boom',
        '{"a":[1,2]} [3]',
        '[object Object]',
        'function f() {}',
        'Symbol(x)',
        '{}',
        'w', 'i', 'e',
    ]


def test_timers():
    logs = run_js("""
        setTimeout(function(a, b) { console.log('args', a, b); }, 10, 'x', 2);
        setTimeout(function() { console.log('no delay'); });
        var t = setTimeout(function() { console.log('cleared?!'); }, 20);
        clearTimeout(t);
        var n = 0;
        var i = setInterval(function() { if (++n === 3) { clearInterval(i); console.log('interval', n); } }, 5);
    """)
    assert sorted(logs) == ['args x 2', 'interval 3', 'no delay']


def test_events():
    logs = run_js("""
        console.log(Event.AT_TARGET, new Event('x', {bubbles: true}).bubbles);
        var e = new Event('a'); e.initEvent('b', true, false); console.log(e.type, e.bubbles);
        var removed = function() { console.log('removed listener ran?!'); };
        Pebble.addEventListener('ready', removed);
        Pebble.removeEventListener('ready', removed);
        Pebble.addEventListener('ready', function(e) { console.log('ready', e.type); e.stopImmediatePropagation(); });
        Pebble.addEventListener('ready', function() { console.log('after stopImmediatePropagation?!'); });
        Promise.resolve('p').then(function(v) { console.log('promise', v); });
    """)
    assert logs == ['2 true', 'b true', 'promise p', 'ready ready']


def test_async_errors_are_logged_not_fatal():
    logs = run_js("""
        setTimeout(function() { throw new Error('async boom'); }, 5);
        setTimeout(function() { console.log('still running'); }, 30);
    """)
    assert logs[0] == 'Error running asynchronous JavaScript:'
    assert logs[1].startswith('Error: async boom')
    assert logs[-1] == 'still running'


def test_local_storage_persists(tmp_path):
    logs = run_js("""
        localStorage.setItem('a', 'short');
        localStorage.b = true;
        console.log(localStorage.length, localStorage.getItem('b'), localStorage.missing);
    """, persist=str(tmp_path))
    assert logs == ['2 true null']
    # Overwriting with a longer value only reaches disk when the store is closed.
    run_js("localStorage.setItem('a', new Array(500).join('x'));", persist=str(tmp_path))
    logs = run_js("""
        console.log(localStorage.length, localStorage.getItem('a').length, Object.keys(localStorage).sort().join(','));
    """, persist=str(tmp_path))
    assert logs == ['2 499 a,b']


def test_xhr(server):
    logs = run_js("""
        var x = new XMLHttpRequest();
        x.open('POST', '%(s)s/post');
        x.setRequestHeader('Content-Type', 'application/json');
        x.onload = function() {
            console.log('async', this.status, this.getResponseHeader('x-foo'), JSON.parse(this.responseText).body);
        };
        x.send(JSON.stringify({city: 'Z\\u00fcrich \\u2603'}));

        var j = new XMLHttpRequest();
        j.responseType = 'json';
        j.open('GET', '%(s)s/json');
        j.onload = function() { console.log('json', this.response.path); };
        j.send();

        var s = new XMLHttpRequest();
        s.open('GET', '%(s)s/sync', false);
        s.send();
        console.log('sync', s.status, s.readyState);

        var a = new XMLHttpRequest();
        a.open('GET', '%(s)s/slow');
        a.onload = function() { console.log('aborted request loaded?!'); };
        a.send();
        a.abort();
    """ % {'s': server}, secs=1.5)
    assert logs[0] == 'sync 200 4'
    assert sorted(logs[1:]) == ['async 200 bar {"city":"Zürich ☃"}', 'json /json']


def test_xhr_user_agent(server):
    logs = run_js("""
        function ua(header) {
            var x = new XMLHttpRequest();
            x.open('GET', '%(s)s/ua', false);
            if (header) x.setRequestHeader('User-Agent', header);
            x.send();
            console.log(JSON.parse(x.responseText).ua);
        }
        ua();
        ua('weather-face/1.0');
    """ % {'s': server})
    assert logs[0].startswith('pypkjs/')
    assert logs[1] == 'weather-face/1.0'


def test_glance_callback_during_sync_xhr(server):
    """The blobdb callback fires on another greenlet while JS is blocked in a sync
    XHR. Running the JS callback there instead of on the event loop segfaults."""
    logs = run_js("""
        Pebble.appGlanceReload([], function() {
            console.log('glance ok', String(new Error('x').stack).length > 0);
        }, function() {});
        var s = new XMLHttpRequest();
        s.open('GET', '%s/slow', false);
        s.send();
        console.log('sync done', s.status);
    """ % server, secs=1.5, glance_delay=0.1)
    assert logs == ['sync done 200', 'glance ok true']


def test_long_built_strings_reach_python():
    # quickjs-ng keeps long concatenations as ropes, which the binding couldn't convert.
    logs = run_js("""
        try {
            Pebble.insertTimelinePin('{"id": "' + 'x'.repeat(2000) + '"}');
            console.log('ok');
        } catch (e) {
            console.log(String(e));
        }
    """)
    assert logs == ['ok']


def test_config_page_response_reaches_webviewclosed():
    # pebble-tool hands the response over as bytes; it must still reach JS as a string.
    response = '{"color":"#ff0000","name":"Ünïcode"}'
    logs = run_js("""
        Pebble.addEventListener('webviewclosed', function(e) {
            console.log(typeof e.response + ' ' + JSON.parse(e.response).name);
        });
    """, secs=1, webview_response=response)
    assert logs == ['string \u00dcn\u00efcode']
