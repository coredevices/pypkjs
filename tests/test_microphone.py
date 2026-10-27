"""Watch microphone streams to PebbleKit JS, and the microphone permission (pypkjs.microphone).

Streams run through tests/microphone_harness.py in a subprocess -- see runtime_harness.py for why
they can't run in-process.
"""
import importlib.util
import json
import os
import subprocess
import sys

HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'microphone_harness.py')
APP_PERMISSIONS = 1 << 27

# Loaded by path: importing the pypkjs package monkey-patches the interpreter for gevent.
_spec = importlib.util.spec_from_file_location(
    'microphone_permissions',
    os.path.join(os.path.dirname(__file__), '..', 'pypkjs', 'microphone', 'permissions.py'))
permissions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(permissions)

SUCCESS = '00092af8010100000004003412'
STOP = '00032710033412'


def stream(**config):
    proc = subprocess.run([sys.executable, HARNESS], input=json.dumps(config),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, "harness exited %d\n%s" % (proc.returncode, proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def refusal(result):
    return '00092af80101000000040%d3412' % result


def test_declares_microphone():
    declares = permissions.declares_microphone
    assert declares({'manifestVersion': 2, 'usesPermissions': ['WatchMicrophone']})
    assert declares({'manifestVersion': 2, 'usesPermissions': [{'name': 'WatchMicrophone'}]})
    assert not declares({'manifestVersion': 2, 'usesPermissions': ['Microphone']})
    assert not declares({'manifestVersion': 2})
    # The phone app ignores a legacy app's declarations.
    assert not declares({'manifestVersion': 1, 'usesPermissions': ['WatchMicrophone']})
    assert not declares({'usesPermissions': ['WatchMicrophone']})


def test_stream_reaches_js_as_decoded_pcm():
    result = stream(seconds=1)
    assert result['sent'] == [SUCCESS]
    logs = [l for l in result['logs'] if l.startswith(('start', 'stop'))]
    assert logs[0] == 'start 16000 pcm16le'
    # 1 s in 100 ms batches of 16-bit samples; the stop event carries no data.
    stop = logs[1].split()
    assert stop[:4] == ['stop', '10', '32000', 'false']
    # A 440 Hz tone at amplitude 8000 survives the codec.
    assert abs(int(stop[5]) - 5657) < 600


def test_partial_batch_is_flushed_before_stop():
    # 12 frames of 320 samples: two full batches, then the 1280 bytes left over.
    stop = [l for l in stream(seconds=0.24)['logs'] if l.startswith('stop')][0].split()
    assert stop[1:3] == ['3', '7680']


def test_undeclared_app_is_refused():
    result = stream(manifest={'usesPermissions': []})
    assert result['sent'] == [refusal(5)]
    assert any("doesn't declare 'WatchMicrophone'" in l for l in result['logs'])
    assert not any(l.startswith('start') for l in result['logs'])


def test_legacy_app_is_refused():
    result = stream(manifest={'manifestVersion': 1})
    assert result['sent'] == [refusal(5)]
    assert any('manifestVersion 1' in l for l in result['logs'])


def test_denied_app_is_refused():
    result = stream(override='deny')
    assert result['sent'] == [refusal(5)]
    assert any('denied the microphone' in l for l in result['logs'])


def test_app_without_js_is_refused():
    result = stream(js=False)
    assert result['sent'] == [refusal(1)]
    assert any('no PebbleKit JS' in l for l in result['logs'])


def test_js_stopping_ends_the_stream():
    result = stream(seconds=1, stop_js_after=0.5)
    assert result['sent'] == [SUCCESS, STOP]


def test_revoking_ends_the_stream_and_updates_the_watch():
    result = stream(seconds=1, revoke_after=0.5, install=True, capabilities=APP_PERMISSIONS)
    assert result['sent'] == [SUCCESS, STOP]
    assert any('lost the microphone' in l for l in result['logs'])
    grant, install, update = result['blobdb']
    assert grant[2][-8:] == '01000000'
    # Only the grant changes, so the watch doesn't take it as a reinstall.
    assert update[2] == grant[2][:-8] + '00000000'


def test_install_grants_before_installing():
    grant, install = stream(install=True, capabilities=APP_PERMISSIONS)['blobdb']
    assert grant[0] == install[0] == 2 and grant[1] == install[1] == 'aaaaaaaabbbbccccddddeeeeeeeeeeee'
    assert len(grant[2]) == 130 * 2 and grant[2].endswith('01000000')
    # Then the installer's own entry, which the watch always takes as a (re)install: after a
    # grant-only update alone, it would keep running the old build.
    assert install[2] == grant[2][:-8]


def test_install_denies_an_undeclared_app():
    grant, _ = stream(install=True, capabilities=APP_PERMISSIONS,
                      manifest={'usesPermissions': []})['blobdb']
    assert len(grant[2]) == 130 * 2 and grant[2].endswith('00000000')


def test_install_on_firmware_without_app_permissions_is_legacy():
    [(_, _, value)] = stream(install=True)['blobdb']
    assert len(value) == 126 * 2
