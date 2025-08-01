
__author__ = 'katharine'

import STPyV8 as v8


class JSRuntimeException(Exception):
    def __init__(self, message):
        trace = v8.JSStackTrace.GetCurrentStackTrace(20, v8.JSStackTrace.Options.Detailed)
        self.stackTrace = "Error: %s\n%s" % (message, str(trace))
        Exception.__init__(self, message)
