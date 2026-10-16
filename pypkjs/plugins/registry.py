"""Every plugin PKJS apps can reach, looked up the way the phone app does (PluginRegistry.kt)."""

import logging

from . import manifest as manifests

logger = logging.getLogger('pypkjs.plugins')


class PluginRegistry(object):
    def __init__(self):
        # Insertion-ordered: built-ins first, then installed plugins, each keeping its first
        # position when replaced. Source lookups fall back in this order.
        self._plugins = {}

    def register(self, plugin):
        old = self._plugins.get(plugin.uuid)
        if old is not None and old is not plugin:
            old.retire()
        self._plugins[plugin.uuid] = plugin

    def unregister(self, uuid):
        plugin = self._plugins.pop(uuid, None)
        if plugin is not None:
            plugin.retire()
        return plugin

    def all(self):
        return list(self._plugins.values())

    def find_plugin(self, uuid):
        """Exact lookup. Actions never fall back to a different plugin."""
        return next((p for p in self._plugins.values() if str(p.uuid) == str(uuid)), None)

    def find_source_plugin(self, category, item, preferred_uuid=None):
        plugins = self.all()
        if preferred_uuid is not None:
            for p in plugins:
                if str(p.uuid) == str(preferred_uuid) and p.source(category, item) is not None:
                    return p
        return next((p for p in plugins if p.source(category, item) is not None), None)

    def summaries(self):
        """What `Pebble.enumeratePlugins()` returns."""
        return [{
            'uuid': str(p.uuid),
            'name': p.name,
            'sources': [manifests.without_defaults(s, manifests.SOURCE_DEFAULTS) for s in p.sources],
            'actions': [manifests.without_defaults(a, manifests.ACTION_DEFAULTS) for a in p.actions],
        } for p in self.all()]
