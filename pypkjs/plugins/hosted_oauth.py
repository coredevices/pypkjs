"""`Pebble.oauth` on the emulator, through the appstore's OAuth broker like the phone's HostedOAuth.

The emulator signs in as the pebble-tool user (`pebble login`): pebble-tool points
PYPKJS_FIREBASE_CREDENTIALS at its Firebase credentials file. The provider redirects to the broker,
which sends the browser back to a one-shot server on 127.0.0.1 (`loopback_port`) instead of the
phone's pebble:// link. The code it delivers is redeemed with the PKCE verifier and the bearer.
"""

import base64
import datetime
import hashlib
import json
import logging
import os
import secrets
import urllib.parse
import webbrowser

import gevent
import gevent.event
import requests
from gevent import pywsgi

logger = logging.getLogger('pypkjs.plugins.oauth')

DEFAULT_API_BASE = 'https://appstore-api.repebble.com'
SIGN_IN_TIMEOUT = 300

_cached = {}


class OAuthError(Exception):
    def __init__(self, message, code='AUTH_REQUIRED'):
        super(OAuthError, self).__init__(message)
        self.code = code


def api_base():
    return (os.environ.get('PYPKJS_APPSTORE_API_BASE') or DEFAULT_API_BASE).rstrip('/')


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _expired(expires_at):
    try:
        expiry = datetime.datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    return _now() >= expiry - datetime.timedelta(minutes=2)


def id_token():
    """The pebble-tool user's Firebase ID token, refreshed in memory when it's about to expire."""
    path = os.environ.get('PYPKJS_FIREBASE_CREDENTIALS')
    if not path:
        raise OAuthError('OAuth sign-in needs a Pebble account: run `pebble login`, then `pebble kill` '
                         'and start the emulator again')
    # Re-read whenever pebble-tool rewrites the file (login, logout, its own refresh); a token this
    # module refreshed is kept only while the file is unchanged.
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        _cached.pop(path, None)
        raise OAuthError('No Pebble account sign-in found: run `pebble login`')
    cached_mtime, creds = _cached.get(path, (None, None))
    if cached_mtime != mtime:
        try:
            with open(path) as f:
                creds = json.load(f)
        except (IOError, ValueError):
            raise OAuthError("Couldn't read the Pebble account at %s: run `pebble login`" % path)
    if creds.get('expires_at') and _expired(creds['expires_at']):
        if not creds.get('refresh_token') or not creds.get('firebase_api_key'):
            raise OAuthError('The Pebble account sign-in expired: run `pebble login`')
        try:
            response = requests.post('https://securetoken.googleapis.com/v1/token?key=%s' % creds['firebase_api_key'],
                                     data={'grant_type': 'refresh_token', 'refresh_token': creds['refresh_token']},
                                     timeout=20)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as e:
            raise OAuthError("Couldn't refresh the Pebble account sign-in: %s" % e)
        creds = dict(creds, id_token=payload['id_token'],
                     refresh_token=payload.get('refresh_token', creds['refresh_token']),
                     expires_at=(_now() + datetime.timedelta(seconds=int(payload.get('expires_in', 3600)))).isoformat())
    _cached[path] = (mtime, creds)
    if not creds.get('id_token'):
        raise OAuthError('No Pebble account sign-in found: run `pebble login`')
    return creds['id_token']


def _broker(method, path, **kwargs):
    try:
        response = requests.request(method, api_base() + path, timeout=30,
                                    headers={'Authorization': 'Bearer %s' % id_token()}, **kwargs)
    except requests.RequestException as e:
        raise OAuthError("Couldn't reach the OAuth broker: %s" % e, 'NETWORK')
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        raise OAuthError('OAuth broker %s failed (%d): %s' % (path.rsplit('/', 1)[-1], response.status_code,
                                                             payload.get('error') or response.text[:200]))
    return payload


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=')
    return verifier, challenge.decode('ascii')


def authorize(plugin_uuid, slug, log=logger.info, open_browser=webbrowser.open):
    plugin_uuid = str(plugin_uuid).lower()
    verifier, challenge = _pkce()
    expected_path = '/oauth/%s/%s' % (plugin_uuid, slug)
    redirect = gevent.event.AsyncResult()

    def app(environ, start_response):
        if environ.get('PATH_INFO') != expected_path:
            start_response('404 Not Found', [('Content-Type', 'text/plain')])
            return [b'Not found']
        params = {k: v[0] for k, v in urllib.parse.parse_qs(environ.get('QUERY_STRING', '')).items()}
        redirect.set(params)
        message = ('Signed in. You can close this tab and go back to the emulator.' if params.get('code') else
                   'Sign-in failed: %s' % params.get('error', 'unknown error'))
        start_response('200 OK', [('Content-Type', 'text/plain; charset=utf-8')])
        return [message.encode('utf-8')]

    server = pywsgi.WSGIServer(('127.0.0.1', 0), app, log=None)
    server.start()
    try:
        started = _broker('GET', '/api/oauth/broker/start', params={
            'plugin_uuid': plugin_uuid, 'connector_slug': slug, 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'loopback_port': server.server_port,
        })
        log('Sign in to %s in your browser: %s' % (slug, started['url']))
        open_browser(started['url'])
        try:
            params = redirect.get(timeout=SIGN_IN_TIMEOUT)
        except gevent.Timeout:
            raise OAuthError('Sign-in timed out')
        if not params.get('code'):
            raise OAuthError('Sign-in failed: %s' % params.get('error', 'no code returned'))
        return _broker('POST', '/api/oauth/broker/exchange', json={'code': params['code'], 'code_verifier': verifier})
    finally:
        server.stop()


def refresh(broker_refresh):
    if not broker_refresh:
        raise OAuthError('Nothing to refresh: sign in again')
    return _broker('POST', '/api/oauth/broker/refresh', json={'broker_refresh': broker_refresh})
