"""Stream watch microphone audio to PebbleKit JS through a real JSRuntime and print what happened
as JSON.

Invoked as a subprocess by test_microphone.py, for the reasons in runtime_harness.py.

stdin: JSON {"manifest": {...} (merged into the app's appinfo), "js": bool (whether the app's JS
       runs), "seconds": float of tone to stream, "override": "grant"|"deny"|null,
       "stop_js_after": float|null (stop the JS mid-stream after that many seconds of audio),
       "capabilities": int, "install": bool}
stdout (last line): JSON {"logs": [...], "sent": [hex of each packet sent to the watch],
                          "blobdb": [[db, key hex, value hex]]}
"""
import json
import math
import os
import struct
import sys
import tempfile
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pypkjs  # noqa: F401  (gevent monkey-patching)
import gevent
from unittest import mock

from libpebble2.protocol.base import PebblePacket
from libpebble2.protocol.blobdb import BlobDatabaseID, BlobStatus

from pypkjs.javascript.runtime import JSRuntime
from pypkjs.microphone import Microphone
from pypkjs.microphone._speex import Encoder
from pypkjs.runner import Runner

APP = UUID('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
SESSION = 0x1234

JS = """
var bytes = 0, chunks = 0, sumsq = 0;
Pebble.addEventListener('audiostreamstart', function(e) {
  console.log('start ' + e.sampleRate + ' ' + e.format);
});
Pebble.addEventListener('audiostream', function(e) {
  var samples = new Int16Array(e.data);
  chunks++; bytes += e.data.byteLength;
  for (var i = 0; i < samples.length; i++) { sumsq += samples[i] * samples[i]; }
});
Pebble.addEventListener('audiostreamstop', function(e) {
  console.log('stop ' + chunks + ' ' + bytes + ' ' + (e.data instanceof ArrayBuffer) + ' rms ' +
              Math.round(Math.sqrt(sumsq / (bytes / 2))));
});
"""


def frame(endpoint, payload):
    return struct.pack('>HH', len(payload), endpoint) + payload


def parse(message):
    return PebblePacket.parse_message(message)[0]


def session_setup():
    attrs = struct.pack('<BH', 0x03, 16) + APP.bytes
    attrs += struct.pack('<BH', 0x01, 29) + b'1.2.1'.ljust(20, b'\0') + struct.pack('<IHBH', 16000, 9800, 4, 320)
    return frame(0x2af8, struct.pack('<BIBHB', 0x01, 1, 0x04, SESSION, 2) + attrs)


def audio_frame(encoded):
    return frame(0x2710, struct.pack('<BHBB', 0x02, SESSION, 1, len(encoded)) + encoded)


class HarnessRunner(object):
    def __init__(self, config, persist):
        self.persist_dir = persist
        self.logs = []
        self.sent = []
        self.blobdb_inserts = []
        manifest = {'uuid': str(APP), 'appKeys': {}, 'capabilities': [], 'shortName': 'Mic',
                    'manifestVersion': 2, 'usesPermissions': ['WatchMicrophone']}
        manifest.update(config.get('manifest', {}))
        self.pbws = {APP: Runner.PBW(APP, JS, manifest, {}, [])}
        self.appmessage = mock.MagicMock()
        self.plugins = mock.MagicMock()
        self.pebble = mock.MagicMock()
        self.pebble.pebble.send_packet.side_effect = lambda p: self.sent.append(p.serialise_packet().hex())
        self.pebble.pebble.watch_info.capabilities = config.get('capabilities', 0)

        def insert(db, key, value, callback):
            self.blobdb_inserts.append([int(db), key.bytes.hex(), bytes(value).hex()])
            callback(BlobStatus.Success)
        self.pebble.blobdb.insert.side_effect = insert
        self.js = None
        self.running_uuid = None
        self.microphone = Microphone(self)

    def app_title(self, uuid):
        return self.pbws[uuid].manifest['shortName']

    def log_output(self, message):
        self.logs.append(message)

    def start_js(self):
        self.running_uuid = APP
        self.js = HarnessRuntime(self.pebble, self.pbws[APP], self)
        self.js_greenlet = gevent.spawn(self.js.run, JS)

    def stop_js(self):
        self.microphone.js_stopping(self.running_uuid)
        self.js.stop()
        self.js_greenlet.join(5)
        self.js = None
        self.running_uuid = None


class HarnessRuntime(JSRuntime):
    def log_output(self, message):
        self.runner.logs.append(message)


def main():
    config = json.loads(sys.stdin.read())
    persist = tempfile.mkdtemp()
    runner = HarnessRunner(config, persist)
    mic = runner.microphone

    if config.get('install'):
        wrapped = mic.permissions.blobdb_for_install(runner.pebble.blobdb)
        wrapped.insert(BlobDatabaseID.App, APP, b'\x01' * 126, lambda status: None)
    if config.get('override'):
        mic.permissions.set_override(APP, config['override'])
    if config.get('js', True):
        runner.start_js()
        gevent.sleep(0.3)

    mic._handle_voice_control(parse(session_setup()))
    gevent.sleep(0.3)

    encoder = Encoder()
    frames = int(config.get('seconds', 1) * 50)
    for n in range(frames):
        if config.get('stop_js_after') is not None and n == int(config['stop_js_after'] * 50):
            runner.stop_js()
        if config.get('revoke_after') is not None and n == int(config['revoke_after'] * 50):
            mic.permissions.set_override(APP, 'deny')
        pcm = struct.pack('<320h', *[int(8000 * math.sin(2 * math.pi * 440 * (n * 320 + i) / 16000))
                                     for i in range(320)])
        mic._handle_audio(parse(audio_frame(encoder.encode(pcm))))
    mic._handle_audio(parse(frame(0x2710, struct.pack('<BH', 0x03, SESSION))))
    gevent.sleep(0.3)
    if runner.js is not None:
        runner.stop_js()

    sys.stdout.write(json.dumps({'logs': runner.logs, 'sent': runner.sent,
                                 'blobdb': runner.blobdb_inserts}) + "\n")
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
