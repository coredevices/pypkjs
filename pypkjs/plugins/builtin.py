"""The phone app's built-in plugins (weather, phone state, calendar, music, notifications, watch
settings, watch state), serving fixture data instead of the phone's.

Declarations match the app's. Readings come from a FixtureStore: defaults below, overridden per
source by a fixtures file or the websocket, and changed by the plugins' own actions.

Fixture instances are in the wire format (see the app's Plugin.kt). An `icon` or `image` shape may
be a placeholder, {"placeholder": "disc"|"fill", "color": "#RRGGBB", "glyph": ...}, drawn at the
size the subscriber asks for (see images.render). As in the app, no bitmap shapes are sent to a subscriber that didn't set
iconPixelSize.
"""

import copy
import datetime
import json
import logging
import os
import uuid as uuid_mod

import gevent

from . import INVALID_ARGS, INVALID_REQUEST, PLUGIN_UNAVAILABLE, action_error
from . import images
from . import manifest as manifests
from .base import Plugin

logger = logging.getLogger('pypkjs.plugins')

BITMAP_SHAPES = ('icon', 'image')


def _source(category, items, properties, refresh=None, multiple=False, permissions=()):
    declaration = {'category': category, 'items': items, 'properties': properties,
                   'supportsMultiple': multiple,
                   'callerPermissions': [{'name': p} for p in permissions],
                   'suggestedRefreshIntervalSec': refresh or manifests.DEFAULT_REFRESH_SEC}
    return declaration


def _action(name, description, parameters, targets=(), permissions=()):
    return dict(manifests.ACTION_DEFAULTS, name=name, description=description, parameters=parameters,
                targets=list(targets), callerPermissions=[{'name': p} for p in permissions])


def _params(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required}


# ------------------------------------------------------------------------------- shape helpers

def short(text):
    return {'shortText': {'text': text}}


def short_long(text):
    return {'shortText': {'text': text}, 'longText': {'text': text}}


def numeric(value, unit=None, low=None, high=None):
    shape = {'value': float(value)}
    for key, v in (('unit', unit), ('min', low), ('max', high)):
        if v is not None:
            shape[key] = float(v) if key != 'unit' else v
    return shape


def temperature(degrees, unit='°C'):
    text = '%d%s' % (degrees, unit)
    return {'shortText': {'text': text}, 'longText': {'text': text}, 'numericValue': numeric(degrees, unit)}


def placeholder(kind, color, glyph=None):
    shape = {'placeholder': kind, 'color': color}
    if glyph is not None:
        shape['glyph'] = glyph
    return shape


# ------------------------------------------------------------------------------- weather

WEATHER_CONDITIONS = {
    # condition_code: (shortText name, disc colour as the app draws it, stand-in glyph)
    'partly_cloudy': ('Partly cloudy', '#FFAA00', 'cloud'),
    'cloudy': ('Cloudy', '#AAAAAA', 'cloud'),
    'light_snow': ('Light snow', '#00AAFF', 'snow'),
    'light_rain': ('Light rain', '#0055FF', 'rain'),
    'heavy_rain': ('Heavy rain', '#0055FF', 'rain'),
    'heavy_snow': ('Heavy snow', '#00AAFF', 'snow'),
    'sun': ('Sunny', '#FFAA00', 'sun'),
    'rain_and_snow': ('Rain and snow', '#0055FF', 'snow'),
    'unknown': ('Unknown', '#AAAAAA', None),
}


def condition(code, forecast=None):
    name, color, glyph = WEATHER_CONDITIONS[code]
    shape = {'shortText': {'text': name}}
    if forecast is not None:
        shape['longText'] = {'text': forecast}
    shape['icon'] = placeholder('disc', '#000000', glyph)
    shape['image'] = placeholder('disc', color, glyph)
    return shape


