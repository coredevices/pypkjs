"""Run a PebbleKit JS snippet against real plugins and print its log output as JSON.

Like runtime_harness.py, this must run in a subprocess (gevent monkey-patching, and the
quickjs-ng extension deadlocks at interpreter shutdown).

stdin: JSON {"src": str, "secs": float, "pbws": [path], "persist": str|null,
             "app_uuid": str|null, "app_keys": {name: int}, "fixtures": path|null,
             "ws_commands": [[delay, payload]]}

The fixtures file is watched for changes; each ws_commands payload (a string) is sent as a
websocket 0x0d command after `delay` seconds, and the reply logged as "WS <byte> [<payload>]".
stdout (last line): JSON list of log lines. AppMessages the app sends are logged as
"APPMSG <json>"; app launches as "LAUNCH <uuid>".
"""
import json
import logging
import os
import sys
import zipfile
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pypkjs  # noqa: F401  (gevent monkey-patching)
import gevent
from unittest import mock

from pypkjs.javascript.runtime import JSRuntime
from pypkjs.plugins.builtin import FixtureStore, builtin_plugins
from pypkjs.plugins.registry import PluginRegistry
from pypkjs.runner import Runner
from pypkjs.runner.config_pages import ConfigPages
from pypkjs.runner.websocket import WebsocketRunner


class HarnessRunner(object):
    """The slice of Runner that plugins and config pages use."""
    _load_plugin = Runner._load_plugin
    _load_config_page = Runner._load_config_page
    app_title = Runner.app_title
    is_plugin_only = staticmethod(Runner.is_plugin_only)
    plugin_list = Runner.plugin_list
    remove_plugin = Runner.remove_plugin
    _pbw_cache_dir = Runner._pbw_cache_dir

    def __init__(self, persist_dir, fixtures):
        self.persist_dir = persist_dir
        self.block_private_addresses = False
        self.logger = logging.getLogger('harness')
        self.logs = []
        self.previous_app_uuid = None
        self.plugins = PluginRegistry()
        self.plugin_fixtures = FixtureStore(fixtures, self.log_output)
        for plugin in builtin_plugins(self.plugin_fixtures, self):
            self.plugins.register(plugin)
        self.plugin_fixtures.load_file()
        self.appmessage = mock.MagicMock()
        self.appmessage.send_message.side_effect = self._send_message
        self.pbws = {}
        self.config_page_files = {}
        self.config_pages = ConfigPages(self)
        self.js = None
        self.running_uuid = None

    def _send_message(self, uuid, message):
        self.log_output('APPMSG ' + json.dumps({str(k): v.value if not isinstance(v.value, bytes) else list(v.value)
                                                for k, v in message.items()}))
        return 0

    def launch_app(self, uuid):
        self.log_output('LAUNCH %s' % uuid)

    def log_output(self, message):
        self.logs.append(message)

    def load(self, path):
        with zipfile.ZipFile(path) as z:
            appinfo = json.loads(z.open('appinfo.json').read())
            uuid = UUID(appinfo['uuid'])
            self._load_plugin(z, uuid, appinfo)
            self._load_config_page(z, uuid, appinfo)
            prefixes = [name[:-len('manifest.json')] for name in z.namelist() if name.endswith('manifest.json')]
            self.pbws[uuid] = Runner.PBW(uuid, None, appinfo, {}, prefixes)


class HarnessWebsocket(object):
    authed = True

    def __init__(self, runner):
        self.runner = runner

    def send(self, message):
        reply = bytes(message[2:]).decode('utf-8')
        self.runner.log_output('WS %d' % message[1] + (' ' + reply if reply else ''))


def send_plugin_command(runner, delay, payload):
    gevent.sleep(delay)
    runner.requires_auth = False
    WebsocketRunner.do_plugin_command(runner, HarnessWebsocket(runner), bytearray(payload.encode('utf-8')))


class HarnessRuntime(JSRuntime):
    def log_output(self, message):
        self.runner.log_output(message)


def main():
    config = json.loads(sys.stdin.read())
    runner = HarnessRunner(config.get('persist'), config.get('fixtures'))
    for path in config.get('pbws', []):
        runner.load(path)

    if config.get('fixtures'):
        gevent.spawn(runner.plugin_fixtures.watch_file, 0.1)
    for delay, payload in config.get('ws_commands', []):
        gevent.spawn(send_plugin_command, runner, delay, payload)

    pbw = mock.MagicMock()
    pbw.uuid = config.get('app_uuid') or 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    pbw.manifest = {'appKeys': config.get('app_keys') or {}, 'capabilities': [], 'shortName': 'harness'}
    rt = HarnessRuntime(mock.MagicMock(), pbw, runner, persist_dir=config.get('persist'))
    greenlet = gevent.spawn(rt.run, config['src'])
    gevent.sleep(config.get('secs', 1))
    rt.stop()
    greenlet.join(5)

    sys.stdout.write(json.dumps(runner.logs) + "\n")
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
