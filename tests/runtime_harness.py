"""Run a PebbleKit JS snippet in a real JSRuntime and print its log output as JSON.

Invoked as a subprocess by test_runtime.py: importing pypkjs monkey-patches the
whole interpreter for gevent, and the quickjs-ng extension deadlocks at
interpreter shutdown, so this must not run inside the pytest process.

stdin: JSON {"src": str, "secs": float, "persist": str|null, "glance_delay": float|null}
stdout (last line): JSON list of log lines.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pypkjs  # noqa: F401  (gevent monkey-patching)
import gevent
from unittest import mock

from libpebble2.protocol.blobdb import BlobStatus

from pypkjs.javascript.runtime import JSRuntime


class HarnessRuntime(JSRuntime):
    def __init__(self, *args, **kwargs):
        super(HarnessRuntime, self).__init__(*args, **kwargs)
        self.logs = []

    def log_output(self, message):
        self.logs.append(message)


def main():
    config = json.loads(sys.stdin.read())

    pbw = mock.MagicMock()
    pbw.uuid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    pbw.manifest = {'appKeys': {}, 'capabilities': []}
    qemu = mock.MagicMock()
    if config.get('glance_delay') is not None:
        # Fire the blobdb callback from another greenlet, like libpebble2 does.
        def insert(db, key, value, callback):
            def later():
                gevent.sleep(config['glance_delay'])
                callback(BlobStatus.Success)
            gevent.spawn(later)
        qemu.blobdb.insert.side_effect = insert

    rt = HarnessRuntime(qemu, pbw, mock.MagicMock(), persist_dir=config.get('persist'))
    greenlet = gevent.spawn(rt.run, config['src'])
    gevent.sleep(config.get('secs', 0.5))
    rt.stop()
    greenlet.join(5)

    sys.stdout.write(json.dumps(rt.logs) + "\n")
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