def weather_location(instance_id, name, temp, feels, high, low, code, forecast, uv=None, precip=None):
    properties = {
        'name': short(name),
        'temperature': temperature(temp),
        'feels_like': temperature(feels),
        'high': temperature(high),
        'low': temperature(low),
        'condition': condition(code, forecast),
        'condition_code': {'longText': {'text': code}},
    }
    if uv is not None:
        properties['uv_index'] = {'numericValue': numeric(uv, low=0, high=11), 'shortText': {'text': '%d' % round(uv)}}
    if precip is not None:
        properties['precipitation'] = {'numericValue': numeric(precip, '%', 0, 100),
                                       'shortText': {'text': '%d%%' % precip}}
    return {'instanceId': instance_id, 'properties': properties}


def weather_hour(index, temp, code):
    return {'instanceId': str(index), 'properties': {
        'temperature': {'numericValue': numeric(temp, '°C'), 'shortText': {'text': '%d°C' % temp}},
        'condition': condition(code),
        'condition_code': {'longText': {'text': code}},
    }}


# ------------------------------------------------------------------------------- watch settings

def switch_setting(pref_id, name, on):
    return {'instanceId': pref_id, 'properties': {'name': short(name), 'value': {'boolean': {'value': on}}}}


def number_setting(pref_id, name, value, low, high, unit):
    return {'instanceId': pref_id, 'properties': {'name': short(name),
                                                  'value': {'numericValue': numeric(value, unit or None, low, high)}}}


def text_setting(pref_id, name, value):
    return {'instanceId': pref_id, 'properties': {'name': short(name), 'value': short(value)}}


def app_setting(pref_id, name, app_uuid):
    return {'instanceId': pref_id, 'properties': {'name': short(name), 'value': {'longText': {'text': app_uuid}}}}


HEALTH_APP_UUID = '36d8c6ed-4c83-4fa1-a9e2-8f12dc941f8c'
TIMELINE_FUTURE_UUID = '79c76b48-6111-4e80-8deb-3119eebef33e'
QUIET_TIME_TOGGLE_UUID = '2220d805-cf9a-4e12-92b9-5ca778aff6bb'

SWITCH_SETTINGS = [
    ('timezoneSource', 'Timezone configured manually', False),
    ('clock24h', '24h clock', False),
    ('stationaryMode', 'Standby Mode', True),
    ('displayOrientationLeftHanded', 'Left-handed Mode', False),
    ('lightEnabled', 'Backlight', True),
    ('lightAmbientSensorEnabled', 'Ambient Light Sensor', True),
    ('lightMotion', 'Backlight Motion', True),
    ('timelineQuickViewEnabled', 'Timeline Quick View', True),
    ('dndManuallyEnabled', 'Quiet Time - Manual', False),
    ('dndSmartEnabled', 'Quiet Time - Calendar Aware', False),
    ('dndWeekdayScheduleEnabled', 'Quiet Time - Weekday Schedule', False),
    ('dndWeekendScheduleEnabled', 'Quiet Time - Weekend Schedule', False),
    ('notifDesignStyle', 'Alternative notification banner Style (B/W watches)', False),
    ('notifVibeDelay', 'Delay Notification Vibration', True),
    ('notifBacklight', 'Notifications - Backlight', True),
    ('menuScrollWrapAround', 'Menu Scrolling - Wrap Around', False),
    ('dndMotionBacklight', 'Quiet Time - Motion Backlight', True),
    ('dndAutoDismiss', 'Quiet Time - Auto Dismiss', False),
    ('musicShowVolumeControls', 'Show Volume Controls', True),
    ('musicShowProgressBar', 'Show Progress Bar', True),
    ('musicShowAlbumArt', 'Show Album Art', False),
]


# ------------------------------------------------------------------------------- defaults

MUSIC_TRACKS = [
    ('Pebble Song', 'The Watchmakers', 'Time After Time', '#FF5500'),
    ('Tick Tock', 'Second Hand', 'Movements', '#00AA55'),
    ('Lap Timer', 'Chronographs', 'Splits', '#5500AA'),
]


