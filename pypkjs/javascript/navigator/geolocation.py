
__author__ = 'katharine'

import time
import requests
import pygeoip
import os.path


class Geolocation(object):
    def __init__(self, runtime):
        self.runtime = runtime

    def _get_position(self, success, failure):
        fixed = getattr(self.runtime.runner, 'location', None)
        if fixed is not None:
            self.runtime.enqueue(success, self._position(fixed['latitude'], fixed['longitude'], 10))
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
                return
        except (requests.RequestException, pygeoip.GeoIPError):
            if callable(failure):
                self.runtime.enqueue(failure)
        else:
            self.runtime.enqueue(success, self._position(record['latitude'], record['longitude'], 1000))

    @staticmethod
    def _position(latitude, longitude, accuracy):
        return {
            'coords': {
                'longitude': longitude,
                'latitude': latitude,
                'accuracy': accuracy,
            },
            'timestamp': round(time.time() * 1000),
        }

    def _enabled(self):
        return True

    def getCurrentPosition(self, success, failure=None, options=None):
        self.runtime.group.spawn(self._get_position, success, failure)

    def watchPosition(self, success, failure=None, options=None):
        self.runtime.group.spawn(self._get_position, success, failure)
        return 42

    def clearWatch(self, thing):
        pass
