
__author__ = 'katharine'

import STPyV8 as v8
import time
import requests
import pygeoip
import os.path

Position = lambda runtime, *args: v8.JSObject.create(runtime.context.locals.Position, args)

Coordinates = lambda runtime, *args: v8.JSObject.create(runtime.context.locals.Coordinates, args)


class Geolocation(object):
    def __init__(self, runtime):
        self.runtime = runtime
        self._watchers = {}
        self._watch_counter = 0

        runtime.run_js("""
            Position = (function(coords, timestamp) {
                this.coords = coords;
                this.timestamp = timestamp;
            });
        """)

        runtime.run_js("""
            Coordinates = (function(long, lat, accuracy) {
                this.longitude = long
                this.latitude = lat
                this.accuracy = accuracy
            });
        """)

    def _get_position(self, success, failure):
        latitude = self.runtime.runner.latitude
        longitude = self.runtime.runner.longitude
        if latitude is not None and longitude is not None:
            self.runtime.enqueue(success, Position(self.runtime, Coordinates(self.runtime, longitude, latitude, 1000), round(time.time() * 1000)))
            return
        try:
            resp = requests.get('https://api.ipify.org')
            resp.raise_for_status()
            ip = resp.text
            gi = pygeoip.GeoIP('%s/GeoLiteCity.dat' % os.path.dirname(__file__))
            record = gi.record_by_addr(ip)
            if record is None:
                if callable(failure):
                    self.runtime.enqueue(failure)
        except (requests.RequestException, pygeoip.GeoIPError):
            if callable(failure):
                self.runtime.enqueue(failure)
        else:
            self.runtime.enqueue(success, Position(self.runtime, Coordinates(self.runtime, record['longitude'], record['latitude'], 1000), round(time.time() * 1000)))

    def _enabled(self):
        return True

    def getCurrentPosition(self, success, failure=None, options=None):
        self.runtime.group.spawn(self._get_position, success, failure)

    def watchPosition(self, success, failure=None, options=None):
        self._watch_counter += 1
        watch_id = self._watch_counter
        self._watchers[watch_id] = (success, failure)
        self.runtime.group.spawn(self._get_position, success, failure)
        return watch_id

    def clearWatch(self, watch_id):
        self._watchers.pop(watch_id, None)

    def notify_watchers(self):
        for success, failure in tuple(self._watchers.values()):
            self.runtime.group.spawn(self._get_position, success, failure)
