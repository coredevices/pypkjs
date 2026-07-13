
__author__ = 'katharine'

import calendar
import collections
import datetime
import dateutil.parser
from dateutil.tz import tzlocal
import logging
import requests
import struct
import time
import traceback
from uuid import UUID
import urllib.request, urllib.parse, urllib.error

from libpebble2.protocol.appglance import AppGlance, AppGlanceSlice, AppGlanceSliceType
from libpebble2.protocol.appmessage import AppMessage
from libpebble2.protocol.blobdb import BlobDatabaseID, BlobStatus
from libpebble2.protocol.system import Model
from libpebble2.services.blobdb import SyncWrapper
from libpebble2.services.notifications import Notifications
from libpebble2.services.appmessage import *
from libpebble2.util.hardware import PebbleHardware
from libpebble2.util.bundle import PebbleBundle

from . import events
from ..timeline.attributes import TimelineAttributeSet
from .exceptions import JSRuntimeException
from .qjs import from_js

logger = logging.getLogger('pypkjs.javascript.pebble')


class TokenException(Exception):
    pass


class Pebble(events.EventSourceMixin):
    def __init__(self, runtime, pebble):
        # Expose our methods as the JS `Pebble` global.
        runtime.context.expose("Pebble", self,
            ['sendAppMessage', 'showSimpleNotificationOnPebble', 'getAccountToken', 'getWatchToken',
             'addEventListener', 'removeEventListener', 'openURL', 'getTimelineToken', 'timelineSubscribe',
             'timelineUnsubscribe', 'timelineSubscriptions', 'getActiveWatchInfo', 'appGlanceReload',
             'insertTimelinePin', 'deleteTimelinePin'])
        runtime.run_js("Pebble.platform = 'pypkjs';")
        self.blobdb = pebble.blobdb
        self.pebble = pebble.pebble
        self.runtime = runtime
        self.tid = 0
        self.uuid = runtime.pbw.uuid
        self.app_keys = runtime.pbw.manifest['appKeys']
        self.pending_acks = {}
        self.is_ready = False
        self._timeline_token = None
        self._appmessage = self.runtime.runner.appmessage
        self._appmessage_handlers = []
        super(Pebble, self).__init__(runtime)

    def _connect(self):
        self._ready()

    def _ready(self):
        self.is_ready = True
        self._appmessage_handlers = [
            self._appmessage.register_handler("ack", self._handle_ack),
            self._appmessage.register_handler("nack", self._handle_nack),
            self._appmessage.register_handler("appmessage", self._handle_message),
        ]
        self.triggerEvent("ready")

    def _shutdown(self):
        for handle in self._appmessage_handlers:
            self._appmessage.unregister_handler(handle)

    def _configure(self):
        self.triggerEvent("showConfiguration")

    def _handle_ack(self, tid, app_uuid):
        self._handle_response(tid, True)

    def _handle_nack(self, tid, app_uuid):
        self._handle_response(tid, False)

    def _handle_response(self, tid, did_succeed):
        try:
            success, failure = self.pending_acks[tid]
        except KeyError:
            return
        callback_param = {"data": {"transactionId": tid}}
        if did_succeed:
            if callable(success):
                self.runtime.enqueue(success, callback_param)
        else:
            if callable(failure):
                self.runtime.enqueue(failure, callback_param)
        del self.pending_acks[tid]

    def _handle_message(self, tid, uuid, dictionary):
        if uuid != self.uuid:
            logger.warning("Discarded message for %s (expected %s)", uuid, self.uuid)
            self.pebble._send_message("APPLICATION_MESSAGE", struct.pack('<BB', 0x7F, tid))  # ACK
            return

        app_keys = dict(list(zip(list(self.app_keys.values()), list(self.app_keys.keys()))))
        d = {}
        for k, v in dictionary.items():
            if isinstance(v, int):
                value = v
            elif isinstance(v, str):
                value = v
            elif isinstance(v, bytearray):
                value = list(v)
            else:
                raise JSRuntimeException("?????")
            d[str(k)] = value
            if k in app_keys:
                d[str(app_keys[k])] = value
        e = events.Event(self.runtime, "AppMessage", {"payload": d})
        self.triggerEvent("appmessage", e)

    def _check_ready(self):
        if not self.is_ready:
            raise JSRuntimeException("Can't interact with the watch before the ready event is fired.")

    def sendAppMessage(self, message, success=None, failure=None):
        self._check_ready()
        message = from_js(message)
        to_send = {}
        message = {k: message[str(k)] for k in list(message.keys())}
        for k, v in message.items():
            if k in self.app_keys:
                k = self.app_keys[k]
            try:
                to_send[int(k)] = v
            except ValueError:
                raise JSRuntimeException("Unknown message key '%s'" % k)

        d = {}
        appmessage = AppMessage()
        for k, v in to_send.items():
            if isinstance(v, str):
                if not isinstance(v, str):
                    v = v.decode('utf-8')
                v = CString(v)
            elif isinstance(v, int):
                v = Int32(v)
            elif isinstance(v, float):  # thanks, javascript
                try:
                    intv = int(round(v))
                except ValueError:
                    self.runtime.log_output("WARNING: illegal float value %s for appmessage key %s" % (v, k))
                    intv = 0
                v = Int32(intv)
            elif isinstance(v, collections.abc.Sequence):
                b = bytearray()
                for byte in v:
                    if isinstance(byte, int):
                        if 0 <= byte <= 255:
                            b.append(byte)
                        else:
                            raise JSRuntimeException("Bytes must be between 0 and 255 inclusive.")
                    elif isinstance(byte, str):  # This is intentionally not basestring; unicode won't work.
                        b.extend(bytearray(byte))
                    else:
                        raise JSRuntimeException("Unexpected value in byte array.")
                v = ByteArray(bytes(b))
            elif v is None:
                continue
            else:
                raise JSRuntimeException("Invalid value data type for key %s: %s" % (k, type(v)))
            d[k] = v

        tid = self._appmessage.send_message(self.uuid, d)
        self.pending_acks[tid] = (success, failure)

    def showSimpleNotificationOnPebble(self, title, message):
        self._check_ready()
        Notifications(self.pebble, self.blobdb).send_notification(title, message)

    def showNotificationOnPebble(self, opts):
        pass

    def getAccountToken(self):
        self._check_ready()
        return self.runtime.runner.account_token

    def getWatchToken(self):
        self._check_ready()
        return self.runtime.runner.watch_token

    def openURL(self, url):
        self.runtime.open_config_page(url, self._handle_config_response)

    def _get_timeline_token(self):
        if self._timeline_token is not None:
            return self._timeline_token
        # Return a dummy token for local timeline emulation.
        # The original Pebble timeline servers are gone; CoreApp uses the same approach.
        self._timeline_token = "emulated-dummy-token"
        logger.info("Using emulated timeline token for app %s", self.uuid)
        return self._timeline_token

    def getTimelineToken(self, success=None, failure=None):
        def go():
            try:
                token = self._get_timeline_token()
            except (requests.RequestException, TokenException) as e:
                if callable(failure):
                    self.runtime.enqueue(failure, str(e))
            except Exception:
                traceback.print_exc()
                if callable(failure):
                    self.runtime.enqueue(failure, "Internal failure.")
            else:
                if callable(success):
                    self.runtime.enqueue(success, token)
        self.runtime.group.spawn(go)

    def _do_timeline_thing(self, method, topic, success, failure):
        # Local emulation: subscriptions are no-ops
        logger.info("Timeline subscription %s for topic '%s' (emulated locally)", method, topic)
        if callable(success):
            self.runtime.enqueue(success)

    def timelineSubscribe(self, topic, success=None, failure=None):
        self.runtime.group.spawn(self._do_timeline_thing, "POST", topic, success, failure)

    def timelineUnsubscribe(self, topic, success=None, failure=None):
        self.runtime.group.spawn(self._do_timeline_thing, "DELETE", topic, success, failure)

    def timelineSubscriptions(self, success=None, failure=None):
        def go():
            # Local emulation: return empty subscription list
            subs = []
            if callable(success):
                self.runtime.enqueue(success, subs)
        self.runtime.group.spawn(go)

    def insertTimelinePin(self, pin):
        """Insert a timeline pin directly, without going through the timeline web API."""
        import json
        import datetime
        import uuid as uuid_mod

        if isinstance(pin, str):
            pin = json.loads(pin)
        else:
            pin = from_js(pin)

        pin_id = str(pin.get('id', uuid_mod.uuid4()))
        guid = uuid_mod.uuid5(uuid_mod.NAMESPACE_DNS, '%s.pin.developer.getpebble.com' % pin_id)
        now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        pin['guid'] = str(guid)
        pin['createTime'] = now
        pin['updateTime'] = now
        pin['topicKeys'] = pin.get('topicKeys', [])
        pin['source'] = 'sdk'
        pin['dataSource'] = 'sandbox-uuid:%s' % self.uuid

        logger.info("insertTimelinePin: '%s' (guid=%s) for app %s", pin_id, guid, self.uuid)
        self.runtime.runner.timeline.handle_pin_create(pin, manual=True)

    def deleteTimelinePin(self, pin_id):
        """Delete a timeline pin directly by its ID."""
        import uuid as uuid_mod

        guid = uuid_mod.uuid5(uuid_mod.NAMESPACE_DNS, '%s.pin.developer.getpebble.com' % pin_id)
        logger.info("deleteTimelinePin: '%s' (guid=%s)", pin_id, guid)
        try:
            self.runtime.runner.timeline.handle_pin_delete({'guid': str(guid)})
        except Exception:
            pass  # Pin may not exist

    def _infer_installed_platform(self):
        available_prefixes = self.runtime.pbw.prefixes
        valid_prefixes = PebbleBundle.prefixes_for_hardware(self.pebble.watch_info.running.hardware_platform)
        for prefix in valid_prefixes:
            if prefix in available_prefixes:
                if prefix == '':
                    return 'aplite'
                else:
                    return prefix[:-1]
        raise JSRuntimeException("Internal consistency error: This app's prefixes (%s) and the supported "
                                 "prefixes (%s) do not intersect!" % (available_prefixes, valid_prefixes))


    def getActiveWatchInfo(self):
        watch_info = self.pebble.watch_info

        js_object = {}
        platform = self._infer_installed_platform()
        js_object['platform'] = platform
        model = self.pebble.watch_model  # Note: this could take a while.
        model_map = {
            Model.TintinBlack: "pebble_black",
            Model.TintinRed: "pebble_red",
            Model.TintinWhite: "pebble_white",
            Model.TintinGrey: "pebble_gray",
            Model.TintinOrange: "pebble_orange",
            Model.TintinGreen: "pebble_green",
            Model.TintinPink: "pebble_pink",
            Model.TintinBlue: "pebble_blue",
            Model.BiancaBlack: "pebble_steel_black",
            Model.BiancaSilver: "pebble_steel_silver",
            Model.SnowyWhite: "pebble_time_white",
            Model.SnowyRed: "pebble_time_red",
            Model.SnowyBlack: "pebble_time_black",
        }
        model = model_map.get(platform, 'qemu_platform_%s' % platform)
        js_object['model'] = model
        js_object['language'] = watch_info.language
        fw_version = self.pebble.firmware_version
        js_object['firmware'] = {
            'major': fw_version.major,
            'minor': fw_version.minor,
            'patch': fw_version.patch,
            'suffix': fw_version.suffix,
        }
        return js_object

    def _handle_config_response(self, response):
        def go():
            e = events.Event(self.runtime, "WebviewClosed", {"response": response})
            self.triggerEvent("webviewclosed", e)
        self.runtime.enqueue(go)

    def appGlanceReload(self, slices, success, failure):
        slices = from_js(slices) or []
        glance_slices = [AppGlanceSlice(
                    self._time_from_js(x.get('expirationTime', None)),
                    AppGlanceSliceType.IconAndSubtitle,
                    TimelineAttributeSet(dict(x['layout']), self.runtime.runner.timeline, self.uuid).serialise())
                  for x in slices]
        glance = AppGlance(
            version=1,
            creation_time=int(time.time()),
            slices=(glance_slices or [])
        )
        logger.debug("Constructed AppGlance: %s", glance)

        def handle_result(result):
            if result != BlobStatus.Success:
                logger.warning("Glance reload failed: {!s}".format(result))
                if callable(failure):
                    self.runtime.call_js(failure, slices, {"success": False})
            else:
                if callable(success):
                    self.runtime.call_js(success, slices, {"success": True})

        self.blobdb.insert(BlobDatabaseID.AppGlance, self.uuid, glance.serialise(), callback=handle_result)

    def _time_from_js(self, js_time):
        if js_time is None:
            return 0
        elif isinstance(js_time, str):
            dt = dateutil.parser.parse(js_time)
            if dt.tzinfo is None:
                raise JSRuntimeException("Date strings without timezone information are not permitted.")
        elif isinstance(js_time, datetime.datetime):
            dt = js_time.replace(tzinfo=tzlocal())
        else:
            raise JSRuntimeException("Expected Date object or time string, got {}.".format(type(js_time)))
        return calendar.timegm(dt.utctimetuple())
