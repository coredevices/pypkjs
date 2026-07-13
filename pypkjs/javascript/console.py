
__author__ = 'katharine'

import json
import logging

from .qjs import from_js

logger = logging.getLogger("pypkjs.javascript.console")


def _stringify(value):
    value = from_js(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    # JS numbers arrive as Python floats; render integer-valued ones without the
    # trailing ".0" so console output matches V8 (42, not 42.0).
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class Console(object):
    def __init__(self, runtime):
        self.runtime = runtime
        runtime.context.expose("console", self, ['log', 'warn', 'info', 'error'])

    def log(self, *params):
        # ponytail: dropped the V8 JSStackTrace-based "file:line" prefix -- it was
        # cosmetic and V8-only. Reinstate via a parsed QuickJS Error().stack if the
        # per-line attribution turns out to matter.
        self.runtime.log_output(' '.join(_stringify(x) for x in params))

    def warn(self, *params):
        self.log(*params)

    def info(self, *params):
        self.log(*params)

    def error(self, *params):
        self.log(*params)
