
__author__ = 'katharine'

from gevent import monkey; monkey.patch_all()
from gevent import GreenletExit
import struct
import websocket

from .exceptions import JSRuntimeException
from .qjs import from_js

# As with XHR, the reactive WebSocket object lives in JS. Its constructor hands the
# Python worker four callbacks (open/message/close/error) and gets back an integer
# id; send/close are addressed to that id. The Python side owns the socket + recv
# greenlet and invokes the callbacks; the JS side owns readyState, handlers, events.
_WS_JS = """
(function() {
    function WebSocket(url, protocols) {
        var self = this;
        this.url = url;
        this.readyState = 0;
        this.bufferedAmount = 0;
        this.onopen = null; this.onerror = null; this.onclose = null; this.onmessage = null;
        this.extensions = ''; this.protocol = null; this.binaryType = 'arraybuffer';
        this._listeners = {};
        this._id = __ws.connect(url, protocols || null,
            function(proto) { self.protocol = proto; self.readyState = 1; self._fire('open'); },
            function(data, binary) {
                if (self.readyState !== 1) return;
                if (binary) { data = (new Uint8Array(data)).buffer; }
                self._fire('message', { type: 'message', data: data, origin: url });
            },
            function(code, reason, wasClean) {
                self.readyState = 3;
                self._fire('close', { type: 'close', code: code, reason: reason, wasClean: wasClean });
            },
            function() { self.readyState = 3; self._fire('error', { type: 'error' }); }
        );
    }
    WebSocket.CONNECTING = 0; WebSocket.OPEN = 1; WebSocket.CLOSING = 2; WebSocket.CLOSED = 3;

    WebSocket.prototype._fire = function(type, ev) {
        ev = ev || { type: type };
        var handler = this['on' + type];
        if (handler) { handler.call(this, ev); }
        var ls = this._listeners[type] || [];
        for (var i = 0; i < ls.length; i++) { ls[i].call(this, ev); }
    };
    WebSocket.prototype.addEventListener = function(type, fn) {
        (this._listeners[type] = this._listeners[type] || []).push(fn);
    };
    WebSocket.prototype.removeEventListener = function(type, fn) {
        var ls = this._listeners[type];
        if (!ls) return;
        this._listeners[type] = ls.filter(function(f) { return f !== fn; });
    };
    WebSocket.prototype.send = function(data) {
        if (typeof data === 'string') { __ws.send(this._id, data, false); return; }
        var view;
        if (data instanceof ArrayBuffer) {
            view = new Uint8Array(data);
        } else if (ArrayBuffer.isView(data)) {
            // Respect the view's window; new Uint8Array(data.buffer) would send
            // the whole backing buffer.
            view = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
        } else {
            throw new Error('Unsupported data - expect String, ArrayBuffer, TypedArray, or DataView.');
        }
        __ws.send(this._id, Array.prototype.slice.call(view), true);
    };
    WebSocket.prototype.close = function(code, reason) {
        __ws.close(this._id, code || 1000, reason || '');
    };
    window.WebSocket = WebSocket;
})();
"""


class WSConn(object):
    CONNECTING, OPEN, CLOSING, CLOSED = 0, 1, 2, 3

    def __init__(self, runtime, url, protocols, on_open, on_message, on_close, on_error):
        self.runtime = runtime
        self.url = url
        if protocols is None:
            self.protocols = None
        elif isinstance(protocols, list):
            self.protocols = protocols
        else:
            self.protocols = [protocols]
        self.on_open, self.on_message = on_open, on_message
        self.on_close, self.on_error = on_close, on_error
        self.ws = None
        self.readyState = self.CONNECTING
        self.runtime.group.spawn(self._handle)

    def _handle(self):
        try:
            self.ws = websocket.create_connection(self.url, subprotocols=self.protocols)
        except (websocket.WebSocketException, OSError):
            self.readyState = self.CLOSED
            self.runtime.enqueue(self.on_error)
            self.runtime.enqueue(self.on_close, 1006, "Connection failed.", False)
            return
        self.readyState = self.OPEN
        self.runtime.enqueue(self.on_open, self.ws.subprotocol or "")
        try:
            while self.ws.connected:
                opcode, data = self.ws.recv_data()
                if opcode == websocket.ABNF.OPCODE_TEXT:
                    self.runtime.enqueue(self.on_message, data.decode('utf-8') if isinstance(data, bytes) else data, False)
                elif opcode == websocket.ABNF.OPCODE_BINARY:
                    self.runtime.enqueue(self.on_message, list(bytearray(data)), True)
                elif opcode == websocket.ABNF.OPCODE_CLOSE:
                    self.readyState = self.CLOSED
                    if len(data) >= 2:
                        close_code, = struct.unpack_from("!H", data, 0)
                        self.runtime.enqueue(self.on_close, close_code, data[2:].decode('utf-8', 'replace'), True)
                    else:
                        self.runtime.enqueue(self.on_close, 1000, "", True)
        except GreenletExit:
            if self.ws is not None and self.ws.connected:
                self.ws.close()
            raise
        except (websocket.WebSocketException, OSError):
            # Abrupt disconnect (peer died, network drop): without this the reader
            # greenlet dies silently and JS stays at readyState 1 forever.
            self.readyState = self.CLOSED
            self.runtime.enqueue(self.on_error)
            self.runtime.enqueue(self.on_close, 1006, "Connection closed abnormally.", False)

    def send(self, data, binary):
        if self.readyState != self.OPEN:
            raise JSRuntimeException("Websocket is not open.")
        if binary:
            self.ws.send_binary(bytes(bytearray(data)))
        else:
            self.ws.send(data)

    def close(self, code, reason):
        if self.readyState != self.OPEN:
            return
        self.readyState = self.CLOSING
        self.ws.send_close(code, reason.encode('utf-8') if isinstance(reason, str) else reason)


class WSWorker(object):
    def __init__(self, runtime):
        self.runtime = runtime
        self.conns = {}
        self.counter = 0

    def connect(self, url, protocols, on_open, on_message, on_close, on_error):
        self.counter += 1
        conn_id = self.counter
        self.conns[conn_id] = WSConn(self.runtime, url, from_js(protocols),
                                     on_open, on_message, on_close, on_error)
        return conn_id

    def send(self, conn_id, data, binary):
        self.conns[conn_id].send(from_js(data), binary)

    def close(self, conn_id, code, reason):
        conn = self.conns.get(conn_id)
        if conn is not None:
            conn.close(code, reason)


class WSExtension:
    def __init__(self, runtime):
        runtime.context.expose("__ws", WSWorker(runtime), ['connect', 'send', 'close'])
        runtime.run_js(_WS_JS)


def prepare_ws(runtime):
    # WebSocket is fully set up in WSExtension.__init__; nothing deferred.
    pass
