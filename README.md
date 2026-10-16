pypkjs
======

PebbleKit JS in Python!

Setup
-----

To get started, clone the repo:

    git clone https://github.com/coredevices/pypkjs.git

Set up a venv and install everything

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Running
-------

You'll need to get [QEMU](https://github.com/pebble/qemu) running and find some pbws.
Then invoke jskit like this:

    ./jskit.py localhost:12344 app1.pbw app2.pbw ...

It'll connect to the running QEMU instance (assuming it's serving bluetooth on port 12344,
as is the default) and provide JavaScript for the given PBWs.

You can also run it like this:

    ./phonesim.py

Then you can use the standard `pebble` command, giving `--phone localhost`, like so:

    pebble install --phone localhost --logs

Configuration Pages
-------------------

Configuration pages are supported, but the pages must be modified. We cannot catch the
`pebble://close` link, so instead we add a `return_to` query param. Your configuration
page should check for the presence of `return_to` and, if present, use it in place of
`pebble://close#`. In its absence, it should behave as before (to support the real
mobile apps).

To open a configuration page using the command-line runner (`jskit.py`), send the running
process `SIGUSR1` (e.g. using `kill -SIGUSR1 somepid`). Configuration pages are not
currently supported using the phonesim setup.

Plugins
-------

Apps can use the Core phone app's plugin API (`Pebble.subscribeToSource`,
`Pebble.invokeAction`, `Pebble.enumeratePlugins`):

* A pbw whose appinfo carries a `plugin` block (and `manifestVersion` 2 or later) has its
  plugin script run the way the phone app runs it. A pbw with only a plugin in it is
  registered, not installed on the watch. Plugin logs show up with the app's, prefixed
  with the plugin's name.
* The phone app's built-in plugins (weather, phone state, calendar, music, notifications,
  watch settings, watch state) serve fixture data. `watch/launch_app` and
  `launch_previous_app` really launch apps on the emulator.
* Every permission a source or action asks of its caller is granted.
* Requests are made as the phone app would make them, but not refused: a pbw with
  `manifestVersion` 2 or later may only reach the domains it declares in `usesPermissions`
  (`{"name": "Internet", "parameters": {"domains": [...]}}`, which also covers their subdomains)
  and needs `LocalNetwork` for the LAN. A request the phone would refuse is logged as a warning.
* `Pebble.oauth` calls fail with `AUTH_REQUIRED`.
* Config pages are served with the phone app's page-side `Pebble` API
  (`Pebble.sendMessage('plugin' | 'pkjs', message)`, `ready` and `message` events), at
  `http://localhost:<port>/config/<uuid>/`. That covers a pbw's `configPage` (which, as on
  the phone, is shown instead of `showConfiguration`) and pages an app opens with
  `Pebble.openURL('data:...')`. `pebble emu-app-config --uuid <uuid>` opens the page of a
  pbw that isn't running, such as a plugin's.

To change what the built-ins serve, write a JSON file of readings keyed by source, in
the wire format the phone app sends, and pass it as `--plugin-fixtures` (or set
`PYPKJS_PLUGIN_FIXTURES`, which also works when `pebble` starts pypkjs for you). The file
is reloaded whenever it changes; sources it doesn't mention keep their defaults. An
`icon` or `image` shape may be a placeholder that's drawn at the size the app asks for:
`{"placeholder": "fill", "color": "#RRGGBB"}` is a full-colour block (framed, with a
diagonal), and
`{"placeholder": "disc", "color": "#RRGGBB", "glyph": "sun"}` a disc with a white `sun`,
`cloud`, `rain` or `snow` mark knocked out of it (omit `glyph` for a plain disc). Apps
may only notice a new image when its pixels change, so give different conditions
different glyphs.

    {
      "phone/phone_state": [
        {"instanceId": "phone", "properties": {
          "battery_level": {"numericValue": {"value": 5, "unit": "%", "min": 0, "max": 100},
                            "shortText": {"text": "5%"}}}}
      ],
      "music/track": [],
      "weather/location": [
        {"instanceId": "home", "properties": {
          "name": {"shortText": {"text": "Home"}},
          "temperature": {"shortText": {"text": "-20°C"}},
          "condition": {"shortText": {"text": "Heavy snow"},
                        "image": {"placeholder": "disc", "color": "#00AAFF", "glyph": "snow"}}}}
      ]
    }

Plugins can also be managed over the phone websocket (`pebble plugins` uses this). Each command
is `0x0d` followed by a command byte; the reply is `0x0d 0x00` (followed by a JSON result for
commands that have one) on success, and `0x0d 0x01` followed by an error message on failure.

* `0x01` + `{"source": "<category>/<item>", "instances": [...]}`: change a built-in source's
  readings live (`"instances": null` restores the default).
* `0x02`: list every registered plugin: `[{"uuid", "name", "kind", "sources", "actions"}]`, where
  `kind` is `builtin`, `plugin` (a pbw with only a plugin) or `app` (a watchapp's plugin).
* `0x03` + uuid: remove a plugin-only pbw, including from the app cache it's reloaded from at
  startup. A watchapp's plugin goes with the app, so it can't be removed this way.

Platforms
-------

Mac OS and Linux are supported. Windows won't work.