def music_track(index, playing=True):
    title, artist, album, color = MUSIC_TRACKS[index % len(MUSIC_TRACKS)]
    return {'instanceId': 'track', 'properties': {
        'title': short_long(title),
        'artist': short_long(artist),
        'album': short_long(album),
        'playing': {'boolean': {'value': playing}, 'shortText': {'text': 'Playing' if playing else 'Paused'}},
        'artwork': {'image': placeholder('fill', color)},
    }}


def calendar_event(index, name, starts, ends, calendar, location=None, all_day=False):
    def clock(when):
        return 'All day' if all_day else when.strftime('%H:%M')
    properties = {'name': short_long(name)}
    if location:
        properties['location'] = short_long(location)
    properties['starts_at'] = {'shortText': {'text': clock(starts)}, 'longText': {'text': clock(starts)},
                               'timestamp': {'value': int(starts.timestamp())}}
    properties['ends_at'] = {'shortText': {'text': clock(ends)}, 'timestamp': {'value': int(ends.timestamp())}}
    properties['all_day'] = {'boolean': {'value': all_day}}
    properties['calendar'] = short(calendar)
    return {'instanceId': str(index), 'properties': properties}


def default_fixtures(now=None):
    now = now or datetime.datetime.now().astimezone()
    hour = now.replace(minute=0, second=0, microsecond=0)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        'weather/location': [
            weather_location('5d6b7a2e-3c41-4b6e-9a3f-1f0c2b7d8e90', 'Palo Alto', 18, 17, 22, 11, 'partly_cloudy',
                             'Partly cloudy, high of 22°C.', uv=5, precip=10),
            weather_location('0c9e4f1a-7b2d-4e8a-b6c3-9d1e2f3a4b5c', 'London', 12, 10, 14, 8, 'light_rain',
                             'Light rain through the evening.', uv=1, precip=70),
        ],
        'weather/hour': [weather_hour(i, t, c) for i, (t, c) in enumerate([
            (18, 'partly_cloudy'), (19, 'sun'), (20, 'sun'), (19, 'cloudy'), (17, 'cloudy'), (15, 'light_rain'),
        ])],
        'phone/phone_state': [{'instanceId': 'phone', 'properties': {
            'battery_level': {'numericValue': numeric(87, '%', 0, 100), 'shortText': {'text': '87%'}},
            'cell': short('5G'),
            'wifi': {'boolean': {'value': True}, 'shortText': {'text': 'Wi-Fi'}},
            'mobile_data': {'boolean': {'value': False}, 'shortText': {'text': 'Off'}},
        }}],
        'calendar/event': [
            calendar_event(0, 'Design review', hour + datetime.timedelta(hours=1),
                           hour + datetime.timedelta(hours=2), 'Work', location='Room 4'),
            calendar_event(1, 'Lunch with Sam', hour + datetime.timedelta(hours=3),
                           hour + datetime.timedelta(hours=4), 'Personal', location='Cafe'),
            calendar_event(2, 'Holiday', today + datetime.timedelta(days=1),
                           today + datetime.timedelta(days=2), 'Personal', all_day=True),
        ],
        'music/track': [music_track(0)],
        'notifications/notification': [
            {'instanceId': '0|com.example.chat|1|null|10234', 'properties': {
                'name': short_long('Alex'), 'body': short_long('Are we still on for 6?'),
                'app': short('Chat'), 'app_icon': {'image': placeholder('fill', '#00AA55')}}},
            {'instanceId': '0|com.example.mail|7|null|10301', 'properties': {
                'name': short_long('Build passed'), 'body': short_long('main is green again.'),
                'app': short('Mail'), 'app_icon': {'image': placeholder('fill', '#0055FF')}}},
        ],
        'watch/switch_setting': [switch_setting(*s) for s in SWITCH_SETTINGS],
        'watch/number_setting': [
            number_setting('lightTimeoutMs', 'Backlight Timeout', 3000, 1, 10000, 'ms'),
            number_setting('timelineQuickViewBeforeTimeMin', 'Timeline Quick View - minutes before event',
                           10, 0, 30, 'minutes'),
            number_setting('notifWindowTimeout', 'Notification Timeout', 180000, 15000, 600000, 'ms'),
        ],
        'watch/text_setting': [
            text_setting('dndWeekdaySchedule', 'Quiet Time - Weekday Hours', '00:00-06:00'),
            text_setting('dndWeekendSchedule', 'Quiet Time - Weekend Hours', '00:00-06:00'),
        ],
        'watch/app_setting': [
            app_setting('qlUp', 'Quick Launch: Hold Up', ''),
            app_setting('qlDown', 'Quick Launch: Hold Down', ''),
            app_setting('qlSelect', 'Quick Launch: Hold Select', ''),
            app_setting('qlBack', 'Quick Launch: Hold Back', QUIET_TIME_TOGGLE_UUID),
            app_setting('qlComboBackUp', 'Quick Launch: Hold Combo Back+Up', ''),
            app_setting('qlComboUpDown', 'Quick Launch: Hold Combo Up+Down', ''),
            app_setting('qlSingleClickUp', 'Quick Launch: Tap Up', HEALTH_APP_UUID),
            app_setting('qlSingleClickDown', 'Quick Launch: Tap Down', TIMELINE_FUTURE_UUID),
        ],
    }


