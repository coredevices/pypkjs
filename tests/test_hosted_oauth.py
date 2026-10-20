"""Pebble.oauth's emulator flow (pypkjs/plugins/hosted_oauth.py) against a fake broker: start with a
loopback port, a "browser" that follows the redirect to 127.0.0.1, then exchange with the verifier.

Runs in a subprocess: importing pypkjs monkey-patches the interpreter for gevent.
"""
import json
import os
import subprocess
import sys

SCRIPT = r'''
import base64, hashlib, json, os, sys, urllib.parse
import pypkjs.plugins.hosted_oauth as oauth
import requests

calls = []

class Resp(object):
    def __init__(self, payload, status=200):
        self.status_code, self._payload, self.text = status, payload, json.dumps(payload)
    def json(self):
        return self._payload

def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
    calls.append((method, url, headers, params, json))
    if url.endswith('/start'):
        state['challenge'] = params['code_challenge']
        state['redirect'] = 'http://127.0.0.1:%d/oauth/%s/%s' % (params['loopback_port'], params['plugin_uuid'],
                                                                 params['connector_slug'])
        return Resp({'url': 'https://provider.example/authorize?state=x'})
    if url.endswith('/exchange'):
        digest = base64.urlsafe_b64encode(hashlib.sha256(json['code_verifier'].encode()).digest()).rstrip(b'=')
        assert digest.decode() == state['challenge'], 'PKCE mismatch'
        assert json['code'] == 'SEALED'
        return Resp({'access_token': 'AT', 'broker_refresh': 'BR'})
    if url.endswith('/refresh'):
        return Resp({'access_token': 'AT2', 'broker_refresh': json['broker_refresh']})
    return Resp({'error': 'nope'}, 404)

state = {}
real_get = requests.get
requests.request = fake_request

def browser(url):
    # The provider and broker callback, collapsed: land on the loopback redirect with a code.
    import gevent
    gevent.spawn(lambda: real_get(state['redirect'] + '?code=SEALED&plugin_uuid=x&connector_slug=y', timeout=10))

bundle = oauth.authorize('123E4567-E89B-12D3-A456-426614174000', 'my-service', log=lambda m: None, open_browser=browser)
assert bundle == {'access_token': 'AT', 'broker_refresh': 'BR'}, bundle
assert calls[0][2]['Authorization'] == 'Bearer ID_TOKEN'
assert calls[0][3]['plugin_uuid'] == '123e4567-e89b-12d3-a456-426614174000'
assert oauth.refresh('BR')['access_token'] == 'AT2'
try:
    oauth.refresh('')
    raise SystemExit('refresh without a token should fail')
except oauth.OAuthError as e:
    assert e.code == 'AUTH_REQUIRED'
print('OK')
'''


def test_authorize_round_trip_through_loopback(tmp_path):
    creds = tmp_path / 'firebase.json'
    creds.write_text(json.dumps({'id_token': 'ID_TOKEN'}))
    env = dict(os.environ, PYPKJS_FIREBASE_CREDENTIALS=str(creds), PYPKJS_APPSTORE_API_BASE='http://broker.test')
    out = subprocess.run([sys.executable, '-c', SCRIPT], env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().endswith('OK'), out.stdout + out.stderr


def test_no_account_means_auth_required():
    env = {k: v for k, v in os.environ.items() if k != 'PYPKJS_FIREBASE_CREDENTIALS'}
    out = subprocess.run([sys.executable, '-c', 'import pypkjs.plugins.hosted_oauth as o\n'
                          'try:\n o.id_token()\nexcept o.OAuthError as e:\n print(e.code)'],
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == 'AUTH_REQUIRED', out.stdout + out.stderr


def test_account_change_and_logout_are_picked_up(tmp_path):
    creds = tmp_path / 'firebase.json'
    creds.write_text(json.dumps({'id_token': 'FIRST'}))
    script = (
        'import json, os, sys\n'
        'import pypkjs.plugins.hosted_oauth as o\n'
        'path = os.environ["PYPKJS_FIREBASE_CREDENTIALS"]\n'
        'assert o.id_token() == "FIRST"\n'
        'with open(path, "w") as f: json.dump({"id_token": "SECOND"}, f)\n'
        'os.utime(path, ns=(1, 1))\n'
        'assert o.id_token() == "SECOND"\n'
        'os.remove(path)\n'
        'try:\n o.id_token(); print("still signed in")\n'
        'except o.OAuthError as e:\n print(e.code)\n'
    )
    env = dict(os.environ, PYPKJS_FIREBASE_CREDENTIALS=str(creds))
    out = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == 'AUTH_REQUIRED', out.stdout + out.stderr
