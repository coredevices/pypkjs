"""The connection to an emulator: its control port and, for the Pebble protocol, its console.

A firmware takes the Pebble protocol in frames on its text console, between log lines and
the shell. Older ones only take it on the emulator's control port, with the packets that
drive the emulator itself; those keep working, the console being tried first."""

import base64
import logging
import queue
import socket
import struct
import threading
import time
import zlib

from libpebble2.communication.transports import BaseTransport, MessageTargetWatch
from libpebble2.communication.transports.qemu import QemuTransport
from libpebble2.exceptions import ConnectionError

logger = logging.getLogger("pypkjs.emulator_transport")

# ESC _ <channel> <base64(seq, payload, crc32)> ESC backslash
FRAME_START = b"\x1b_"
FRAME_END = b"\x1b\\"
FRAME_MTU = 512
FRAME_MAX_ENCODED = 1 + (1 + FRAME_MTU + 4 + 2) // 3 * 4
CHANNEL_ACK = b"A"
CHANNEL_PROTOCOL = b"P"
ACK_TIMEOUT_S = 1.0
SEND_TIMEOUT_S = 20.0

DATA = 1
OPEN = 2
CLOSE = 3
# A firmware that takes sessions on its console answers right away once booted.
OPEN_TIMEOUT_S = 2.0
OPEN_RETRY_S = 0.25


def frame_crc(channel, body):
    return zlib.crc32(body, zlib.crc32(channel))


def encode_frame(channel, seq, payload):
    body = bytes([seq]) + payload
    encoded = base64.b64encode(body + struct.pack("<I", frame_crc(channel, body)))
    return FRAME_START + channel + encoded + FRAME_END


class FrameParser(object):
    """Takes the frames out of what a console prints; the text between them is dropped."""

    def __init__(self):
        self._raw = b""

    def feed(self, data):
        """The (channel, seq, payload) of each valid frame completed by ``data``."""
        frames = []
        self._raw += data
        while True:
            start = self._raw.find(FRAME_START)
            if start < 0:
                # A trailing ESC may start a frame.
                self._raw = self._raw[-1:] if self._raw.endswith(b"\x1b") else b""
                return frames
            self._raw = self._raw[start:]
            end = self._raw.find(FRAME_END)
            if end < 0:
                if len(self._raw) >= len(FRAME_START) + FRAME_MAX_ENCODED + len(FRAME_END):
                    self._raw = self._raw[len(FRAME_START):]
                    continue
                return frames
            frame = self._decode(self._raw[len(FRAME_START):end])
            if frame is not None:
                frames.append(frame)
            self._raw = self._raw[end + len(FRAME_END):]

    @staticmethod
    def _decode(frame):
        channel = frame[:1]
        try:
            raw = base64.b64decode(frame[1:], validate=True)
        except ValueError:
            return None
        if len(raw) < 5 or struct.unpack("<I", raw[-4:])[0] != frame_crc(channel, raw[:-4]):
            return None
        return channel, raw[0], raw[1:-4]