# ------------------------------------------------------------------------------- store

class FixtureStore(object):
    """Current readings per "<category>/<item>": defaults, then file / websocket overrides, as
    changed by actions. Changing a source refreshes its subscribers."""

    def __init__(self, path=None, log_output=None):
        self.path = path
        self._log_output = log_output or (lambda message: None)
        self._defaults = default_fixtures()
        self._current = copy.deepcopy(self._defaults)
        self._file_keys = set()
        self._mtime = None
        self._plugins = []

    def attach(self, plugin):
        self._plugins.append(plugin)

    def instances(self, key):
        return self._current.get(key, [])

    def set(self, key, instances):
        """Replace a source's readings; None puts back its default."""
        if instances is None:
            instances = copy.deepcopy(self._defaults.get(key, []))
        elif not isinstance(instances, list):
            raise ValueError('instances for %s must be a list' % key)
        self._current[key] = instances
        for plugin in self._plugins:
            plugin.refresh([key])

    def find(self, keys, instance_id):
        """(key, instance) for the instance with this id in any of these sources."""
        for key in keys:
            for instance in self._current.get(key, []):
                if instance.get('instanceId') == instance_id:
                    return key, instance
        return None, None

    def load_file(self):
        """(Re)apply the fixtures file. Sources it no longer mentions go back to their defaults."""
        if self.path is None:
            return
        try:
            self._mtime = os.path.getmtime(self.path)
            with open(self.path) as f:
                fixtures = json.load(f)
            if not isinstance(fixtures, dict):
                raise ValueError('expected an object of "<category>/<item>": [instances]')
        except (IOError, OSError, ValueError) as e:
            self._log_output("Couldn't load plugin fixtures from %s: %s" % (self.path, e))
            return
        for key in self._file_keys - set(fixtures):
            self.set(key, None)
        for key, instances in fixtures.items():
            try:
                self.set(key, instances)
            except ValueError as e:
                self._log_output("Plugin fixtures: %s" % e)
        self._file_keys = set(fixtures)
        self._log_output("Loaded plugin fixtures for %s from %s" % (', '.join(sorted(fixtures)) or 'nothing',
                                                                   self.path))

    def watch_file(self, interval=1):
        """Reload the fixtures file whenever it changes."""
        while self.path is not None:
            gevent.sleep(interval)
            try:
                mtime = os.path.getmtime(self.path)
            except OSError:
                continue
            if mtime != self._mtime:
                self.load_file()


# ------------------------------------------------------------------------------- plugins

