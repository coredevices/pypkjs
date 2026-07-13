
__author__ = 'katharine'


class JSRuntimeException(Exception):
    def __init__(self, message):
        # ponytail: dropped the V8 JSStackTrace capture -- it was only ever used to
        # populate .stackTrace for logging. The message is the useful part.
        self.stackTrace = "Error: %s" % message
        Exception.__init__(self, message)
