"""Phone-side plugins (sources and actions) for PebbleKit JS apps, as the Core phone app hosts them."""

PLUGIN_UNAVAILABLE = 'PLUGIN_UNAVAILABLE'
PERMISSION_DENIED = 'PERMISSION_DENIED'
RATE_LIMITED = 'RATE_LIMITED'
TIMEOUT = 'TIMEOUT'
AUTH_REQUIRED = 'AUTH_REQUIRED'
INVALID_ARGS = 'INVALID_ARGS'
INVALID_REQUEST = 'INVALID_REQUEST'
UNKNOWN = 'UNKNOWN'


def action_error(code, message=None):
    return {'ok': False, 'code': code, 'message': message}


class PluginRetired(Exception):
    """The plugin was replaced or removed while something was subscribed to it."""
