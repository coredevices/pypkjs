"""Tests for the fixture-backed stand-ins for the phone app's built-in plugins."""
import base64
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, 'plugin_harness.py')

# Loaded by path: importing the pypkjs package monkey-patches the interpreter for gevent.
_spec = importlib.util.spec_from_file_location(
    'plugin_images', os.path.join(HERE, '..', 'pypkjs', 'plugins', 'images.py'))
images = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(images)

MUSIC = '00000000-0000-0000-0001-000000000003'
WATCH_SETTINGS = '00000000-0000-0000-0001-000000000006'
WATCH_STATE = '00000000-0000-0000-0001-000000000007'


def harness(src, secs=1.0, fixtures=None, ws_commands=()):
    config = {'src': src, 'pbws': [], 'secs': secs, 'persist': None, 'fixtures': fixtures,
              'ws_commands': list(ws_commands)}
    return subprocess.Popen([sys.executable, HARNESS], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True), json.dumps(config)


def run_js(src, **kwargs):
    proc, config = harness(src, **kwargs)
    out, err = proc.communicate(config, timeout=60)
    assert proc.returncode == 0, "harness exited %d\n%s" % (proc.returncode, err)
    return json.loads(out.strip().splitlines()[-1])


def tagged(logs, tag):
    return [json.loads(line[len(tag) + 1:]) for line in logs if line.startswith(tag + ' ')]


def subscribe(category, item, extra=''):
    return """
        Pebble.subscribeToSource({ category: '%s', item: '%s', %s
            onData: function (e) { console.log('D', JSON.stringify(e)); },
            onError: function (e) { console.log('E', JSON.stringify(e)); } });
    """ % (category, item, extra)


def invoke(plugin, action, args, tag='R'):
    return """
        Pebble.invokeAction({ plugin: '%s', action: '%s', args: %s })
            .then(function (r) { console.log('%s', JSON.stringify(r)); });
    """ % (plugin, action, json.dumps(args), tag)


# ---------------------------------------------------------------------------- images


def test_image_rows_pack_even_x_into_the_high_nibble():
    image = images.encode(3, 2, [0xC0, 0xFF], lambda x, y: (x + y) % 2)
    assert base64.b64decode(image['pixels']) == bytes([0x01, 0x00, 0x10, 0x10])
    assert base64.b64decode(image['palette']) == bytes([0xC0, 0xFF])
    assert (image['width'], image['height']) == (3, 2)


def test_gcolor8():
    assert images.gcolor8('#FFAA00') == 0xF8
    assert images.gcolor8('#000000') == 0xC0
    assert images.gcolor8('#FFFFFF') == 0xFF


def test_disc_placeholder_is_square_at_the_smaller_side_with_transparent_last():
    image = images.render({'placeholder': 'disc', 'color': '#0055FF'}, {'w': 30, 'h': 20})
    assert (image['width'], image['height']) == (20, 20)
    assert base64.b64decode(image['palette']) == bytes([0xC7, 0xFF, 0x00])
    assert len(base64.b64decode(image['pixels'])) == 10 * 20


def test_glyphs_tell_conditions_apart_by_pixels():
    # Faces may only resend an image when its pixels change.
    pixels = {glyph: images.render({'placeholder': 'disc', 'color': '#000000', 'glyph': glyph}, {'w': 26, 'h': 26})
              ['pixels'] for glyph in (None, 'sun', 'cloud', 'rain', 'snow')}
    assert len(set(pixels.values())) == 5


def test_fill_placeholder_covers_the_requested_size_in_full_colour():
    image = images.render({'placeholder': 'fill', 'color': '#FF5500'}, {'w': 5, 'h': 3})
    assert (image['width'], image['height']) == (5, 3)
    assert len(base64.b64decode(image['pixels'])) == 3 * 3
    # Apps take a palette of two entries or fewer for a monochrome glyph.
    assert len(base64.b64decode(image['palette'])) > 2


# ---------------------------------------------------------------------------- sources


def test_builtins_enumerate_in_the_apps_order():
    logs = run_js("console.log('P', JSON.stringify(Pebble.enumeratePlugins().map(function (p) { return p.name; })));",
                  secs=0.3)
    assert tagged(logs, 'P') == [['Phone', 'Calendar', 'Watch Settings', 'Watch State', 'Music', 'Notifications',
                                  'Weather']]


