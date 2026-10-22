"""Serve config pages (pypkjs.runner.config_pages) for test_config_pages.py to drive.

Runs in a subprocess for the same reasons as runtime_harness.py.

stdin: JSON {"pbws": [path], "secs": float, "persist": str|null,
             "app": {"uuid": str, "src": str}|null, "opened_url": str|null}
stdout: first line the port served on; last line a JSON list of log lines.
With "app", that pbw's PKJS runs as the watch's running app; "opened_url" is a data: URL it
opened with Pebble.openURL.
"""
import json
import os
import sys
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pypkjs  # noqa: F401  (gevent monkey-patching)
import gevent
from gevent import pywsgi
from geventwebsocket.handler import WebSocketHandler
from unittest import mock

from plugin_harness import HarnessRunner, HarnessRuntime


def main():
    config = json.loads(sys.stdin.read())
    runner = HarnessRunner(config.get('persist'), None)
    for path in config.get('pbws', []):
        runner.load(path)

    app = config.get('app')
    if app:
        uuid = UUID(app['uuid'])
        rt = HarnessRuntime(mock.MagicMock(), runner.pbws[uuid], runner, persist_dir=config.get('persist'))
        runner.js, runner.running_uuid = rt, uuid
        gevent.spawn(rt.run, app['src'])
        if config.get('opened_url'):
            runner.config_pages.open_data_page(uuid, config['opened_url'])

    server = pywsgi.WSGIServer(('127.0.0.1', 0), runner.config_pages.handle, handler_class=WebSocketHandler,
                               log=None)
    server.start()
    sys.stdout.write('%d\n' % server.server_port)
    sys.stdout.flush()
    gevent.sleep(config.get('secs', 3))

    sys.stdout.write(json.dumps(runner.logs) + "\n")
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