class ConsoleSession(object):
    """A Pebble protocol session in frames on a console. ``on_data(bytes)`` gets what the
    firmware sends and ``on_closed()`` is called when the session ends on its own."""

    def __init__(self, host, port, on_data, on_closed):
        self.address = (host, port)
        self._on_data = on_data
        self._on_closed = on_closed
        self._socket = None
        self._parser = FrameParser()
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._seq = 0
        self._acked = None
        self._last_seq = None
        self._opened = False
        self.is_open = False

    def open(self):
        """Whether the firmware took the session."""
        try:
            self._socket = socket.create_connection(self.address, timeout=OPEN_TIMEOUT_S)
        except socket.error as e:
            logger.info("no console at %s:%d (%s)", self.address[0], self.address[1], e)
            return False
        self._socket.settimeout(None)
        # Each frame waits for the last one's acknowledgement.
        self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        reader = threading.Thread(target=self._read)
        reader.daemon = True
        reader.start()

        deadline = time.monotonic() + OPEN_TIMEOUT_S
        while time.monotonic() < deadline:
            # Repeats do no harm: seq 0.
            self._write(encode_frame(CHANNEL_PROTOCOL, 0, bytes([OPEN])))
            with self._cond:
                if self._cond.wait_for(lambda: self._opened, OPEN_RETRY_S):
                    self.is_open = True
                    return True
        self.close()
        return False

    def close(self):
        if self.is_open:
            self.is_open = False
            try:
                self._write(encode_frame(CHANNEL_PROTOCOL, 0, bytes([CLOSE])))
            except socket.error:
                pass
        if self._socket is not None:
            try:
                self._socket.close()
            except socket.error:
                pass

    def send(self, data):
        for i in range(0, len(data), FRAME_MTU - 1):
            self._send_frame(bytes([DATA]) + data[i:i + FRAME_MTU - 1])

    def _send_frame(self, payload):
        with self._frame_lock:
            self._seq = self._seq % 255 + 1
            seq = self._seq
            frame = encode_frame(CHANNEL_PROTOCOL, seq, payload)
            deadline = time.monotonic() + SEND_TIMEOUT_S
            with self._cond:
                self._acked = None
            while True:
                self._write(frame)
                with self._cond:
                    if self._cond.wait_for(lambda: self._acked == seq, ACK_TIMEOUT_S):
                        return
                if not self.is_open:
                    raise ConnectionError("the session ended")
                if time.monotonic() > deadline:
                    raise ConnectionError("the firmware does not acknowledge on its console")

    def _write(self, data):
        with self._write_lock:
            self._socket.sendall(data)

    def _read(self):
        while True:
            try:
                data = self._socket.recv(4096)
            except (socket.error, ValueError):
                data = b""
            if not data:
                self._lost()
                return
            for channel, seq, payload in self._parser.feed(data):
                self._handle_frame(channel, seq, payload)

    def _lost(self):
        was_open = self.is_open
        self.is_open = False
        with self._cond:
            self._cond.notify_all()
        if was_open:
            self._on_closed()

    def _handle_frame(self, channel, seq, payload):
        if channel == CHANNEL_ACK:
            if payload:
                with self._cond:
                    self._acked = payload[0]
                    self._cond.notify_all()
            return
        if seq != 0:
            try:
                self._write(encode_frame(CHANNEL_ACK, 0, bytes([seq])))
            except socket.error:
                return
            # A lost acknowledgment repeats the frame.
            if seq == self._last_seq:
                return
            self._last_seq = seq
        if channel != CHANNEL_PROTOCOL or not payload:
            return
        if payload[0] == DATA:
            self._on_data(payload[1:])
        elif payload[0] == OPEN:
            with self._cond:
                self._opened = True
                self._cond.notify_all()
        elif payload[0] == CLOSE and self.is_open:
            self._lost()


class EmulatorTransport(BaseTransport):
    """What :class:`QemuTransport` is, with the Pebble protocol on the console at ``console``
    (host, port) when the firmware takes it there. Packets for the emulator always go to
    the control port, and Pebble protocol packets are read from wherever they come."""

    must_initialise = True

    def __init__(self, host, port, console=None):
        self._qemu = QemuTransport(host, port)
        self._console_address = console
        self._console = None
        self._received = queue.Queue()
        self._buffer = b""
        self._connected = False

    def connect(self):
        self._qemu.connect()
        self._connected = True
        if self._console_address is not None:
            console = ConsoleSession(self._console_address[0], self._console_address[1],
                                     self._feed_console, self._disconnected)
            if console.open():
                self._console = console
                logger.info("Pebble protocol on the console at %s:%d", *self._console_address)
            else:
                logger.info("no session on the console; Pebble protocol on the control port")
        reader = threading.Thread(target=self._read_qemu)
        reader.daemon = True
        reader.start()

    @property
    def connected(self):
        return self._connected and self._qemu.connected

    @property
    def uses_console(self):
        return self._console is not None

    def _disconnected(self):
        self._connected = False
        self._received.put(None)

    def _read_qemu(self):
        while True:
            try:
                self._received.put(self._qemu.read_packet())
            except ConnectionError:
                self._disconnected()
                return

    def _feed_console(self, data):
        self._buffer += data
        while len(self._buffer) >= 4:
            length = struct.unpack("!H", self._buffer[:2])[0] + 4
            if len(self._buffer) < length:
                return
            self._received.put((MessageTargetWatch(), self._buffer[:length]))
            self._buffer = self._buffer[length:]

    def read_packet(self):
        item = self._received.get()
        if item is None:
            # For the readers after this one.
            self._received.put(None)
            raise ConnectionError("Disconnected.")
        return item

    def send_packet(self, message, target=MessageTargetWatch()):
        if self._console is not None and isinstance(target, MessageTargetWatch):
            try:
                self._console.send(message)
            except socket.error as e:
                self._disconnected()
                raise ConnectionError(str(e))
        else:
            self._qemu.send_packet(message, target=target)
