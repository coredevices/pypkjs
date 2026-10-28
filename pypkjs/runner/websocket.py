
__author__ = 'katharine'

from gevent import monkey
monkey.patch_all()

import gevent
from gevent import pywsgi
from geventwebsocket import WebSocketError
from geventwebsocket.handler import WebSocketHandler
import argparse
import json
import logging
import os
import ssl
import struct
import sys
import tempfile
import traceback
from uuid import UUID

from libpebble2.communication.transports.qemu import MessageTargetQemu
from libpebble2.services.install import AppInstaller

from . import Runner
from ..microphone.permissions import DENY, GRANT, WATCH_MICROPHONE
from ..version import __version__


def must_auth(f):
    def g(self, ws, *args, **kwargs):
        if not self.requires_auth or ws.authed:
            f(self, ws, *args, **kwargs)
    return g


class Websocket(object):
    def __init__(self, ws):
        self.ws = ws
        self.authed = False

    def send(self, message):
        self.ws.send(message)

    def receive(self):
        return self.ws.receive()

    def close(self):
        self.ws.close()


class WebsocketRunner(Runner):
    def __init__(self, qemu, pbws, port, token=None, ssl_root=None, persist_dir=None, oauth_token=None,
                 layout_file=None, block_private_addresses=False, plugin_fixtures=None):
        self.port = port
        self.token = token
        self.requires_auth = (token is not None)
        self.authed = not self.requires_auth
        self.server = None
        self.websockets = []
        self.ssl_root = ssl_root
        self.config_callback = None
        super(WebsocketRunner, self).__init__(qemu, pbws, persist_dir=persist_dir, oauth_token=oauth_token,
                                              layout_file=layout_file, block_private_addresses=block_private_addresses,
                                              plugin_fixtures=plugin_fixtures)

    def run(self):
        pebble_greenlet = self.pebble.connect()
        self.pebble.pebble.register_raw_inbound_handler(self._handle_inbound)
        self.pebble.pebble.register_raw_outbound_handler(self._handle_outbound)
        # continuous_sync() deliberately NOT enabled -- the remote server is dead.
        # do_maintenance() handles sending pending pins and cleanup.
        self.timeline.do_maintenance()
        logging.getLogger().addHandler(WebsocketLogHandler(self, level=logging.WARNING))
        if self.ssl_root is not None:
            ssl_args = {
                'keyfile': '%s/server-key.pem' % self.ssl_root,
                'certfile': '%s/server-cert.pem' % self.ssl_root,
                'ca_certs': '%s/ca-cert.pem' % self.ssl_root,
                'ssl_version': ssl.PROTOCOL_TLSv1,
            }
        else:
            ssl_args = {}
        self.server = pywsgi.WSGIServer(("", self.port), self.handle_ws, handler_class=WebSocketHandler, **ssl_args)
        gevent.spawn(self.server.serve_forever)
        pebble_greenlet.join()
        self.server.close()
        self.pebble.disconnect()

    def log_output(self, message):
        self.broadcast(b'\x02' + message.encode('utf-8'))

    def config_page_url(self, uuid):
        if uuid is None or not self.config_pages.has_page(uuid):
            return None
        return 'http://localhost:%d%s' % (self.port, self.config_pages.path_for(uuid))

    def open_config_page(self, url, callback):
        if url.startswith('data:') and self.running_uuid is not None:
            # Served from here, so the page gets the Pebble.sendMessage bridge.
            self.config_pages.open_data_page(self.running_uuid, url)
            url = self.config_page_url(self.running_uuid)
        encoded = url.encode('utf-8')
        self.broadcast(struct.pack('>BBI%ds' % len(encoded), 0x0a, 0x01, len(encoded), encoded))
        self.config_callback = callback

    def _handle_outbound(self, message):
        self.broadcast(b'\x01' + message)

    def _handle_inbound(self, message):
        self.broadcast(b'\x00' + message)

    def handle_ws(self, environ, start_response):
        if environ['PATH_INFO'].startswith('/config/'):
            return self.config_pages.handle(environ, start_response)
        if environ['PATH_INFO'] == '/':
            ws = Websocket(environ['wsgi.websocket'])
            self.websockets.append(ws)
            self.on_open(ws)
            while True:
                try:
                    self.on_message(ws, ws.receive())
                except WebSocketError:
                    break
            self.on_close(ws)
            return []
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'Not found.']

    def on_open(self, ws, *args, **kwargs):
        ws.authed = not self.requires_auth

    def on_close(self, ws):
        if ws in self.websockets:
            self.websockets.remove(ws)

    def broadcast(self, message):
        to_remove = []
        for i, ws in enumerate(self.websockets):
            if ws.authed:
                try:
                    ws.send(message)
                except (WebSocketError, ssl.SSLError):
                    to_remove.append(i)
        for i in reversed(to_remove):
            del self.websockets[i]

    # https://pebbletechnology.atlassian.net/wiki/pages/viewpage.action?pageId=491742
    def on_message(self, ws, message):
        if not isinstance(message, bytearray):
            self.logger.debug("not a bytearray")
            self.logger.debug("received: %s", message)
            return
        opcode = message[0]
        opcode_handlers = {
            0x01: self.do_relay,
            0x04: self.do_install,
            0x06: self.do_phone_info,
            0x09: self.do_auth,
            0x0a: self.do_config_ws,
            0x0b: self.do_qemu_command,
            0x0c: self.do_timeline_command,
            0x0d: self.do_plugin_command,
            0x0e: self.do_location_command,
            0x0f: self.do_permission_command,
        }

        if opcode in opcode_handlers:
            opcode_handlers[opcode](ws, message[1:])

    @must_auth
    def do_phone_info(self, ws, message):
        try:
            ws.send(bytearray(b"\x06pypkjs,0.0.0,qemu"))
        except WebSocketError:
            pass

    def do_auth(self, ws, message):
        length = message[0]
        token, = struct.unpack_from('<%ds' % length, message, 1)
        if token == self.token:
            ws.authed = True
            ws.send(bytearray([0x09, 0x00]))  # token okay
            ws.send(bytearray([0x08, 0xff]))  # send "connected" response immediately; we're always connected.
        else:
            ws.send(bytearray([0x09, 0x01]))  # bad token

    @must_auth
    def do_relay(self, ws, message):
        self.pebble.pebble.send_raw(bytes(message))

    @must_auth
    def do_install(self, ws, message):
        def go_do_install():
            with tempfile.NamedTemporaryFile() as f:
                f.write(message)
                f.flush()
                try:
                    uuid = self.load_pbws([f.name], cache=True)[0]
                    if not self.is_plugin_only(self.pbws[uuid]):
                        AppInstaller(self.pebble.pebble, f.name,
                                     blobdb_client=self.microphone.permissions.blobdb_for_install(
                                         self.pebble.blobdb)).install()
                except:
                    try:
                        ws.send(bytearray([0x05, 0x00, 0x00, 0x00, 0x01]))
                    except WebSocketError:
                        pass
                    raise
                else:
                    try:
                        ws.send(bytearray([0x05, 0x00, 0x00, 0x00, 0x00]))
                    except WebSocketError:
                        pass
        gevent.spawn(go_do_install)

    @must_auth
    def do_config_ws(self, ws, message):
        if message[0] == 0x01:
            self.do_config()
            return
        if self.config_callback is None:
            return
        if message[0] == 0x02:
            length, = struct.unpack_from(">I", message, 1)
            result, = struct.unpack_from(">%ds" % length, message, 5)
            self.config_callback(result.decode('utf-8', 'replace'))
            self.config_callback = None
        elif message[0] == 0x03:
            self.config_callback("")
            self.config_callback = None

    @must_auth
    def do_qemu_command(self, ws, message):
        protocol = message[0]
        self.pebble.pebble.transport.send_packet(bytes(message[1:]),
                                                 target=MessageTargetQemu(protocol=protocol, raw=True))

    @must_auth
    def do_timeline_command(self, ws, message):
        command = message[0]
        message = bytes(message[1:]).decode('utf-8')
        try:
            if command == 0x01:
                try:
                    pin = json.loads(message)
                except ValueError:
                    ws.send(bytearray([0x0c, 0x01]))
                    return
                self.timeline.handle_pin_create(pin, manual=True)
                ws.send(bytearray([0x0c, 0x00]))
            elif command == 0x02:
                self.timeline.handle_pin_delete({'guid': message})
        except Exception as e:
            traceback.print_exc()
            self.log_output("Pin insert failed: %s: %s" % (type(e).__name__, e))
            ws.send(bytearray([0x0c, 0x01]))


    @must_auth
    def do_plugin_command(self, ws, message):
        """Replies 0x0d 0x00 (+ JSON, if the command has a result) on success, and 0x0d 0x01 + an error
        message on failure.

        0x01 + JSON {"source": "<category>/<item>", "instances": [...] or null}: replace a built-in
             source's readings (null restores its default).
        0x02: list the registered plugins.
        0x03 + uuid: remove a plugin-only pbw, from the app cache too."""
        command = message[0]
        try:
            body = bytes(message[1:]).decode('utf-8')
            if command == 0x01:
                request = json.loads(body)
                self.plugin_fixtures.set(request['source'], request.get('instances'))
                result = None
            elif command == 0x02:
                result = self.plugin_list()
            elif command == 0x03:
                result = {'removed': self.remove_plugin(body.strip())}
            else:
                raise ValueError("unknown plugin command %d" % command)
        except (ValueError, KeyError, TypeError) as e:
            self.log_output("Plugin command failed: %s" % e)
            ws.send(bytearray([0x0d, 0x01]) + str(e).encode('utf-8'))
        else:
            ws.send(bytearray([0x0d, 0x00]) + (json.dumps(result).encode('utf-8') if result is not None else b''))

    @must_auth
    def do_location_command(self, ws, message):
        """JSON {"latitude": float, "longitude": float}, or null to go back to an IP lookup: where
        geolocation puts the phone. Replies 0x0e 0x00, or 0x0e 0x01 + an error message."""
        try:
            self.set_location(json.loads(bytes(message).decode('utf-8')))
        except (ValueError, KeyError, TypeError) as e:
            ws.send(bytearray([0x0e, 0x01]) + str(e).encode('utf-8'))
        else:
            ws.send(bytearray([0x0e, 0x00]))


    @must_auth
    def do_permission_command(self, ws, message):
        """JSON {"permission": "WatchMicrophone", "uuid": optional, defaulting to the running app,
        "state": "grant", "deny", "reset" or absent to only ask}: what the app may use, as a user's
        answer in the phone app. Replies 0x0f 0x00 + JSON {"uuid", "name", "granted", "reason"},
        or 0x0f 0x01 + an error message."""
        try:
            request = json.loads(bytes(message).decode('utf-8'))
            if request.get('permission') != WATCH_MICROPHONE:
                raise ValueError("unknown permission %r; pypkjs knows %s" % (request.get('permission'),
                                                                         WATCH_MICROPHONE))
            uuid = request.get('uuid')
            if uuid is not None:
                uuid = UUID(uuid)
            elif self.running_app_uuid is not None:
                uuid = self.running_app_uuid
            else:
                raise ValueError("no app is running; name one with --uuid")
            state = request.get('state')
            permissions = self.microphone.permissions
            if state is None:
                granted, reason = permissions.decision(uuid)
            elif state in (GRANT, DENY, 'reset'):
                granted, reason = permissions.set_override(uuid, None if state == 'reset' else state)
            else:
                raise ValueError("unknown state %r" % state)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            ws.send(bytearray([0x0f, 0x01]) + str(e).encode('utf-8'))
        else:
            result = {'uuid': str(uuid), 'name': self.app_title(uuid), 'granted': granted, 'reason': reason}
            ws.send(bytearray([0x0f, 0x00]) + json.dumps(result).encode('utf-8'))


