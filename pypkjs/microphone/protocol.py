"""Pebble Protocol messages for watch microphone streams.

A watchapp's mic_stream_to_phone_start() reuses dictation's endpoints: a session setup on
VoiceControl with session type AudioStream, then Speex frames and a stop on AudioStream.
libpebble2 only knows dictation's session types, and its reader raises on anything else, so
importing this module replaces its VoiceControl parser with one that parses no further than the
command and leaves the rest to `parse_session_setup`.
"""

import struct
from enum import IntEnum
from uuid import UUID

from libpebble2.protocol.audio import AudioStream, StopTransfer
from libpebble2.protocol.base import PebblePacket
from libpebble2.protocol.base.types import BinaryArray, Uint8, Uint16, Uint32

__all__ = ["VOICE_CONTROL", "SessionType", "SetupResult", "VoiceControl", "AudioStreamSetupResult",
           "SessionSetup", "parse_session_setup", "stop_transfer"]

VOICE_CONTROL = 0x2af8
COMMAND_SESSION_SETUP = 0x01
FLAG_APP_INITIATED = 0x01

ATTRIBUTE_SPEEX_ENCODER_INFO = 0x01
ATTRIBUTE_APP_UUID = 0x03


class SessionType(IntEnum):
    Dictation = 0x01
    Command = 0x02
    AudioStream = 0x04


class SetupResult(IntEnum):
    Success = 0x00
    FailServiceUnavailable = 0x01
    FailTimeout = 0x02
    FailRecognizerError = 0x03
    FailInvalidRecognizerResponse = 0x04
    FailDisabled = 0x05
    FailInvalidMessage = 0x06


class VoiceControl(PebblePacket):
    """Any VoiceControl message from the watch."""
    class Meta:
        endpoint = VOICE_CONTROL
        endianness = '<'

    command = Uint8()
    flags = Uint32()
    payload = BinaryArray()


class AudioStreamSetupResult(PebblePacket):
    """The phone's answer to an AudioStream session setup. Unlike dictation's, it names the
    session, so the watch can ignore an answer to a stream it has given up on."""
    class Meta:
        endpoint = VOICE_CONTROL
        endianness = '<'
        register = False

    command = Uint8(default=COMMAND_SESSION_SETUP)
    flags = Uint32()
    session_type = Uint8(default=SessionType.AudioStream)
    result = Uint8()
    session_id = Uint16()


class SessionSetup(object):
    def __init__(self, session_type, session_id, app_uuid, speex):
        self.session_type = session_type
        self.session_id = session_id
        #: None for a stream a system app started.
        self.app_uuid = app_uuid
        #: (sample_rate, bit_rate, frame_size), or None if the watch sent no encoder info.
        self.speex = speex


def parse_session_setup(packet):
    """A VoiceControl session setup, or None for any other command. Raises ValueError if it's
    malformed."""
    if packet.command != COMMAND_SESSION_SETUP:
        return None
    data = packet.payload
    try:
        session_type, session_id, count = struct.unpack_from('<BHB', data)
        offset = 4
        app_uuid = speex = None
        for _ in range(count):
            attribute, length = struct.unpack_from('<BH', data, offset)
            offset += 3
            value = data[offset:offset + length]
            if len(value) != length:
                raise ValueError("attribute %d is truncated" % attribute)
            offset += length
            if attribute == ATTRIBUTE_APP_UUID and length == 16:
                app_uuid = UUID(bytes=bytes(value))
            elif attribute == ATTRIBUTE_SPEEX_ENCODER_INFO and length == 29:
                # char version[20], u32 sample_rate, u16 bit_rate, u8 bitstream_version, u16 frame_size
                sample_rate, bit_rate, _, frame_size = struct.unpack_from('<IHBH', value, 20)
                speex = (sample_rate, bit_rate, frame_size)
    except struct.error as e:
        raise ValueError(str(e))
    if not packet.flags & FLAG_APP_INITIATED or app_uuid == UUID(int=0):
        app_uuid = None
    return SessionSetup(session_type, session_id, app_uuid, speex)


def stop_transfer(session_id):
    return AudioStream(packet_id=0x03, session_id=session_id, data=StopTransfer())
