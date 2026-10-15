
__author__ = 'katharine'

from .exceptions import JSRuntimeException
from .qjs import JSError


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

    def triggerEvent(self, event_name, event_type=None, data=None):
        """Dispatch event_name to its listeners and on<event_name> handler.

        The JS Event is built inside go(), on the event loop greenlet: callers may
        run on any greenlet, and touching the JS engine from one while another is
        suspended mid-call (e.g. in a sync XHR) can crash QuickJS."""
        def go():
            event = self.__runtime.build_event(event_type or event_name, data)
            for listener in list(self.__listeners.get(event_name, [])):
                try:
                    self.__runtime.call_js(listener, event)
                except (JSError, JSRuntimeException) as e:
                    self.__runtime.log_output(getattr(e, 'stackTrace', str(e)))
                except Exception as e:
                    self.__runtime.log_output(str(e))
                    raise
                finally:
                    if self.__runtime.event_aborted(event):
                        break
            # An on<event> handler property, if the subclass exposes one.
            dom_event = getattr(self, "on" + event_name, None)
            if dom_event is not None:
                try:
                    self.__runtime.call_js(dom_event, event)
                except (JSError, JSRuntimeException) as e:
                    self.__runtime.log_output(getattr(e, 'stackTrace', str(e)))
                except Exception as e:
                    self.__runtime.log_output(str(e))
                    raise

        self.__runtime.enqueue(go)
