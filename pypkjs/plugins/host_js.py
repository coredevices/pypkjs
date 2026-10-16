"""JS evaluated into every plugin session ahead of the plugin's own script.

PLUGIN_HOST_JS and BASE64_JS are verbatim copies of the phone app's (libpebble3
PluginHostJs.kt / Base64Js.kt) and must be kept in step with them. The rest stand in for
host APIs the app implements natively.
"""

PLUGIN_HOST_JS = r'''(function (global) {
  var sourceHandler = null;
  var actionHandler = null;
  var configHandler = null;

  global.console = {
    log:   function () { _JsBridge.log('info',  join(arguments)); },
    info:  function () { _JsBridge.log('info',  join(arguments)); },
    warn:  function () { _JsBridge.log('warn',  join(arguments)); },
    error: function () { _JsBridge.log('error', join(arguments)); },
  };

  function join(args) {
    var parts = [];
    for (var i = 0; i < args.length; i++) {
      var a = args[i];
      parts.push(typeof a === 'string' ? a : safeStringify(a));
    }
    return parts.join(' ');
  }

  function safeStringify(v) {
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }

  global.Pebble = global.Pebble || {};
  global.Pebble.registerSourceHandler = function (fn) { sourceHandler = fn; };
  global.Pebble.registerActionHandler = function (fn) { actionHandler = fn; };

  /**
   * Messages from this plugin's config page, while it is open. Nothing else can reach this —
   * it is the plugin's own conversation with its own settings UI, so the payloads are whatever
   * the two of them agree on.
   */
  global.Pebble.registerConfigHandler = function (fn) { configHandler = fn; };

  /**
   * Report `"<category>/<item>"` sources as stale, so anything watching them re-reads now
   * rather than at the end of its poll interval. For changes made outside a source or action
   * request — the config page adding a stock, say.
   */
  global.Pebble.refreshSources = function (keys) {
    _JsBridge.refreshSources(JSON.stringify(keys || []));
  };

  /** Push at the open config page. No reply. */
  global.Pebble.sendConfigMessage = function (message) {
    _JsBridge.configMessage(JSON.stringify(message === undefined ? null : message));
  };

  global.__pluginConfigMessage = function (requestId, message) {
    var settled = false;
    function respond(reply) {
      if (settled) return;
      settled = true;
      _JsBridge.respond(requestId, JSON.stringify(reply === undefined ? null : reply));
    }
    if (!configHandler) {
      respond({ error: 'no config handler registered' });
      return;
    }
    try {
      var maybePromise = configHandler(message, respond);
      if (maybePromise && typeof maybePromise.catch === 'function') {
        maybePromise.catch(function (e) { respond({ error: String(e) }); });
      }
    } catch (e) {
      respond({ error: String(e) });
    }
  };

  global.__pluginDispatch = function (requestId, request) {
    var settled = false;
    function send(payload) {
      if (settled) return;
      settled = true;
      _JsBridge.respond(requestId, JSON.stringify(payload));
    }
    var respond = {
      data: function (envelope) { send(envelope || { instances: [] }); },
      ok: function (result) {
        result = result || {};
        send({ ok: true, text: result.text || null, refreshed: result.refreshed || [] });
      },
      error: function (code, message) {
        if (request.kind === 'action') {
          send({ ok: false, code: code || 'UNKNOWN', message: message || null });
        } else {
          send({ error: (code || 'UNKNOWN') + (message ? ': ' + message : '') });
        }
      },
    };

    try {
      var handler = request.kind === 'action' ? actionHandler : sourceHandler;
      if (!handler) {
        respond.error('PLUGIN_UNAVAILABLE', 'no ' + request.kind + ' handler registered');
        return;
      }
      var maybePromise = handler(request, respond);
      if (maybePromise && typeof maybePromise.catch === 'function') {
        maybePromise.catch(function (e) { respond.error('UNKNOWN', String(e)); });
      }
    } catch (e) {
      respond.error('UNKNOWN', String(e));
    }
  };
})(this);
'''

