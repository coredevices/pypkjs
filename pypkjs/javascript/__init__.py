
__author__ = 'katharine'

from .console import Console
from .performance import Performance
from .timers import Timers
from .localstorage import LocalStorage
from .pebble import Pebble
from .xhr import prepare_xhr, XHRExtension
from .navigator import Navigator
from .ws import prepare_ws, WSExtension
from .events import EventExtension


class PebbleKitJS(object):
    def __init__(self, runtime, pebble, persist=None):
        self.runtime = runtime
        self.pebble = Pebble(runtime, pebble)
        self.local_storage = LocalStorage(runtime, persist)

        self.extensions = [
            Console(runtime),
            Performance(runtime),
            self.local_storage,
            Navigator(runtime),
            Timers(runtime),
            self.pebble,
            EventExtension(runtime),
            XHRExtension(runtime),
            WSExtension(runtime),
        ]

    def do_post_setup(self):
        prepare_xhr(self.runtime)
        prepare_ws(self.runtime)

    def shutdown(self):
        self.local_storage._shutdown()
        self.pebble._shutdown()
