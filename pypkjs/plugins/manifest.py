"""A pbw's `plugin` block (appinfo.json), as the phone app reads it (libpebble3 PluginManifest.kt)."""

import re

# Plugins only load from pbws built for the plugin-era appinfo rules.
MIN_MANIFEST_VERSION = 2

DEFAULT_REFRESH_SEC = 300

SOURCE_DEFAULTS = {
    'properties': {},
    'supportsMultiple': False,
    'callerPermissions': [],
    'suggestedRefreshIntervalSec': DEFAULT_REFRESH_SEC,
}

ACTION_DEFAULTS = {
    'description': '',
    'parameters': None,
    'targets': [],
    'destructive': False,
    'requiresConfirmation': False,
    'callerPermissions': [],
}

PERMISSION_KINDS = {'read', 'write', 'exfiltrating'}
PERMISSION_NAME = re.compile(r'[A-Za-z][A-Za-z0-9]*')
RESERVED_PERMISSION_NAMES = {'InvokeAction'}


def loadable_plugin(appinfo):
    """The pbw's plugin manifest with defaults filled in, or None if it carries none it may load."""
    plugin = appinfo.get('plugin')
    if not isinstance(plugin, dict):
        return None
    if int(appinfo.get('manifestVersion', 0) or 0) < MIN_MANIFEST_VERSION:
        return None
    return {
        'description': plugin.get('description', ''),
        'script': plugin.get('script', 'plugin.js'),
        'oauth': plugin.get('oauth', {}),
        'definesPermissions': plugin.get('definesPermissions', []),
        'sources': [_with_defaults(s, SOURCE_DEFAULTS) for s in plugin.get('sources', [])],
        'actions': [_with_defaults(a, ACTION_DEFAULTS) for a in plugin.get('actions', [])],
    }


def _with_defaults(declaration, defaults):
    filled = dict(declaration)
    for key, value in defaults.items():
        filled.setdefault(key, value)
    filled['callerPermissions'] = [_permission(p) for p in filled['callerPermissions']]
    return filled


def _permission(permission):
    """A manifest may write a permission as its bare name; it always reads back as an object."""
    if not isinstance(permission, dict):
        return {'name': str(permission)}
    return {k: v for k, v in permission.items() if k == 'name' or v}


def problems(manifest):
    """What the phone app would reject this manifest's permission definitions for."""
    errors = []
    for d in manifest.get('definesPermissions', []):
        name = d.get('name', '')
        if not PERMISSION_NAME.fullmatch(name):
            errors.append("permission name '%s' must be alphanumeric" % name)
        if name in RESERVED_PERMISSION_NAMES:
            errors.append("permission name '%s' is reserved" % name)
        if not str(d.get('label', '')).strip():
            errors.append("permission '%s' has no label" % name)
        if d.get('kind') not in PERMISSION_KINDS:
            errors.append("permission '%s' needs a kind: one of %s" % (name, ', '.join(sorted(PERMISSION_KINDS))))
    return errors


def serves(source, category, item):
    return source['category'] == category and item in source['items']


def required_params(action):
    return list((action.get('parameters') or {}).get('required', []))


def caller_permissions(declaration):
    return [p['name'] for p in declaration.get('callerPermissions', [])]


def without_defaults(declaration, defaults):
    """As the app serialises declarations to PKJS: fields still at their default are left out."""
    return {k: v for k, v in declaration.items() if k not in defaults or defaults[k] != v}
