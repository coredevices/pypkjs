"""Tests for phone-side plugins: the PKJS API (subscribeToSource / invokeAction /
enumeratePlugins) and the host third-party plugin scripts run in.

Integration tests run through tests/plugin_harness.py in a subprocess -- see that file for why.
"""
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, 'plugin_harness.py')

# Loaded by path: importing the pypkjs package monkey-patches the interpreter for gevent.
_spec = importlib.util.spec_from_file_location(
    'plugin_manifest', os.path.join(HERE, '..', 'pypkjs', 'plugins', 'manifest.py'))
manifests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(manifests)

PLUGIN_UUID = '11111111-2222-3333-4444-555555555555'
OTHER_UUID = '66666666-7777-8888-9999-000000000000'

DEMO_PLUGIN = {
    'description': 'Test plugin',
    'sources': [{
        'category': 'demo',
        'items': ['message'],
        'properties': {'text': ['shortText']},
        'callerPermissions': ['Location'],
        'suggestedRefreshIntervalSec': 300,
    }],
    'actions': [{
        'name': 'set_text',
        'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']},
        'targets': ['demo/message/text'],
    }, {
        'name': 'hang',
    }],
}

DEMO_SCRIPT = """
Pebble.registerSourceHandler(function (request, respond) {
  respond.data({ instances: [
    { instanceId: 'm', properties: { text: { shortText: { text: localStorage.getItem('text') || 'hello' } } } },
  ] });
});
Pebble.registerActionHandler(function (request, respond) {
  if (request.action === 'hang') return;
  localStorage.setItem('text', request.args.text);
  respond.ok({ text: 'set', refreshed: ['demo/message'] });
});
"""


def make_pbw(tmp_path, uuid, plugin, script, name='plugin.pbw', manifest_version=2, oauth=None):
    appinfo = {'uuid': uuid, 'shortName': 'Demo', 'longName': 'Demo Plugin', 'manifestVersion': manifest_version,
               'plugin': dict(plugin, **({'oauth': oauth} if oauth else {}))}
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('appinfo.json', json.dumps(appinfo))
        if script is not None:
            z.writestr('plugin.js', script)
    return path