class FixturePlugin(Plugin):
    builtin = True

    def __init__(self, uuid, name, description, sources, actions, store):
        super(FixturePlugin, self).__init__(uuid_mod.UUID(uuid), name, description, sources, actions)
        self.store = store
        store.attach(self)

    def read(self, category, item, properties, icon_pixel_size):
        instances = [_render(i, icon_pixel_size) for i in self.store.instances('%s/%s' % (category, item))]
        return {'pluginUuid': str(self.uuid), 'instances': instances}

    def invoke(self, action, args):
        handler = getattr(self, 'do_' + action, None)
        if handler is None or self.action(action) is None:
            return action_error(PLUGIN_UNAVAILABLE, 'no action %s' % action)
        return handler(args)


def _render(instance, icon_pixel_size):
    properties = {}
    for name, shapes in instance.get('properties', {}).items():
        rendered = {}
        for shape, payload in shapes.items():
            if shape in BITMAP_SHAPES:
                if icon_pixel_size is None:
                    continue
                if isinstance(payload, dict) and 'placeholder' in payload:
                    payload = images.render(payload, icon_pixel_size)
            rendered[shape] = payload
        if rendered:
            properties[name] = rendered
    return dict(instance, properties=properties)


def _ok(text, refreshed=()):
    return {'ok': True, 'text': text, 'refreshed': list(refreshed), 'code': None, 'message': None}


class MusicPlugin(FixturePlugin):
    def __init__(self, store):
        super(MusicPlugin, self).__init__(
            '00000000-0000-0000-0001-000000000003', 'Music',
            'What is playing on the phone, with play/pause and track skipping.',
            [_source('music', ['track'], {'title': ['shortText', 'longText'], 'artist': ['shortText', 'longText'],
                                          'album': ['shortText', 'longText'], 'playing': ['boolean', 'shortText'],
                                          'artwork': ['image']},
                     refresh=5, permissions=['ReadMusic'])],
            [_action('set_playing', 'Resume or pause playback.', _params({'on': {'type': 'boolean'}}, ['on']),
                     ['music/track/playing'], ['ControlMusic']),
             _action('next_track', 'Skip to the next track.', _params({}, []), ['music/track'], ['ControlMusic']),
             _action('previous_track', 'Go back to the previous track.', _params({}, []), ['music/track'],
                     ['ControlMusic'])],
            store)
        self._track = 0

    def _playing(self):
        tracks = self.store.instances('music/track')
        return bool(tracks) and tracks[0]['properties'].get('playing', {}).get('boolean', {}).get('value', False)

    def do_set_playing(self, args):
        on = args.get('on') is True
        tracks = self.store.instances('music/track')
        if tracks:
            tracks = copy.deepcopy(tracks)
            tracks[0]['properties']['playing'] = {'boolean': {'value': on},
                                                  'shortText': {'text': 'Playing' if on else 'Paused'}}
            self.store.set('music/track', tracks)
        return _ok('Playing.' if on else 'Paused.', ['music/track'])

    def do_next_track(self, args):
        self._track += 1
        self.store.set('music/track', [music_track(self._track, self._playing())])
        return _ok('Skipped.', ['music/track'])

    def do_previous_track(self, args):
        self._track -= 1
        self.store.set('music/track', [music_track(self._track, self._playing())])
        return _ok('Back a track.', ['music/track'])


WATCH_SETTING_ITEMS = ['switch_setting', 'number_setting', 'text_setting', 'app_setting']


