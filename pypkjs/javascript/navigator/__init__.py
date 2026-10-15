
__author__ = 'katharine'

from .geolocation import Geolocation


class Navigator(object):
    def __init__(self, runtime):
        self._runtime = runtime
        self._geolocation = Geolocation(runtime)

        # geolocation.* methods delegate to the Python Geolocation object.
        runtime.context.expose("__navigator_geolocation", self._geolocation,
                               ['getCurrentPosition', 'watchPosition', 'clearWatch'])
        runtime.run_js("""
            var navigator = {
                language: 'en-GB',
                geolocation: __navigator_geolocation,
            };
        """)
