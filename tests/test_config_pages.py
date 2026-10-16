"""Tests for config pages talking to their pbw's plugin and PKJS (Pebble.sendMessage), driven over
the page bridge the injected shim uses. The pages are served by tests/config_harness.py.
"""
import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import pytest
import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, 'config_harness.py')

UUID = '11111111-2222-3333-4444-555555555555'

PLUGIN = {'description': 'Test plugin', 'sources': [], 'actions': []}

PAGE = "<html><head><title>Settings</title><script>window.pageSawPebble = !!window.Pebble;</script></head></html>"

COUNTING_PLUGIN = """
var count = 0;
Pebble.registerConfigHandler(function (message, respond) {
  if (message.type === 'push') {
    Pebble.sendConfigMessage({ pushed: message.value });
    respond({ ok: true });
  } else if (message.type === 'later') {
    setTimeout(function () { respond({ late: true }); }, 50);
  } else {
    count += 1;
    respond({ count: count, echo: message });
  }
});
"""

APP = """
Pebble.addEventListener('configmessage', function (e) {
  if (e.data.type === 'push') {
    Pebble.sendConfigMessage({ fromApp: true });
  }
  e.respond({ app: e.data });
});
"""


def make_pbw(tmp_path, plugin_script=None, config_page='config.html', page=PAGE):
    appinfo = {'uuid': UUID, 'shortName': 'Demo', 'longName': 'Demo Plugin', 'manifestVersion': 2,
               'appKeys': {}, 'capabilities': [], 'configPage': config_page}
    if plugin_script is not None:
        appinfo['plugin'] = PLUGIN
    path = str(tmp_path / 'demo.pbw')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('appinfo.json', json.dumps(appinfo))
        if plugin_script is not None:
            z.writestr('plugin.js', plugin_script)
        if page is not None:
            z.writestr('config.html', page)
    return path


@contextlib.contextmanager
def serving(pbws, app_src=None, secs=4, opened_url=None):
    config = {'pbws': pbws, 'secs': secs, 'persist': None,
              'app': {'uuid': UUID, 'src': app_src} if app_src else None, 'opened_url': opened_url}
    proc = subprocess.Popen([sys.executable, HARNESS], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    proc.stdin.write(json.dumps(config))
    proc.stdin.close()
    port = int(proc.stdout.readline())
    server = Server(port)
    try:
        yield server
    finally:
        server.logs = json.loads(proc.stdout.read().strip().splitlines()[-1])
        proc.wait(timeout=30)


class Server(object):
    def __init__(self, port):
        self.base = 'http://127.0.0.1:%d/config/%s/' % (port, UUID)
        self.logs = None

    def page(self):
        return urllib.request.urlopen(self.base).read().decode('utf-8')

    def bridge(self):
        return Bridge(self.base.replace('http', 'ws') + 'bridge')


class Bridge(object):
    """The page's side of the bridge, as the shim speaks it."""

    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=10)
        self.next_id = 0
        self.events = []

    def call(self, method, target='', message=None):
        self.next_id += 1
        call_id = self.next_id
        self.ws.send(json.dumps({'id': call_id, 'method': method, 'target': target, 'message': message}))
        while True:
            event = self._receive()
            if event['kind'] == 'reply' and event['id'] == call_id:
                return json.loads(event['json'])
            self.events.append(event)

    def wait_for(self, kind, target):
        for event in self.events:
            if event['kind'] == kind and event['target'] == target:
                return event
        while True:
            event = self._receive()
            self.events.append(event)
            if event['kind'] == kind and event['target'] == target:
                return event

    def _receive(self):
        return json.loads(self.ws.recv())

    def close(self):
        self.ws.close()


def test_page_is_served_with_the_shim_ahead_of_its_scripts(tmp_path):
    with serving([make_pbw(tmp_path, COUNTING_PLUGIN)]) as server:
        html = server.page()
    assert html.index('__pebbleConfig') < html.index('pageSawPebble')
    assert '"/config/%s/bridge"' % UUID in html


def test_unknown_page_is_404(tmp_path):
    with serving([make_pbw(tmp_path, COUNTING_PLUGIN, config_page=None, page=None)]) as server:
        with pytest.raises(urllib.error.HTTPError) as e:
            server.page()
    assert e.value.code == 404


