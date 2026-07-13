
__author__ = 'katharine'

import time


class Performance(object):
    # This is an approximation for now.
    def __init__(self, runtime):
        self._start = time.time()
        runtime.context.expose("performance", self, ['now'])

    def now(self):
        return (time.time() - self._start) * 1000
