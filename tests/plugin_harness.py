"""Run a PebbleKit JS snippet against real plugins and print its log output as JSON.

Like runtime_harness.py, this must run in a subprocess (gevent monkey-patching, and the
quickjs-ng extension deadlocks at interpreter shutdown).

stdin: JSON {"src": str, "secs": float, "pbws": [path], "persist": str|null,
             "app_uuid": str|null}
stdout (last line): JSON list of log lines.
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
from pypkjs.plugins.registry import PluginRegistry
from pypkjs.runner import Runner


class HarnessRunner(object):
    """The slice of Runner that plugins use."""
    _load_plugin = Runner._load_plugin

    def __init__(self, persist_dir):
        self.persist_dir = persist_dir
        self.block_private_addresses = False
        self.logger = logging.getLogger('harness')
        self.logs = []
        self.plugins = PluginRegistry()
        self.appmessage = mock.MagicMock()

    def log_output(self, message):
        self.logs.append(message)

    def load(self, path):
        with zipfile.ZipFile(path) as z:
            appinfo = json.loads(z.open('appinfo.json').read())
            self._load_plugin(z, UUID(appinfo['uuid']), appinfo)


class HarnessRuntime(JSRuntime):
    def log_output(self, message):
        self.runner.log_output(message)


def main():
    config = json.loads(sys.stdin.read())
    runner = HarnessRunner(config.get('persist'))
    for path in config.get('pbws', []):
        runner.load(path)

    pbw = mock.MagicMock()
    pbw.uuid = config.get('app_uuid') or 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    pbw.manifest = {'appKeys': {}, 'capabilities': [], 'shortName': 'harness'}
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
