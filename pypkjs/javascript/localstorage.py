
__author__ = 'katharine'

import errno
import logging
import os
import os.path
import dbm.dumb  # This is the only one that actually syncs data if the process dies before I can close().
logger = logging.getLogger("pypkjs.javascript.localstorage")

_storage_cache = {}  # This is used when filesystem-based storage is unavailable.


def _to_str(value):
    # The dbm.dumb persistent store returns bytes for keys and values; the transient
    # dict store returns str. JS values must be str -- quickjs won't convert bytes.
    return value.decode('utf-8') if isinstance(value, bytes) else value


class LocalStorage(object):
    def __init__(self, runtime, persist_dir=None):
        self.storage = None
        if persist_dir is not None:
            try:
                try:
                    os.makedirs(os.path.join(persist_dir, 'localstorage'))
                except OSError as e:
                    if e.errno != errno.EEXIST:
                        raise

                self.storage = dbm.dumb.open(os.path.join(persist_dir, 'localstorage', str(runtime.pbw.uuid)), 'c')
            except IOError:
                pass
        if self.storage is None:
            logger.warning("Using transient store.")
            self.storage = _storage_cache.setdefault(str(runtime.pbw.uuid), {})

        # Expose the Python methods as a native object, then build a JS Proxy whose
        # traps delegate to it. Property access (localStorage.foo) hits the get/set
        # traps; the DOM methods (getItem, ...) are returned as functions by get.
        runtime.context.expose("__localstorage", self,
                               ['get', 'set', 'has', 'deleteProperty', 'ownKeys',
                                'getOwnPropertyDescriptor', 'clear', 'getItem', 'setItem',
                                'removeItem', 'key'])
        runtime.run_js("""
        (function() {
            var _ls = __localstorage;
            var methods = ['clear', 'getItem', 'setItem', 'removeItem', 'key'];
            var handler = {
                get: function(target, property, receiver) {
                    if (methods.indexOf(property) !== -1) {
                        if (property === 'setItem') {
                            // Coerce JS-side: String(true) -> 'true', String([1,2]) -> '1,2'
                            // (Python str() would store 'True' / a quickjs.Object repr).
                            return function(k, v) { return _ls.setItem(String(k), String(v)); };
                        }
                        return function() { return _ls[property].apply(_ls, arguments); };
                    }
                    return _ls.get(null, property);
                },
                set: function(target, property, value, receiver) {
                    return _ls.set(null, property, String(value), null);
                },
                has: function(target, property) { return _ls.has(null, property); },
                deleteProperty: function(target, property) { return _ls.deleteProperty(null, property); },
                ownKeys: function(target) { return _ls.ownKeys(null); },
                getOwnPropertyDescriptor: function(target, property) {
                    return _ls.getOwnPropertyDescriptor(null, property);
                },
            };
            window.localStorage = new Proxy({}, handler);
        })();
        """)

    def get(self, receiver, name):
        return _to_str(self.storage.get(str(name), None))

    def set(self, target, name, value, receiver):
        self.storage[str(name)] = str(value)
        return True

    def has(self, target, name):
        return str(name) in self.storage

    def deleteProperty(self, target, name):
        if str(name) in self.storage:
            del self.storage[str(name)]
            return True
        else:
            return False

    def ownKeys(self, target):
        return [_to_str(k) for k in self.storage.keys()]

    def getOwnPropertyDescriptor(self, target, name):
        if str(name) in self.storage:
            return {
                'enumerable': True,
                'configurable': True,
            }
        return None

    def clear(self, *args):
        self.storage.clear()

    def getItem(self, name, *args):
        return self.get(None, name)

    def setItem(self, name, value, *args):
        self.set(None, name, value, None)

    def removeItem(self, name, *args):
        return self.deleteProperty(None, name)

    def key(self, index, *args):
        keys = list(self.storage.keys())
        if len(keys) > index:
            return _to_str(keys[index])
        else:
            return None

    def _shutdown(self):
        if hasattr(self.storage, 'close'):
            self.storage.close()
