
__author__ = 'katharine'

import logging

logger = logging.getLogger("pypkjs.javascript.console")


class Console(object):
    def __init__(self, runtime):
        self.runtime = runtime
        # Arguments are formatted JS-side: JSON.stringify can't represent errors,
        # functions, symbols or circular objects, and logging must never throw.
        runtime.context.expose("__console", self, ['output'])
        runtime.run_js("""
        (function() {
            function format(x) {
                if (typeof x === 'string') return x;
                if (x instanceof Error) return String(x);
                if (x !== null && typeof x === 'object') {
                    try {
                        var json = JSON.stringify(x);
                        if (json !== undefined) return json;
                    } catch (e) {}
                }
                try { return String(x); } catch (e) { return Object.prototype.toString.call(x); }
            }
            function log() {
                var parts = [];
                for (var i = 0; i < arguments.length; i++) { parts.push(format(arguments[i])); }
                __console.output(parts.join(' '));
            }
            var console = { log: log, warn: log, info: log, error: log, debug: log };
            window.console = console;
        })();
        """)

    def output(self, message):
        # The V8 runtime prefixed lines with the caller's "file:line"; QuickJS's
        # binding doesn't expose script names, so we log the message alone.
        self.runtime.log_output(message)
