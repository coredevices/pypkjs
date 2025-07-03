
__author__ = 'katharine'

import STPyV8 as v8
from .geolocation import Geolocation


class Navigator(object):
    def __init__(self, runtime):

        self._runtime = runtime
        self._runtime = runtime

        runtime.register_syscall('__get_internal_location', lambda : Geolocation(runtime))

        runtime.run_js("""
        navigator = new (function() {
            var _internal_location = exec('__get_internal_location', []);
            this.language = 'en-GB';

            var location = _internal_location;
            if(true) { // TODO: this should be a check on geolocation being enabled.
                this.geolocation = new (function() {
                    _make_proxies(this, location, ['getCurrentPosition', 'watchPosition', 'clearWatch']);
                })();
            }
        })();
        """)