class WatchSettingsPlugin(FixturePlugin):
    def __init__(self, store):
        values = {'switch_setting': ['boolean'], 'number_setting': ['numericValue'],
                  'text_setting': ['shortText'], 'app_setting': ['longText']}
        super(WatchSettingsPlugin, self).__init__(
            '00000000-0000-0000-0001-000000000006', 'Watch Settings',
            "The watch's own settings, readable and changeable.",
            [_source('watch', [item], {'name': ['shortText'], 'value': values[item]}, multiple=True,
                     permissions=['ReadWatchSettings']) for item in WATCH_SETTING_ITEMS],
            [_action('set_on', 'Turn a setting that is a switch on or off.',
                     _params({'instanceId': {'type': 'string', 'description': 'Id from watch/switch_setting.'},
                              'on': {'type': 'boolean'}}, ['instanceId', 'on']),
                     ['watch/switch_setting/value'], ['WriteWatchSettings']),
             _action('set_value',
                     'Set a setting to a value: `on`/`off` for a switch, one of the choices by the name it is '
                     'shown under, a number, a time range like `22:00-07:00`, or the uuid of the app a '
                     'quick-launch button should open — empty to leave the button unassigned.',
                     _params({'instanceId': {'type': 'string', 'description': 'Id from any watch source.'},
                              'value': {'type': 'string'}}, ['instanceId', 'value']),
                     ['watch/%s/value' % item for item in WATCH_SETTING_ITEMS], ['WriteWatchSettings'])],
            store)

    def _find(self, instance_id):
        key, instance = self.store.find(['watch/%s' % item for item in WATCH_SETTING_ITEMS], instance_id)
        if instance is None:
            return None, None, action_error(INVALID_ARGS, "no setting called '%s'" % instance_id)
        return key, instance, None

    def _write(self, key, instance, value_shapes, reading):
        instances = copy.deepcopy(self.store.instances(key))
        for i in instances:
            if i['instanceId'] == instance['instanceId']:
                i['properties']['value'] = value_shapes
        self.store.set(key, instances)
        name = instance['properties']['name']['shortText']['text']
        return _ok('%s: %s' % (name, reading), [key])

    def do_set_on(self, args):
        key, instance, error = self._find(args.get('instanceId'))
        if error:
            return error
        if not isinstance(args.get('on'), bool):
            return action_error(INVALID_ARGS, "'on' must be a boolean")
        if key != 'watch/switch_setting':
            return action_error(INVALID_ARGS, '%s is not a switch' % instance['properties']['name']['shortText']['text'])
        return self._write(key, instance, {'boolean': {'value': args['on']}}, 'On' if args['on'] else 'Off')

    def do_set_value(self, args):
        key, instance, error = self._find(args.get('instanceId'))
        if error:
            return error
        raw = str(args.get('value', '')).strip()
        name = instance['properties']['name']['shortText']['text']

        def bad(expected):
            return action_error(INVALID_ARGS, "'%s' is not a value for %s — expected %s" % (raw, name, expected))

        if key == 'watch/switch_setting':
            on = {'on': True, 'true': True, '1': True, 'yes': True,
                  'off': False, 'false': False, '0': False, 'no': False}.get(raw.lower())
            if on is None:
                return bad('on or off')
            return self._write(key, instance, {'boolean': {'value': on}}, 'On' if on else 'Off')
        if key == 'watch/number_setting':
            shape = dict(instance['properties']['value']['numericValue'])
            unit = shape.get('unit', '')
            number = raw[:-len(unit)].strip() if unit and raw.endswith(unit) else raw
            low, high = shape.get('min'), shape.get('max')
            try:
                value = int(number)
            except ValueError:
                return bad('a whole number')
            if (low is not None and value < low) or (high is not None and value > high):
                return bad('a number from %d to %d' % (low, high))
            shape['value'] = float(value)
            return self._write(key, instance, {'numericValue': shape}, ('%d %s' % (value, unit)).strip())
        if key == 'watch/app_setting':
            if raw:
                try:
                    raw = str(uuid_mod.UUID(raw))
                except ValueError:
                    return bad('an app uuid, or nothing')
            return self._write(key, instance, {'longText': {'text': raw}}, raw or 'Nothing')
        return self._write(key, instance, {'shortText': {'text': raw}}, raw)