def run_js(src, pbws, secs=1.0, persist=None, app_uuid=None):
    config = {'src': src, 'pbws': pbws, 'secs': secs, 'persist': persist, 'app_uuid': app_uuid}
    proc = subprocess.run([sys.executable, HARNESS], input=json.dumps(config),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, "harness exited %d\n%s" % (proc.returncode, proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def tagged(logs, tag):
    """The JSON payloads of `console.log(tag, JSON.stringify(x))` lines."""
    return [json.loads(line[len(tag) + 1:]) for line in logs if line.startswith(tag + ' ')]


@pytest.fixture
def demo(tmp_path):
    return make_pbw(tmp_path, PLUGIN_UUID, DEMO_PLUGIN, DEMO_SCRIPT)


# ---------------------------------------------------------------------------- manifest


def test_manifest_fills_defaults_and_normalises_permissions():
    plugin = manifests.loadable_plugin({'manifestVersion': 2, 'plugin': DEMO_PLUGIN})
    source = plugin['sources'][0]
    assert source['supportsMultiple'] is False
    assert source['callerPermissions'] == [{'name': 'Location'}]
    assert plugin['script'] == 'plugin.js'
    assert plugin['actions'][1]['parameters'] is None


def test_manifest_from_legacy_pbw_is_not_loadable():
    assert manifests.loadable_plugin({'plugin': DEMO_PLUGIN}) is None
    assert manifests.loadable_plugin({'manifestVersion': 1, 'plugin': DEMO_PLUGIN}) is None
    assert manifests.loadable_plugin({'manifestVersion': 2}) is None


def test_manifest_problems_match_the_phone_apps_rules():
    plugin = manifests.loadable_plugin({'manifestVersion': 2, 'plugin': {'definesPermissions': [
        {'name': 'Home Control', 'label': 'Control lights', 'kind': 'write'},
        {'name': 'InvokeAction', 'label': 'x', 'kind': 'read'},
        {'name': 'Lights', 'label': ' ', 'kind': 'sometimes'},
    ]}})
    assert manifests.problems(plugin) == [
        "permission name 'Home Control' must be alphanumeric",
        "permission name 'InvokeAction' is reserved",
        "permission 'Lights' has no label",
        "permission 'Lights' needs a kind: one of exfiltrating, read, write",
    ]


# ---------------------------------------------------------------------------- PKJS API


def test_enumerate_plugins_omits_defaults_like_the_app(demo):
    logs = run_js("console.log('P', JSON.stringify(Pebble.enumeratePlugins()));", [demo], secs=0.3)
    assert tagged(logs, 'P') == [[{
        'uuid': PLUGIN_UUID,
        'name': 'Demo Plugin',
        'sources': [{'category': 'demo', 'items': ['message'], 'properties': {'text': ['shortText']},
                     'callerPermissions': [{'name': 'Location'}]}],
        'actions': [{'name': 'set_text',
                     'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}},
                                    'required': ['text']},
                     'targets': ['demo/message/text']},
                    {'name': 'hang'}],
    }]]


def test_subscribe_delivers_envelopes(demo):
    logs = run_js("""
        Pebble.subscribeToSource({ category: 'demo', item: 'message',
            onData: function (e) { console.log('D', JSON.stringify(e)); } });
    """, [demo])
    assert tagged(logs, 'D') == [{
        'pluginUuid': PLUGIN_UUID,
        'instances': [{'instanceId': 'm', 'properties': {'text': {'shortText': {'text': 'hello'}}}}],
    }]


def test_subscribe_to_unserved_source_errors(demo):
    logs = run_js("""
        Pebble.subscribeToSource({ category: 'demo', item: 'nope',
            onError: function (e) { console.log('E', JSON.stringify(e)); } });
    """, [demo], secs=0.3)
    assert tagged(logs, 'E') == [{'code': 'PLUGIN_UNAVAILABLE', 'message': 'No plugin for demo/nope'}]


def test_preferred_plugin_falls_back_to_one_serving_the_item(tmp_path, demo):
    other = make_pbw(tmp_path, OTHER_UUID, {'sources': []}, 'Pebble.registerSourceHandler(function(){});',
                     name='other.pbw')
    logs = run_js("""
        Pebble.subscribeToSource({ category: 'demo', item: 'message', plugin: '%s',
            onData: function (e) { console.log('D', e.pluginUuid); } });
    """ % OTHER_UUID, [other, demo])
    assert 'D %s' % PLUGIN_UUID in logs


def test_unsubscribe_stops_updates(demo):
    logs = run_js("""
        var sub = Pebble.subscribeToSource({ category: 'demo', item: 'message',
            onData: function () { console.log('data'); } });
        sub.unsubscribe();
    """, [demo])
    assert 'data' not in logs


def test_action_result_and_refresh(demo):
    logs = run_js("""
        Pebble.subscribeToSource({ category: 'demo', item: 'message',
            onData: function (e) { console.log('T', JSON.stringify(e.instances[0].properties.text.shortText.text)); } });
        setTimeout(function () {
            Pebble.invokeAction({ plugin: '%s', action: 'set_text', args: { text: 'changed' } })
                .then(function (r) { console.log('R', JSON.stringify(r)); });
        }, 400);
    """ % PLUGIN_UUID, [demo], secs=2)
    assert tagged(logs, 'R') == [{'ok': True, 'text': 'set', 'refreshed': ['demo/message']}]
    # The action's `refreshed` re-reads the source at once rather than after 300s.
    assert tagged(logs, 'T') == ['hello', 'changed']


@pytest.mark.parametrize('call, expected', [
    ("{ plugin: '%s', action: 'set_text', args: {} }" % PLUGIN_UUID,
     {'ok': False, 'code': 'INVALID_ARGS', 'message': 'missing required: text'}),
    ("{ plugin: '%s', action: 'nope' }" % PLUGIN_UUID,
     {'ok': False, 'code': 'PLUGIN_UNAVAILABLE', 'message': 'Demo Plugin has no action nope'}),
    ("{ plugin: '%s', action: 'set_text' }" % OTHER_UUID,
     {'ok': False, 'code': 'PLUGIN_UNAVAILABLE', 'message': 'No plugin %s' % OTHER_UUID}),
    ("{ plugin: '%s', action: 'hang', timeoutMs: 300 }" % PLUGIN_UUID,
     {'ok': False, 'code': 'TIMEOUT', 'message': 'no response in 300ms'}),
])
def test_action_errors(demo, call, expected):
    logs = run_js("Pebble.invokeAction(%s).then(function (r) { console.log('R', JSON.stringify(r)); });" % call,
                  [demo], secs=1)
    assert tagged(logs, 'R') == [expected]


# ---------------------------------------------------------------------------- plugin host


def plugin_source_logs(tmp_path, script, secs=1.0, oauth=None, persist=None):
    """Logs from subscribing to a demo/message source served by `script`."""
    pbw = make_pbw(tmp_path, PLUGIN_UUID, DEMO_PLUGIN, script, oauth=oauth)
    return run_js("""
        Pebble.subscribeToSource({ category: 'demo', item: 'message',
            onData: function (e) { console.log('D', JSON.stringify(e)); } });
    """, [pbw], secs=secs, persist=persist)


def test_source_error_is_logged_not_delivered(tmp_path):
    logs = plugin_source_logs(tmp_path, """
        Pebble.registerSourceHandler(function (request, respond) { respond.error('AUTH_REQUIRED', 'sign in'); });
    """)
    assert '[Demo Plugin] WARN: source error: AUTH_REQUIRED: sign in' in logs
    assert tagged(logs, 'D') == []


def test_plugin_that_fails_to_start_is_logged(tmp_path):
    logs = plugin_source_logs(tmp_path, "throw new Error('broken');")
    assert any(line.startswith('[Demo Plugin] ERROR: failed to start') and 'broken' in line for line in logs)


def test_plugin_console_and_request(tmp_path):
    logs = plugin_source_logs(tmp_path, """
        Pebble.registerSourceHandler(function (request, respond) {
          console.log('request', JSON.stringify(request));
          console.warn('careful');
          respond.data({ validUntilMs: 5, instances: [] });
        });
    """)
    assert '[Demo Plugin] request {"kind":"source","category":"demo","item":"message"}' in logs
    assert '[Demo Plugin] WARN: careful' in logs
    assert tagged(logs, 'D') == [{'pluginUuid': PLUGIN_UUID, 'instances': [], 'validUntilMs': 5}]


def test_plugin_refresh_sources_cuts_the_poll_short(tmp_path):
    pbw = make_pbw(tmp_path, PLUGIN_UUID, DEMO_PLUGIN, """
        Pebble.registerSourceHandler(function (request, respond) {
          respond.data({ instances: [{ instanceId: localStorage.getItem('text') || 'none', properties: {} }] });
        });
        Pebble.registerActionHandler(function (request, respond) {
          localStorage.setItem('text', request.args.text);
          Pebble.refreshSources(['demo/message']);
          respond.ok({});
        });
    """)
    logs = run_js("""
        Pebble.subscribeToSource({ category: 'demo', item: 'message',
            onData: function (e) { console.log('D', JSON.stringify(e.instances[0].instanceId)); } });
        setTimeout(function () {
            Pebble.invokeAction({ plugin: '%s', action: 'set_text', args: { text: 'new' } })
                .then(function (r) { console.log('R', JSON.stringify(r)); });
        }, 400);
    """ % PLUGIN_UUID, [pbw], secs=2)
    assert tagged(logs, 'R') == [{'ok': True}]
    assert tagged(logs, 'D') == ['none', 'new']


def test_secure_storage_persists_between_sessions(tmp_path):
    script = """
        Pebble.registerSourceHandler(async function (request, respond) {
          var seen = await Pebble.secureStorage.getItem('token');
          await Pebble.secureStorage.setItem('token', 'secret');
          respond.data({ instances: [{ instanceId: String(seen), properties: {} }] });
        });
    """
    persist = str(tmp_path / 'persist')
    first = plugin_source_logs(tmp_path, script, persist=persist)
    second = plugin_source_logs(tmp_path, script, persist=persist)
    assert tagged(first, 'D')[0]['instances'][0]['instanceId'] == 'null'
    assert tagged(second, 'D')[0]['instances'][0]['instanceId'] == 'secret'


def test_plugin_shares_local_storage_with_its_pbws_app(tmp_path, demo):
    logs = run_js("""
        Pebble.invokeAction({ plugin: '%s', action: 'set_text', args: { text: 'from plugin' } })
            .then(function () { console.log('S', JSON.stringify(localStorage.getItem('text'))); });
    """ % PLUGIN_UUID, [demo], persist=str(tmp_path / 'persist'), app_uuid=PLUGIN_UUID)
    assert tagged(logs, 'S') == ['from plugin']


def test_oauth_rejects_as_signed_out(tmp_path):
    logs = plugin_source_logs(tmp_path, """
        Pebble.registerSourceHandler(async function (request, respond) {
          try { await Pebble.oauth.authorize('todoist'); }
          catch (e) { respond.data({ instances: [{ instanceId: e.code, properties: {} }] }); }
        });
    """, oauth={'todoist': {}})
    assert tagged(logs, 'D')[0]['instances'][0]['instanceId'] == 'AUTH_REQUIRED'


def test_oauth_absent_unless_declared(tmp_path):
    logs = plugin_source_logs(tmp_path, """
        Pebble.registerSourceHandler(function (request, respond) {
          respond.data({ instances: [{ instanceId: typeof Pebble.oauth, properties: {} }] });
        });
    """)
    assert tagged(logs, 'D')[0]['instances'][0]['instanceId'] == 'undefined'


# ---------------------------------------------------------------------------- fetch


class _Handler(http.server.BaseHTTPRequestHandler):
    def _respond(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length).decode('utf-8')
        status = 404 if self.path.startswith('/missing') else 200
        if self.path.startswith('/bytes'):
            out = bytes([0, 1, 254, 255])
        else:
            out = json.dumps({'method': self.command, 'path': self.path, 'body': body,
                              'auth': self.headers.get('Authorization')}).encode('utf-8')
        self.send_response(status)
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


def fetch_results(tmp_path, body):
    """Run `body` (which awaits fetches and returns a JSON-able value) in a plugin."""
    logs = plugin_source_logs(tmp_path, """
        Pebble.registerSourceHandler(async function (request, respond) {
          var out;
          try { out = await (async function () { %s })(); }
          catch (e) { out = { threw: e.name + ': ' + e.message }; }
          respond.data({ instances: [{ instanceId: JSON.stringify(out), properties: {} }] });
        });
    """ % body, secs=2)
    return json.loads(tagged(logs, 'D')[0]['instances'][0]['instanceId'])


def test_fetch_json_post_with_headers(tmp_path, server):
    out = fetch_results(tmp_path, """
        var r = await fetch('%s/echo', { method: 'POST', body: 'hi', headers: { Authorization: 'Bearer x' } });
        return { ok: r.ok, status: r.status, foo: r.headers.get('x-foo'), json: await r.json() };
    """ % server)
    assert out == {'ok': True, 'status': 200, 'foo': 'bar',
                   'json': {'method': 'POST', 'path': '/echo', 'body': 'hi', 'auth': 'Bearer x'}}


def test_fetch_non_2xx_resolves_not_ok(tmp_path, server):
    out = fetch_results(tmp_path, "var r = await fetch('%s/missing'); return [r.ok, r.status];" % server)
    assert out == [False, 404]


def test_fetch_array_buffer(tmp_path, server):
    out = fetch_results(tmp_path, """
        var r = await fetch('%s/bytes', { responseType: 'arraybuffer' });
        return Array.from(new Uint8Array(await r.arrayBuffer()));
    """ % server)
    assert out == [0, 1, 254, 255]


def test_fetch_network_failure_rejects_with_type_error(tmp_path):
    out = fetch_results(tmp_path, "await fetch('http://127.0.0.1:1/'); return 'resolved';")
    assert out == {'threw': 'TypeError: fetch failed: http://127.0.0.1:1/'}


def test_base64(tmp_path):
    out = fetch_results(tmp_path, "return [btoa('\\x00\\xffhi'), atob('AP9oaQ==') === '\\x00\\xffhi'];")
    assert out == ['AP9oaQ==', True]
