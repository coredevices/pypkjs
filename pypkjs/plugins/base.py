"""What every plugin, script-backed or built in, shares: its declarations and the source poll loop."""

import gevent.event

from . import PluginRetired
from . import manifest as manifests


class Plugin(object):
    builtin = False

    def __init__(self, uuid, name, description, sources, actions):
        self.uuid = uuid
        self.name = name
        self.description = description
        self.sources = sources
        self.actions = actions
        self.retired = False
        self._refresh_waiters = []

    def source(self, category, item):
        return next((s for s in self.sources if manifests.serves(s, category, item)), None)

    def action(self, name):
        return next((a for a in self.actions if a['name'] == name), None)

    def retire(self):
        self.retired = True
        self.refresh(None)

    def refresh(self, keys):
        """Cut short the poll wait of subscriptions to these "<category>/<item>" keys (None: all)."""
        for key, event in list(self._refresh_waiters):
            if keys is None or key in keys:
                event.set()

    def observe(self, category, item, properties, icon_pixel_size, emit):
        """Read the source until killed, calling emit(envelope) with each good reading."""
        declaration = self.source(category, item)
        if declaration is None:
            return
        key = '%s/%s' % (category, item)
        while True:
            if self.retired:
                raise PluginRetired('%s was updated or removed; subscribe again' % self.name)
            envelope = self.read(category, item, properties, icon_pixel_size)
            if envelope is not None:
                emit(envelope)
            self._wait_for_refresh(key, max(1, declaration['suggestedRefreshIntervalSec']))

    def read(self, category, item, properties, icon_pixel_size):
        """One reading of a source this plugin serves, as an envelope; None to skip this round."""
        raise NotImplementedError

    def invoke(self, action, args):
        raise NotImplementedError

    def _wait_for_refresh(self, key, timeout):
        waiter = (key, gevent.event.Event())
        self._refresh_waiters.append(waiter)
        try:
            waiter[1].wait(timeout)
        finally:
            self._refresh_waiters.remove(waiter)