def test_weather_declarations_match_the_app():
    logs = run_js("""
        var weather = Pebble.enumeratePlugins().filter(function (p) { return p.name === 'Weather'; })[0];
        console.log('W', JSON.stringify(weather.sources[1]));
    """, secs=0.3)
    assert tagged(logs, 'W') == [{
        'category': 'weather', 'items': ['hour'],
        'properties': {'temperature': ['numericValue', 'shortText'], 'condition': ['shortText', 'icon', 'image'],
                       'condition_code': ['longText']},
        'supportsMultiple': True, 'callerPermissions': [{'name': 'Location'}], 'suggestedRefreshIntervalSec': 900,
    }]


def test_no_bitmaps_without_icon_pixel_size():
    envelope = tagged(run_js(subscribe('weather', 'location')), 'D')[0]
    assert envelope['pluginUuid'] == '00000000-0000-0000-0001-000000000002'
    assert set(envelope['instances'][0]['properties']['condition']) == {'shortText', 'longText'}


def test_bitmaps_drawn_at_the_requested_size():
    envelope = tagged(run_js(subscribe('weather', 'location', "iconPixelSize: { w: 30, h: 20 },")), 'D')[0]
    condition = envelope['instances'][0]['properties']['condition']
    assert (condition['image']['width'], condition['image']['height']) == (20, 20)
    assert (condition['icon']['width'], condition['icon']['height']) == (20, 20)


def test_property_with_only_bitmaps_is_dropped_without_icon_pixel_size():
    envelope = tagged(run_js(subscribe('music', 'track')), 'D')[0]
    assert 'artwork' not in envelope['instances'][0]['properties']


# ---------------------------------------------------------------------------- actions


def test_music_set_playing_refreshes_the_track():
    logs = run_js(subscribe('music', 'track') + """
        setTimeout(function () { %s }, 300);
    """ % invoke(MUSIC, 'set_playing', {'on': False}))
    assert tagged(logs, 'R') == [{'ok': True, 'text': 'Paused.', 'refreshed': ['music/track']}]
    playing = [e['instances'][0]['properties']['playing']['boolean']['value'] for e in tagged(logs, 'D')]
    assert playing == [True, False]


def test_music_next_track():
    logs = run_js(subscribe('music', 'track') + "setTimeout(function () { %s }, 300);"
                  % invoke(MUSIC, 'next_track', {}))
    titles = [e['instances'][0]['properties']['title']['shortText']['text'] for e in tagged(logs, 'D')]
    assert titles == ['Pebble Song', 'Tick Tock']


@pytest.mark.parametrize('action, args, expected', [
    ('set_on', {'instanceId': 'clock24h', 'on': True},
     {'ok': True, 'text': '24h clock: On', 'refreshed': ['watch/switch_setting']}),
    ('set_on', {'instanceId': 'lightTimeoutMs', 'on': True},
     {'ok': False, 'code': 'INVALID_ARGS', 'message': 'Backlight Timeout is not a switch'}),
    ('set_on', {'instanceId': 'clock24h', 'on': 'yes'},
     {'ok': False, 'code': 'INVALID_ARGS', 'message': "'on' must be a boolean"}),
    ('set_on', {'instanceId': 'nope', 'on': True},
     {'ok': False, 'code': 'INVALID_ARGS', 'message': "no setting called 'nope'"}),
    ('set_value', {'instanceId': 'lightTimeoutMs', 'value': '5000 ms'},
     {'ok': True, 'text': 'Backlight Timeout: 5000 ms', 'refreshed': ['watch/number_setting']}),
    ('set_value', {'instanceId': 'lightTimeoutMs', 'value': '20000'},
     {'ok': False, 'code': 'INVALID_ARGS',
      'message': "'20000' is not a value for Backlight Timeout — expected a number from 1 to 10000"}),
    ('set_value', {'instanceId': 'stationaryMode', 'value': 'off'},
     {'ok': True, 'text': 'Standby Mode: Off', 'refreshed': ['watch/switch_setting']}),
    ('set_value', {'instanceId': 'qlUp', 'value': ''},
     {'ok': True, 'text': 'Quick Launch: Hold Up: Nothing', 'refreshed': ['watch/app_setting']}),
    ('set_value', {'instanceId': 'qlUp', 'value': 'not-a-uuid'},
     {'ok': False, 'code': 'INVALID_ARGS',
      'message': "'not-a-uuid' is not a value for Quick Launch: Hold Up — expected an app uuid, or nothing"}),
])
def test_watch_settings_actions(action, args, expected):
    assert tagged(run_js(invoke(WATCH_SETTINGS, action, args), secs=0.5), 'R') == [expected]


