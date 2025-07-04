
__author__ = 'katharine'

import STPyV8 as v8
import errno
import logging
import os
import os.path
import dbm.dumb  # This is the only one that actually syncs data if the process dies before I can close().
logger = logging.getLogger("pypkjs.javascript.localstorage")

_storage_cache = {}  # This is used when filesystem-based storage is unavailable.


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

        runtime.register_syscall('__get_internal_localstorage', lambda : self)
        runtime.run_js("""
        (function() {
            var _internal = exec('__get_internal_localstorage', []);

            const handler = {
                get(target, property, receiver) {
                    if (['clear', 'getItem', 'setItem', 'removeItem', 'key'].includes(property)) {
                        return function(...args) {
                            return _internal[property](...args);
                        }
                    }
                    return _internal.get(null, property)
                }
            }

            _make_proxies(handler, _internal, ['set', 'has', 'deleteProperty', 'ownKeys', 'getOwnPropertyDescriptor', 'enumerate']);

            this.localStorage = new Proxy({}, handler);
        })();
        """)

    def get(self, receiver, name):
        return self.storage.get(str(name), v8.JSNull())

    def set(self, target, name, value, receiver):
        self.storage[str(name)] = str(value)
        return True

    def has(self, target, name):
        return name in self.storage

    def deleteProperty(self, target, name):
        if name in self.storage:
            del self.storage[name]
            return True
        else:
            return False

    def ownKeys(self, target):
        return v8.JSArray(list(self.storage.keys()))

    def getOwnPropertyDescriptor(self, target, name):
        if name in self.storage:
            return {
                'enumerable': True,
                'configurable': True
            }
        return None

    def enumerate(self, target, receiver):
        return v8.JSArray(list(self.storage.keys()))

    def clear(self, *args):
        self.storage.clear()

    def getItem(self, name, *args):
        return self.get(None, name)

    def setItem(self, name, value, *args):
        self.set(None, name, value, None)

    def removeItem(self, name, *args):
        return self.deleteProperty(None, name)

    def key(self, index, *args):
        if len(self.storage) > index:
            return list(self.storage.keys())[index]
        else:
            return v8.JSNull()

    def _shutdown(self):
        if hasattr(self.storage, 'close'):
            self.storage.close()
