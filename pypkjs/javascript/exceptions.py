
__author__ = 'katharine'


class JSRuntimeException(Exception):
    def __init__(self, message):
        # QuickJS has no equivalent of V8's JSStackTrace capture; the message is
        # the useful part of the log line anyway.
        self.stackTrace = "Error: %s" % message
        Exception.__init__(self, message)
