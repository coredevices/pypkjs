"""The phone's side of the watch microphone: streams from mic_stream_to_phone_start() to the
app's PebbleKit JS, and the microphone permission (see permissions.py).

Mirrors the phone app: a stream is accepted only for the running app's PebbleKit JS, once it is
ready, if the app may use the microphone. JS gets `audiostreamstart` ({sampleRate, format:
'pcm16le'}), then `audiostream` ({data: ArrayBuffer}) every 100 ms of decoded audio, and
`audiostreamstop`.
"""

import logging

import gevent
from libpebble2.protocol.audio import AudioStream, DataTransfer, StopTransfer

from .permissions import MicrophonePermissions, WATCH_MICROPHONE
from .protocol import (AudioStreamSetupResult, SessionType, SetupResult, VoiceControl,
                       FLAG_APP_INITIATED, parse_session_setup, stop_transfer)

try:
    from ._speex import Decoder
except ImportError:  # Built without its extension, e.g. from a source checkout.
    Decoder = None

logger = logging.getLogger('pypkjs.microphone')

# How long the phone app waits for PebbleKit JS to be ready before refusing a stream.
JS_READY_TIMEOUT = 6


class Stream(object):
    def __init__(self, session_id, app_uuid, sample_rate, frame_size):
        self.session_id = session_id
        self.app_uuid = app_uuid
        self.sample_rate = sample_rate
        self.decoder = Decoder()
        if self.decoder.frame_size != frame_size:
            raise ValueError("the watch encodes %d-sample frames; expected %d"
                             % (frame_size, self.decoder.frame_size))
        self.batch_bytes = sample_rate // 10 * 2
        self.pending = bytearray()
        self.accepted = False


class Microphone(object):
    def __init__(self, runner):
        self.runner = runner
        self.permissions = MicrophonePermissions(runner)
        self.stream = None
        self._warned = set()

    def attach(self):
        """Start answering the watch, once connected."""
        pebble = self.runner.pebble.pebble
        pebble.register_endpoint(VoiceControl, self._handle_voice_control)
        pebble.register_endpoint(AudioStream, self._handle_audio)

    @property
    def _pebble(self):
        return self.runner.pebble.pebble

    def _log(self, message):
        self.runner.log_output(message)

    def _reply(self, setup, result):
        self._pebble.send_packet(AudioStreamSetupResult(
            flags=FLAG_APP_INITIATED if setup.app_uuid is not None else 0,
            result=result, session_id=setup.session_id))

    def _refuse(self, setup, result, why):
        self._log("Refused a microphone stream: %s." % why)
        self._reply(setup, result)

    def _handle_voice_control(self, packet):
        try:
            setup = parse_session_setup(packet)
        except ValueError as e:
            logger.warning("Malformed voice session setup: %s", e)
            return
        # Dictation is `pebble transcribe`'s.
        if setup is None or setup.session_type != SessionType.AudioStream:
            return
        if self.stream is not None:
            self._end_stream(notify_watch=True)
        if setup.app_uuid is None:
            return self._refuse(setup, SetupResult.FailDisabled, "only watchapps can stream to the phone")
        granted, why = self.permissions.decision(setup.app_uuid)
        if not granted:
            return self._refuse(setup, SetupResult.FailDisabled, why)
        if self.runner.running_uuid != setup.app_uuid or self.runner.js is None:
            return self._refuse(setup, SetupResult.FailServiceUnavailable,
                                "%s has no PebbleKit JS running to receive it" % self.runner.app_title(setup.app_uuid))
        if Decoder is None:
            return self._refuse(setup, SetupResult.FailServiceUnavailable,
                                "this pypkjs was installed without its Speex decoder")
        if setup.speex is None:
            return self._refuse(setup, SetupResult.FailServiceUnavailable, "the watch sent no Speex encoder info")
        sample_rate, _, frame_size = setup.speex
        try:
            self.stream = Stream(setup.session_id, setup.app_uuid, sample_rate, frame_size)
        except ValueError as e:
            return self._refuse(setup, SetupResult.FailServiceUnavailable, str(e))
        gevent.spawn(self._accept, setup, self.stream)

    @staticmethod
    def _ready_js(js):
        """The app's Pebble object, once its JS has fired `ready`."""
        pebble = getattr(getattr(js, 'pjs', None), 'pebble', None)
        return pebble if pebble is not None and pebble.is_ready else None

    def _accept(self, setup, stream):
        """Accept once the app's JS is ready, like the phone app."""
        waited = 0
        while self._ready_js(self.runner.js) is None and waited < JS_READY_TIMEOUT:
            gevent.sleep(0.1)
            waited += 0.1
        if stream is not self.stream:
            return
        pebble = self._ready_js(self.runner.js)
        if pebble is None:
            self.stream = None
            return self._refuse(setup, SetupResult.FailServiceUnavailable, "PebbleKit JS didn't get ready in time")
        stream.accepted = True
        self._reply(setup, SetupResult.Success)
        pebble._audio_event('audiostreamstart', {'sampleRate': stream.sample_rate, 'format': 'pcm16le'})

    def _handle_audio(self, packet):
        stream = self.stream
        if stream is None or packet.session_id != stream.session_id or not stream.accepted:
            return
        if isinstance(packet.data, DataTransfer):
            for frame in packet.data.frames:
                try:
                    stream.pending += stream.decoder.decode(frame.data)
                except ValueError:
                    pass  # The phone app drops undecodable frames too.
            while len(stream.pending) >= stream.batch_bytes:
                self._deliver(stream.pending[:stream.batch_bytes])
                del stream.pending[:stream.batch_bytes]
        elif isinstance(packet.data, StopTransfer):
            self._end_stream(notify_watch=False)

    def _deliver(self, pcm):
        pebble = self._ready_js(self.runner.js)
        if pebble is not None:
            pebble._audio_event('audiostream', None, bytes(pcm))

    def _end_stream(self, notify_watch):
        stream, self.stream = self.stream, None
        if stream is None:
            return
        if notify_watch:
            self._pebble.send_packet(stop_transfer(stream.session_id))
        if stream.accepted:
            if stream.pending:
                self._deliver(stream.pending)
            pebble = self._ready_js(self.runner.js)
            if pebble is not None and pebble.uuid == stream.app_uuid:
                pebble._audio_event('audiostreamstop', {})

    def js_stopping(self, uuid):
        """The app's JS is going away: the phone app ends its stream."""
        if self.stream is not None and self.stream.app_uuid == uuid:
            self._end_stream(notify_watch=True)

    def permission_changed(self, uuid):
        if self.stream is not None and self.stream.app_uuid == uuid and not self.permissions.granted(uuid):
            self._log("%s lost the microphone; stopping its stream." % self.runner.app_title(uuid))
            self._end_stream(notify_watch=True)
