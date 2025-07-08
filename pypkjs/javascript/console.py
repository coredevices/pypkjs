
__author__ = 'katharine'

import STPyV8 as v8
import logging

logger = logging.getLogger("pypkjs.javascript.console")

class Console(object):
    def __init__(self, runtime):
        self.runtime = runtime

        runtime.register_syscall("__get_internal_console", lambda : self)

        runtime.run_js("""
        console = new (function () {
            var _internal_console = exec('__get_internal_console', []);
            _make_proxies(this, _internal_console, ['log', 'warn', 'info', 'error']);
        })();
        """)

    def log(self, *params):
        # kOverview == kLineNumber | kColumnOffset | kScriptName | kFunctionName
        trace_str = str(v8.JSStackTrace.GetCurrentStackTrace(2, v8.JSStackTrace.Options.Overview))
        try:
            frames = v8.JSError.parse_stack(trace_str.strip())
            caller_frame = frames[0]
            filename = caller_frame[1]
            line_num = caller_frame[2]
            file_and_line = "{}:{}".format(filename, line_num)
        except:
            file_and_line = "???:?:?"

        log_str = ' '.join([
            x.toString() if hasattr(x, 'toString')
            else str(x)
            for x in params
        ])

        logger.debug("{} {}".format(file_and_line, log_str))
        self.runtime.log_output("{} {}".format(file_and_line, log_str))

    def warn(self, *params):
        self.log(*params)

    def info(self, *params):
        self.log(*params)

    def error(self, *params):
        self.log(*params)