class WebsocketLogHandler(logging.Handler):
    def __init__(self, ws_runner, *args, **kwargs):
        self.ws_runner = ws_runner
        logging.Handler.__init__(self, *args, **kwargs)
        self.setFormatter(logging.Formatter("[PHONESIM] [%(levelname)s] %(message)s"))

    def emit(self, record):
        self.ws_runner.log_output(self.format(record))

def run_tool():
    try:
        import urllib3.contrib.pyopenssl
        urllib3.contrib.pyopenssl.inject_into_urllib3()
    except ImportError:
        pass
    version_string = "pypkjs v{}".format(__version__)
    parser = argparse.ArgumentParser(description='Emulate a phone running the Pebble app')
    parser.add_argument('--version', action='version', version=version_string)
    parser.add_argument('--qemu', default='127.0.0.1:12344', help="Location of qemu bluetooth socket")
    parser.add_argument('--port', default=9000, type=int, help="Port for phone websocket to listen on")
    parser.add_argument('--token', help="Token for the websocket to require as authentication")
    parser.add_argument('--ssl-root', help="Root for SSL certs, containing server-cert.pem and server-key.pem")
    parser.add_argument('--persist', help="Directory in which to read and write persistent data (localStorage, timeline)")
    parser.add_argument('--oauth', default=None, help="Pebble OAuth token.")
    parser.add_argument('--layout', default=None, help="Path to a firmware layout.json file on disk.")
    parser.add_argument('--debug', action='store_true', help="Very, very verbose debug spew.")
    parser.add_argument('--block-private-addresses', action='store_true', help="Disable access to private IPs.")
    parser.add_argument('--plugin-fixtures', default=os.environ.get('PYPKJS_PLUGIN_FIXTURES'),
                        help="JSON file of readings for the built-in plugins, reloaded when it changes "
                             "(default: $PYPKJS_PLUGIN_FIXTURES).")
    parser.add_argument('pbws', nargs='*', help="Set of pbws.")
    args = parser.parse_args(sys.argv[1:])
    logging.basicConfig()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)
    runner = WebsocketRunner(args.qemu,args.pbws, args.port, token=args.token, ssl_root=args.ssl_root,
                             persist_dir=args.persist, oauth_token=args.oauth, layout_file=args.layout,
                             block_private_addresses=args.block_private_addresses,
                             plugin_fixtures=args.plugin_fixtures)
    runner.run()
    # The quickjs-ng extension deadlocks against gevent's hub during interpreter
    # shutdown (see pypkjs/javascript/qjs.py), so shut the JS runtime down
    # explicitly -- flushing localStorage -- and then exit without the normal
    # interpreter teardown.
    runner.stop_js(wait=5)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
