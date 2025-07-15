
__author__ = 'katharine'

from gevent import monkey; monkey.patch_all()
from gevent import GreenletExit
import struct
import websocket

import STPyV8 as v8
from .exceptions import JSRuntimeException
from . import events

CloseEvent = lambda runtime, *args: v8.JSObject.create(runtime.context.locals.CloseEvent, args)
MessageEvent = lambda runtime, *args: v8.JSObject.create(runtime.context.locals.MessageEvent, args)

class WSExtension:
    def __init__(self, runtime):
        runtime.run_js("""
            CloseEvent = function(eventInitDict) {
                Event.call(this, "close", eventInitDict);
                var wasClean = eventInitDict.wasClean;
                var code = eventInitDict.code;
                var reason = eventInitDict.reason;
                Object.defineProperties(this, {
                    wasClean: {
                        get: function() { return wasClean; },
                        enumerable: true,
                    },
                    code: {
                        get: function() { return code; },
                        enumerable: true,
                    },
                    reason: {
                        get: function() { return reason; },
                        enumerable: true,
                    },
                });
            };
            CloseEvent.prototype = Object.create(Event.prototype);
            CloseEvent.prototype.constructor = CloseEvent;
            MessageEvent = function(origin, data, eventInitDict) {
                Event.call(this, "message", eventInitDict);
                this.data = data;
                this.origin = origin;
            };
            MessageEvent.prototype = Object.create(Event.prototype);
            MessageEvent.prototype.constructor = CloseEvent;
        """)

        runtime.run_js("""
            _init_websocket = function(runtime, session) {
                var _ws = exec('__get_ws', []);
                this.WebSocket = function(url, protocols) {
                    var origin = new _ws(runtime, url, protocols);
                    _make_proxies(this, origin, ['close', 'send']);
                    _make_properties(this, origin, ['readyState', 'bufferedAmount', 'onopen', 'onerror', 'onclose', 'onmessage',
                                                    'extensions', 'protocol', 'binaryType']);
                };
                this.WebSocket.CONNECTING = 0;
                this.WebSocket.OPEN = 1;
                this.WebSocket.CLOSING = 2;
                this.WebSocket.CLOSED = 3;
            }
        """)

class WebSocket(events.EventSourceMixin):
    CONNECTING = 0
    OPEN = 1
    CLOSING = 2
    CLOSED = 3

    def __init__(self, runtime, url, protocols=None):
        super(WebSocket, self).__init__(runtime)

        self.runtime = runtime
        self.url = url
        if protocols is None or protocols == v8.JSNull:
            self.protocols = None
        else:
            # In JS, websocket protocol argument can be either a string or list
            if isinstance(protocols, v8.JSArray):
                self.protocols = list(protocols)
            else:
                self.protocols = [protocols]

        self.runtime.group.spawn(self.handle_ws)
        self.ws = None

        # JS properties
        self.readyState = self.CONNECTING
        self.bufferedAmount = 0
        self.onopen = None
        self.onerror = None
        self.onclose = None
        self.onmessage = None
        self.extensions = ''
        self.protocol = None
        self.binaryType = 'arraybuffer'

    def close(self, code=1000, reason=b"", *args):
        if self.readyState != self.OPEN:
            return
        self.readyState = self.CLOSING
        self.ws.send_close(code, reason)

    def send(self, data, *args):
        if self.readyState != self.OPEN:
            raise JSRuntimeException("Websocket is not open.")
        if isinstance(data, str):
            self.ws.send(data)
            return

        array_buffer = self.runtime.context.locals.ArrayBuffer
        uint8_array = self.runtime.context.locals.Uint8Array

        if array_buffer.isView(data):
            data_array = uint8_array.create(uint8_array, (data.buffer, data.byteOffset, data.byteLength))
            self.ws.send_binary(bytes(data_array[str(x)] for x in range(data_array.length)))
        elif str(data) == '[object ArrayBuffer]':
            data_array = uint8_array.create(uint8_array, (data,))
            self.ws.send_binary(bytes(data_array[str(x)] for x in range(data_array.length)))

    def handle_ws(self):
        try:
            self.ws = websocket.create_connection(self.url, subprotocols=self.protocols)
        except websocket.WebSocketException:
            self.handle_error(1006, "Connection failed.")
            return
        self.protocol = self.ws.subprotocol
        self.readyState = self.OPEN
        self.triggerEvent("open")
        try:
            while self.ws.connected:
                opcode, data = self.ws.recv_data()
                if opcode == websocket.ABNF.OPCODE_TEXT:
                    self.handle_text(data)
                elif opcode == websocket.ABNF.OPCODE_BINARY:
                    self.handle_binary(data)
                elif opcode == websocket.ABNF.OPCODE_CLOSE:
                    # this is annoying.
                    if len(data) >= 2:
                        close_code, = struct.unpack_from("!H", data, 0)
                        reason = data[2:]
                        self.handle_closed(close_code, reason)
                    else:
                        self.handle_closed()
                else:
                    continue
        except GreenletExit:
            if self.ws is not None and self.ws.connected:
                self.ws.close()
            raise

    def handle_text(self, data):
        def go():
            if self.readyState != self.OPEN:
                return
            self.triggerEvent("message", MessageEvent(self.runtime, self.url, data))
        self.runtime.enqueue(go)

    def handle_binary(self, data):
        def go():
            if self.readyState != self.OPEN:
                return
            if self.binaryType == "arraybuffer":
                uint8_array = self.runtime.context.locals.Uint8Array
                buffer = uint8_array.create(uint8_array, (v8.JSArray(list(bytearray(data))),)).buffer
                self.triggerEvent("message", MessageEvent(self.runtime, self.url, buffer))
        self.runtime.enqueue(go)

    def handle_error(self, code, reason):
        def go():
            self.readyState = self.CLOSED
            self.triggerEvent("error")
            self.triggerEvent("close", CloseEvent(self.runtime, {'wasClean': False, code: code, reason: reason}))
        self.runtime.enqueue(go)

    def handle_closed(self, code=1000, reason=""):
        def go():
            self.readyState = self.CLOSED
            self.triggerEvent("close", CloseEvent(self.runtime, {'wasClean': True, code: code, reason: reason}))
        self.runtime.enqueue(go)


def prepare_ws(runtime):
    runtime.register_syscall('__get_ws', lambda : WebSocket)
    return runtime.context.locals._init_websocket(runtime)
