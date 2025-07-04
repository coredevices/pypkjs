
__author__ = 'katharine'

import STPyV8 as v8
import time


class Performance(object):
    # This is an approximation for now
    def __init__(self, runtime):
        runtime.register_syscall("__get_time", lambda : time.time())
        runtime.run_js("""
            performance = new (function() {
                function _time() {
                    return exec('__get_time', []);
                }
            
                var start = _time();

                this.now = function() {
                    return (_time() - start) * 1000;
                };
            })();
        """)