def test_plugin_target(tmp_path):
    with serving([make_pbw(tmp_path, COUNTING_PLUGIN)]) as server:
        bridge = server.bridge()
        bridge.wait_for('ready', 'plugin')
        assert bridge.call('hello') == {'ready': ['plugin']}
        assert bridge.call('targets') == [{'id': 'plugin', 'name': 'Demo Plugin'}]
        assert bridge.call('sendMessage', 'plugin', {'type': 'hi'}) == {'count': 1, 'echo': {'type': 'hi'}}
        # One session for as long as the page is open.
        assert bridge.call('sendMessage', 'plugin', {'type': 'hi'})['count'] == 2
        assert bridge.call('sendMessage', 'plugin', {'type': 'later'}) == {'late': True}
        bridge.close()
        # A new page gets a new session.
        bridge = server.bridge()
        assert bridge.call('sendMessage', 'plugin', {'type': 'hi'})['count'] == 1
        bridge.close()


def test_plugin_push_reaches_the_page(tmp_path):
    with serving([make_pbw(tmp_path, COUNTING_PLUGIN)]) as server:
        bridge = server.bridge()
        assert bridge.call('sendMessage', 'plugin', {'type': 'push', 'value': 7}) == {'ok': True}
        event = bridge.wait_for('message', 'plugin')
        assert json.loads(event['json']) == {'pushed': 7}
        bridge.close()


@pytest.mark.parametrize('method, target, expected', [
    ('sendMessage', 'pkjs', {'error': "no script called 'pkjs' (have: plugin)"}),
    ('webSignIn', '', {'error': 'web sign-in is not available for this app'}),
    ('nope', '', {'error': "unknown method 'nope'"}),
])
def test_errors(tmp_path, method, target, expected):
    with serving([make_pbw(tmp_path, COUNTING_PLUGIN)]) as server:
        bridge = server.bridge()
        assert bridge.call(method, target, {}) == expected
        bridge.close()


def test_plugin_without_a_config_handler(tmp_path):
    with serving([make_pbw(tmp_path, 'Pebble.registerSourceHandler(function () {});')]) as server:
        bridge = server.bridge()
        assert bridge.call('sendMessage', 'plugin', {}) == {'error': 'no config handler registered'}
        bridge.close()


def test_running_app_is_the_pkjs_target(tmp_path):
    with serving([make_pbw(tmp_path, COUNTING_PLUGIN)], app_src=APP) as server:
        time.sleep(0.5)  # the app's JS starting
        bridge = server.bridge()
        bridge.wait_for('ready', 'plugin')
        assert [e['target'] for e in bridge.events if e['kind'] == 'ready'] == ['pkjs', 'plugin']
        assert bridge.call('sendMessage', 'pkjs', {'type': 'hi'}) == {'app': {'type': 'hi'}}
        assert bridge.call('sendMessage', 'pkjs', {'type': 'push'}) == {'app': {'type': 'push'}}
        assert json.loads(bridge.wait_for('message', 'pkjs')['json']) == {'fromApp': True}
        bridge.close()


def test_app_without_a_configmessage_listener(tmp_path):
    with serving([make_pbw(tmp_path)], app_src="console.log('up');") as server:
        time.sleep(0.5)
        bridge = server.bridge()
        assert bridge.call('targets') == [{'id': 'pkjs', 'name': 'Demo Plugin'}]
        assert bridge.call('sendMessage', 'pkjs', {}) == {'error': 'no configmessage listener registered'}
        bridge.close()


def test_page_opened_with_open_url_gets_the_bridge(tmp_path):
    url = 'data:text/html;charset=utf-8,' + urllib.parse.quote('<html><head></head><body>Legacy é</body></html>')
    with serving([make_pbw(tmp_path, config_page=None, page=None)], app_src=APP, opened_url=url) as server:
        time.sleep(0.5)
        html = server.page()
        assert '__pebbleConfig' in html and 'Legacy é' in html
        bridge = server.bridge()
        assert bridge.call('sendMessage', 'pkjs', {'type': 'hi'}) == {'app': {'type': 'hi'}}
        bridge.close()