class WatchStatePlugin(FixturePlugin):
    """Launches apps on the emulated watch for real."""

    def __init__(self, store, runner):
        super(WatchStatePlugin, self).__init__(
            '00000000-0000-0000-0001-000000000007', 'Watch State', 'Launch apps and watchfaces on the watch.',
            [],
            [_action('launch_app',
                     "Open an app or watchface on the watch, by its uuid. It has to be in the user's locker.",
                     _params({'uuid': {'type': 'string'}}, ['uuid']), permissions=['LaunchApps']),
             _action('launch_previous_app',
                     'Go back to the app or watchface that was running before the current one.',
                     _params({}, []), permissions=['LaunchApps'])],
            store)
        self.runner = runner

    def do_launch_app(self, args):
        try:
            app_uuid = uuid_mod.UUID(str(args.get('uuid')))
        except ValueError:
            return action_error(INVALID_ARGS, "'uuid' must be an app uuid")
        return self._launch(app_uuid)

    def do_launch_previous_app(self, args):
        if self.runner.previous_app_uuid is None:
            return action_error(INVALID_REQUEST, 'no previous app since the watch connected')
        return self._launch(self.runner.previous_app_uuid)

    def _launch(self, app_uuid):
        try:
            self.runner.launch_app(app_uuid)
        except Exception as e:
            logger.warning("launch of %s failed", app_uuid, exc_info=True)
            return action_error(PLUGIN_UNAVAILABLE, 'no watch connected (%s)' % e)
        return _ok('Opened %s.' % self.runner.app_title(app_uuid))


def builtin_plugins(store, runner):
    """In the app's registration order, which is the order source lookups fall back in."""
    return [
        FixturePlugin('00000000-0000-0000-0001-000000000001', 'Phone',
                      "The phone's battery level and network connectivity.",
                      [_source('phone', ['phone_state'], {'battery_level': ['numericValue', 'shortText'],
                                                          'cell': ['shortText'],
                                                          'wifi': ['boolean', 'shortText'],
                                                          'mobile_data': ['boolean', 'shortText']}, refresh=60)],
                      [], store),
        FixturePlugin('00000000-0000-0000-0001-000000000004', 'Calendar',
                      "Upcoming events from the phone's calendars, soonest first.",
                      [_source('calendar', ['event'], {'name': ['shortText', 'longText'],
                                                       'location': ['shortText', 'longText'],
                                                       'starts_at': ['shortText', 'longText', 'timestamp'],
                                                       'ends_at': ['shortText', 'timestamp'],
                                                       'all_day': ['boolean'],
                                                       'calendar': ['shortText']},
                               multiple=True, permissions=['Calendar'])],
                      [], store),
        WatchSettingsPlugin(store),
        WatchStatePlugin(store, runner),
        MusicPlugin(store),
        FixturePlugin('00000000-0000-0000-0001-000000000005', 'Notifications',
                      "Notifications currently in the phone's notification shade, newest first.",
                      [_source('notifications', ['notification'], {'name': ['shortText', 'longText'],
                                                                   'body': ['shortText', 'longText'],
                                                                   'app': ['shortText'], 'app_icon': ['image']},
                               refresh=30, multiple=True, permissions=['Notifications'])],
                      [], store),
        FixturePlugin('00000000-0000-0000-0001-000000000002', 'Weather',
                      "Current conditions and forecast for the user's saved weather locations.",
                      [_source('weather', ['location'], {'name': ['shortText'],
                                                         'temperature': ['shortText', 'longText', 'numericValue'],
                                                         'feels_like': ['shortText', 'longText', 'numericValue'],
                                                         'high': ['shortText', 'longText', 'numericValue'],
                                                         'low': ['shortText', 'longText', 'numericValue'],
                                                         'condition': ['shortText', 'longText', 'icon', 'image'],
                                                         'condition_code': ['longText'],
                                                         'uv_index': ['numericValue', 'shortText'],
                                                         'precipitation': ['numericValue', 'shortText']},
                               refresh=900, multiple=True, permissions=['Location']),
                       _source('weather', ['hour'], {'temperature': ['numericValue', 'shortText'],
                                                     'condition': ['shortText', 'icon', 'image'],
                                                     'condition_code': ['longText']},
                               refresh=900, multiple=True, permissions=['Location'])],
                      [], store),
    ]