BASE64_JS = r'''(function (global) {
  if (typeof global.atob === 'function' && typeof global.btoa === 'function') return;

  var CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  var VALUES = {};
  for (var i = 0; i < CHARS.length; i++) VALUES[CHARS.charAt(i)] = i;

  /** Binary string (one byte per char) -> base64. */
  global.btoa = function (input) {
    var binary = String(input);
    var out = '';
    for (var i = 0; i < binary.length; i += 3) {
      var a = binary.charCodeAt(i);
      var b = binary.charCodeAt(i + 1);
      var c = binary.charCodeAt(i + 2);
      if (a > 255 || b > 255 || c > 255) throw new Error('btoa: not a binary string');
      var chunk = (a << 16) | ((b || 0) << 8) | (c || 0);
      out += CHARS.charAt((chunk >> 18) & 63) +
             CHARS.charAt((chunk >> 12) & 63) +
             (isNaN(b) ? '=' : CHARS.charAt((chunk >> 6) & 63)) +
             (isNaN(c) ? '=' : CHARS.charAt(chunk & 63));
    }
    return out;
  };

  /** base64 -> binary string. */
  global.atob = function (input) {
    var base64 = String(input).replace(/[\s=]/g, '');
    if (base64.length % 4 === 1) throw new Error('atob: bad base64');
    var out = '';
    var bits = 0;
    var held = 0;
    for (var i = 0; i < base64.length; i++) {
      var value = VALUES[base64.charAt(i)];
      if (value === undefined) throw new Error('atob: bad base64');
      bits = (bits << 6) | value;
      held += 6;
      if (held >= 8) {
        held -= 8;
        out += String.fromCharCode((bits >> held) & 255);
      }
    }
    return out;
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
'''

# The app's fetch (FetchJs.kt) talks to its native request manager; this is the same surface
# built on pypkjs' XMLHttpRequest. Headers and Response match the app's.
FETCH_JS = r'''(function (global) {
  function Headers(init) {
    this._entries = [];
    if (!init) return;
    var self = this;
    if (init instanceof Headers) {
      init.forEach(function (value, name) { self.append(name, value); });
    } else if (Array.isArray(init)) {
      init.forEach(function (pair) { self.append(pair[0], pair[1]); });
    } else {
      Object.keys(init).forEach(function (name) { self.append(name, init[name]); });
    }
  }

  Headers.prototype.append = function (name, value) {
    this._entries.push([String(name).toLowerCase(), String(value)]);
  };
  Headers.prototype.set = function (name, value) {
    var key = String(name).toLowerCase();
    this._entries = this._entries.filter(function (e) { return e[0] !== key; });
    this.append(key, value);
  };
  Headers.prototype.get = function (name) {
    var key = String(name).toLowerCase();
    var hit = this._entries.filter(function (e) { return e[0] === key; });
    return hit.length ? hit.map(function (e) { return e[1]; }).join(', ') : null;
  };
  Headers.prototype.has = function (name) { return this.get(name) !== null; };
  Headers.prototype.forEach = function (callback, thisArg) {
    var self = this;
    this._entries.slice().forEach(function (e) { callback.call(thisArg, e[1], e[0], self); });
  };
  Headers.prototype.keys = function () { return this._entries.map(function (e) { return e[0]; }); };
  Headers.prototype.entries = function () { return this._entries.slice(); };

  function Response(body, init) {
    this._body = body;
    this.status = init.status;
    this.statusText = init.statusText || '';
    this.headers = init.headers;
    this.url = init.url || '';
    this.ok = this.status >= 200 && this.status < 300;
    this.bodyUsed = false;
  }

  Response.prototype._consume = function () {
    if (this.bodyUsed) return Promise.reject(new TypeError('body already read'));
    this.bodyUsed = true;
    return Promise.resolve(this._body);
  };
  Response.prototype.text = function () {
    return this._consume().then(function (body) { return body === null ? '' : String(body); });
  };
  Response.prototype.json = function () { return this.text().then(JSON.parse); };
  Response.prototype.arrayBuffer = function () {
    return this._consume().then(function (body) {
      if (body instanceof Uint8Array) return body.buffer;
      var text = body === null ? '' : String(body);
      var bytes = new Uint8Array(text.length);
      for (var i = 0; i < text.length; i++) bytes[i] = text.charCodeAt(i) & 0xff;
      return bytes.buffer;
    });
  };

  function parseHeaders(raw) {
    var headers = new Headers();
    (raw || '').split('\r\n').forEach(function (line) {
      var split = line.indexOf(': ');
      if (split > 0) headers.append(line.substring(0, split), line.substring(split + 2));
    });
    return headers;
  }

  global.Headers = Headers;
  global.Response = Response;

  global.fetch = function (url, options) {
    options = options || {};
    var target = String(url);
    var responseType = options.responseType === 'arraybuffer' ? 'arraybuffer' : '';

    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      var settled = false;
      function fail(type) {
        if (settled) return;
        settled = true;
        reject(new TypeError('fetch ' + (type === 'error' ? 'failed' : type) + ': ' + target));
      }
      xhr.open(options.method || 'GET', target, true);
      xhr.responseType = responseType;
      if (options.headers) {
        new Headers(options.headers).forEach(function (value, name) { xhr.setRequestHeader(name, value); });
      }
      xhr.onload = function () {
        if (settled) return;
        settled = true;
        var body = responseType === 'arraybuffer'
          ? new Uint8Array(xhr.response || new ArrayBuffer(0))
          : xhr.responseText;
        resolve(new Response(body, {
          status: xhr.status,
          statusText: xhr.statusText,
          headers: parseHeaders(xhr.getAllResponseHeaders()),
          url: target,
        }));
      };
      xhr.onerror = function () { fail('error'); };
      xhr.ontimeout = function () { fail('timeout'); };
      xhr.onabort = function () { fail('abort'); };
      var signal = options.signal;
      if (signal) {
        if (signal.aborted) {
          fail('abort');
          return;
        }
        if (typeof signal.addEventListener === 'function') {
          signal.addEventListener('abort', function () { xhr.abort(); });
        }
      }
      xhr.send(options.body === undefined ? null : options.body);
    });
  };
})(this);
'''