def test_watch_setting_change_is_read_back():
    logs = run_js(subscribe('watch', 'switch_setting') + "setTimeout(function () { %s }, 300);"
                  % invoke(WATCH_SETTINGS, 'set_on', {'instanceId': 'clock24h', 'on': True}))
    clock = [next(i for i in e['instances'] if i['instanceId'] == 'clock24h')['properties']['value']
             for e in tagged(logs, 'D')]
    assert clock == [{'boolean': {'value': False}}, {'boolean': {'value': True}}]


def test_launch_app():
    logs = run_js(invoke(WATCH_STATE, 'launch_app', {'uuid': '8B1C6B0E-7D6A-4CF2-A9B2-2C3F8B1C6B0E'}), secs=0.5)
    assert 'LAUNCH 8b1c6b0e-7d6a-4cf2-a9b2-2c3f8b1c6b0e' in logs
    assert tagged(logs, 'R') == [{'ok': True, 'text': 'Opened 8b1c6b0e-7d6a-4cf2-a9b2-2c3f8b1c6b0e.'}]


def test_launch_previous_app_needs_one():
    logs = run_js(invoke(WATCH_STATE, 'launch_previous_app', {}), secs=0.5)
    assert tagged(logs, 'R') == [{'ok': False, 'code': 'INVALID_REQUEST',
                                  'message': 'no previous app since the watch connected'}]


# ---------------------------------------------------------------------------- overrides


PHONE_AT = lambda level: {'phone/phone_state': [{'instanceId': 'phone', 'properties': {
    'battery_level': {'numericValue': {'value': level, 'unit': '%', 'min': 0, 'max': 100},
                      'shortText': {'text': '%d%%' % level}}}}]}


def battery_levels(logs):
    return [e['instances'][0]['properties']['battery_level']['shortText']['text'] for e in tagged(logs, 'D')]


def test_fixtures_file_overrides_and_reloads(tmp_path):
    path = tmp_path / 'fixtures.json'
    path.write_text(json.dumps(PHONE_AT(5)))
    proc, config = harness(subscribe('phone', 'phone_state'), secs=2, fixtures=str(path))
    proc.stdin.write(config)
    proc.stdin.close()
    time.sleep(1)
    path.write_text(json.dumps(PHONE_AT(42)))
    os.utime(str(path), (time.time() + 5, time.time() + 5))
    out = proc.stdout.read()
    assert proc.wait(timeout=60) == 0, proc.stderr.read()
    logs = json.loads(out.strip().splitlines()[-1])
    assert battery_levels(logs) == ['5%', '42%']


def test_bad_fixtures_file_is_reported(tmp_path):
    path = tmp_path / 'fixtures.json'
    path.write_text('{nope')
    logs = run_js(subscribe('phone', 'phone_state'), fixtures=str(path))
    assert any(line.startswith("Couldn't load plugin fixtures from") for line in logs)
    assert battery_levels(logs) == ['87%']


def test_websocket_command_sets_and_resets_a_source():
    logs = run_js(subscribe('phone', 'phone_state'), secs=1.5, ws_commands=[
        [0.3, '\x01' + json.dumps({'source': 'phone/phone_state',
                                   'instances': PHONE_AT(12)['phone/phone_state']})],
        [0.6, '\x01' + json.dumps({'source': 'phone/phone_state', 'instances': None})],
        [0.9, '\x01{nope'],
    ])
    assert battery_levels(logs) == ['87%', '12%', '87%']
    assert [line for line in logs if line.startswith('WS ')] == ['WS 0', 'WS 0', 'WS 1']
