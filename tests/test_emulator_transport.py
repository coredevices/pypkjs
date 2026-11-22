"""The Pebble protocol on an emulator's console, and on its control port for firmwares without it
(pypkjs.runner.emulator_transport)."""
import importlib.util
import os
import socket
import struct
import threading
import time

import pytest
from libpebble2.communication.transports import MessageTargetWatch
from libpebble2.communication.transports.qemu import MessageTargetQemu
from libpebble2.communication.transports.qemu.protocol import QemuBluetoothConnection, QemuPacket, QemuSPP

# Loaded by path: importing the pypkjs package monkey-patches the interpreter for gevent.
_spec = importlib.util.spec_from_file_location(
    'emulator_transport', os.path.join(os.path.dirname(__file__), '..', 'pypkjs', 'runner', 'emulator_transport.py'))
et = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(et)

PING = struct.pack('!HH', 5, 2001) + b'\x00\x00\x00\x00\x01'


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


class Server(object):
    """A port of the emulator, taking one client."""

    def __init__(self):
        self._listener = socket.socket()
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen(1)
        self.address = self._listener.getsockname()
        self.client = None
        self.received = b''
        self._cond = threading.Condition()
        thread = threading.Thread(target=self._run)
        thread.daemon = True
        thread.start()

    def _run(self):
        try:
            self.client, _ = self._listener.accept()
        except OSError:
            return
        while True:
            try:
                data = self.client.recv(4096)
            except OSError:
                data = b''
            if not data:
                return
            self.handle(data)
            with self._cond:
                self.received += data
                self._cond.notify_all()

    def handle(self, data):
        pass

    def wait_for(self, predicate):
        with self._cond:
            assert self._cond.wait_for(lambda: predicate(self.received), 5)

    def close(self):
        self._listener.close()
        if self.client is not None:
            self.client.close()


class Console(Server):
    """The console of a firmware that takes sessions on it: it acknowledges frames, answers
    the opening of a session and collects the session's data."""

    def __init__(self, answers=True):
        self.answers = answers
        self.parser = et.FrameParser()
        self.data = b''
        self.acks = []
        self.opened = False
        self.last_seq = None
        super(Console, self).__init__()

    def handle(self, data):
        if not self.answers:
            return
        for channel, seq, payload in self.parser.feed(data):
            if channel == et.CHANNEL_ACK:
                self.acks.append(payload[0])
                continue
            if seq != 0:
                self.client.sendall(et.encode_frame(et.CHANNEL_ACK, 0, bytes([seq])))
                if seq == self.last_seq:
                    continue
                self.last_seq = seq
            if payload[0] == et.OPEN:
                self.opened = True
                self.client.sendall(b'I boot> text ' + et.encode_frame(et.CHANNEL_PROTOCOL, 0, bytes([et.OPEN])))
            elif payload[0] == et.DATA:
                self.data += payload[1:]

    def send(self, seq, data):
        self.client.sendall(et.encode_frame(et.CHANNEL_PROTOCOL, seq, bytes([et.DATA]) + data))


@pytest.fixture
def ports(monkeypatch):
    monkeypatch.setattr(et, 'OPEN_TIMEOUT_S', 0.5)
    monkeypatch.setattr(et, 'OPEN_RETRY_S', 0.1)
    servers = []

    def make(cls, *args, **kwargs):
        servers.append(cls(*args, **kwargs))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def test_parser_takes_frames_out_of_text():
    parser = et.FrameParser()
    frame = et.encode_frame(b'P', 3, b'abc')
    assert parser.feed(b'log line\r\n' + frame[:7]) == []
    assert parser.feed(frame[7:] + b'pebble> ') == [(b'P', 3, b'abc')]


def test_parser_drops_corrupt_frames():
    parser = et.FrameParser()
    frame = bytearray(et.encode_frame(b'P', 3, b'abcdef'))
    frame[5] ^= 0x01
    assert parser.feed(bytes(frame) + et.encode_frame(b'P', 4, b'x')) == [(b'P', 4, b'x')]


def test_protocol_goes_on_the_console(ports):
    qemu, console = ports(Server), ports(Console)
    transport = et.EmulatorTransport(*qemu.address, console=console.address)
    transport.connect()
    assert transport.uses_console and console.opened

    message = PING * 200
    transport.send_packet(message)
    assert console.data == message
    assert qemu.received == b''

    console.send(1, PING[:3])
    console.send(1, PING[:3])
    console.send(2, PING[3:])
    target, packet = transport.read_packet()
    assert isinstance(target, MessageTargetWatch) and packet == PING
    # The repeat is acknowledged again, and taken once.
    wait_until(lambda: console.acks == [1, 1, 2])


def test_emulator_packets_go_on_the_control_port(ports):
    qemu, console = ports(Server), ports(Console)
    transport = et.EmulatorTransport(*qemu.address, console=console.address)
    transport.connect()

    packet = QemuBluetoothConnection(connected=True)
    transport.send_packet(packet, target=MessageTargetQemu())
    expected = QemuPacket(data=packet).serialise()
    qemu.wait_for(lambda received: received == expected)

    # A firmware with both still sends on the control port what it started there.
    qemu.client.sendall(QemuPacket(data=QemuSPP(payload=PING)).serialise())
    target, received = transport.read_packet()
    assert isinstance(target, MessageTargetWatch) and received == PING


@pytest.mark.parametrize('console_kind', ['silent', 'absent', 'unset'])
def test_firmware_without_console_sessions_uses_the_control_port(ports, console_kind):
    qemu = ports(Server)
    if console_kind == 'silent':
        address = ports(Console, answers=False).address
    elif console_kind == 'absent':
        closed = socket.socket()
        closed.bind(('127.0.0.1', 0))
        address = closed.getsockname()
        closed.close()
    else:
        address = None
    transport = et.EmulatorTransport(*qemu.address, console=address)
    transport.connect()
    assert not transport.uses_console

    transport.send_packet(PING)
    expected = QemuPacket(data=QemuSPP(payload=PING)).serialise()
    qemu.wait_for(lambda received: received == expected)


def test_read_fails_once_the_emulator_is_gone(ports):
    from libpebble2.exceptions import ConnectionError

    qemu, console = ports(Server), ports(Console)
    transport = et.EmulatorTransport(*qemu.address, console=console.address)
    transport.connect()
    wait_until(lambda: qemu.client is not None)
    qemu.client.close()
    with pytest.raises(ConnectionError):
        transport.read_packet()
    assert not transport.connected