def promise_bridge_js(bridge_name, reply_function, failure_message):
    """The app's promiseBridgeJs (PromiseJsBridge.kt): `call(op, args)` -> a Promise settled by
    `<reply_function>(id, {result} | {error: {code, message}})`."""
    return r'''
  var nextId = 1;
  var pending = Object.create(null);
  global.%(reply)s = function (id, payload) {
    var callbacks = pending[id];
    if (!callbacks) return;
    delete pending[id];
    if (payload && payload.error) {
      var error = new Error(payload.error.message || '%(failure)s');
      error.code = payload.error.code;
      callbacks.reject(error);
    } else {
      callbacks.resolve(payload ? payload.result : undefined);
    }
  };
  function call(operation, args) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      %(bridge)s.request(id, JSON.stringify(Object.assign({ operation: operation }, args)));
    });
  }
''' % {'bridge': bridge_name, 'reply': reply_function, 'failure': failure_message}


SECURE_STORAGE_BRIDGE = '_PluginSecureStorageBridge'
SECURE_STORAGE_REPLY = '__pluginSecureStorageReply'

SECURE_STORAGE_JS = r'''(function (global) {
%s
  Pebble.secureStorage = Object.freeze({
    getItem: function (key) { return call('getItem', { key: String(key) }); },
    setItem: function (key, value) { return call('setItem', { key: String(key), value: String(value) }); },
    removeItem: function (key) { return call('removeItem', { key: String(key) }); }
  });
})(this);
''' % promise_bridge_js(SECURE_STORAGE_BRIDGE, SECURE_STORAGE_REPLY, 'secure storage operation failed')

# Hosted OAuth goes through Core's broker, which the emulator can't reach. Plugins that declare a
# connector get calls that fail the way a signed-out phone does.
OAUTH_UNAVAILABLE_JS = r'''(function (global) {
  function unavailable() {
    var error = new Error('OAuth sign-in is not available in the emulator');
    error.code = 'AUTH_REQUIRED';
    return Promise.reject(error);
  }
  Pebble.oauth = Object.freeze({ authorize: unavailable, refresh: unavailable });
})(this);
'''
