
__author__ = 'katharine'

from .exceptions import JSRuntimeException
from .qjs import JSError, from_js


class EventExtension:
    def __init__(self, runtime):
        # The base Event constructor lives in runtime.setup(). ProgressEvent (used
        # by XHR) extends it.
        runtime.run_js("""
            var ProgressEvent = function(computable, loaded, total) {
                Event.call(this, 'progress');
                this.lengthComputable = computable || false;
                this.loaded = loaded || 0;
                this.total = total || 0;
            };
            ProgressEvent.prototype = Object.create(Event.prototype);
            ProgressEvent.prototype.constructor = ProgressEvent;
        """)


def Event(runtime, event_type, data=None):
    """Mint a JS Event object (carrying its methods) with optional data props."""
    return runtime.build_event(event_type, data)


class EventSourceMixin(object):
    def __init__(self, runtime):
        self.__listeners = {}
        self.__runtime = runtime
        super(EventSourceMixin, self).__init__()

    def addEventListener(self, event, listener, capture=False):
        self.__listeners.setdefault(event, []).append(listener)

    def removeEventListener(self, event, listener=None):
        if event not in self.__listeners:
            return
        if listener is None:
            del self.__listeners[event]
        else:
            # js_same, not !=: the same JS function arrives as a fresh wrapper
            # object on every crossing, so Python equality never matches.
            self.__listeners[event] = [l for l in self.__listeners[event]
                                       if not self.__runtime.js_same(l, listener)]

    def triggerEvent(self, event_name, event=None, *params):
        if event is None:
            event = Event(self.__runtime, event_name)

        def go():
            for listener in list(self.__listeners.get(event_name, [])):
                try:
                    self.__runtime.call_js(listener, event, *params)
                except (JSError, JSRuntimeException) as e:
                    self.__runtime.log_output(getattr(e, 'stackTrace', str(e)))
                except Exception as e:
                    self.__runtime.log_output(str(e))
                    raise
                finally:
                    if from_js(event).get('_aborted'):
                        break
            # An on<event> handler property, if the subclass exposes one.
            dom_event = getattr(self, "on" + event_name, None)
            if dom_event is not None:
                try:
                    self.__runtime.call_js(dom_event, event, *params)
                except (JSError, JSRuntimeException) as e:
                    self.__runtime.log_output(getattr(e, 'stackTrace', str(e)))
                except Exception as e:
                    self.__runtime.log_output(str(e))
                    raise

        self.__runtime.enqueue(go)
