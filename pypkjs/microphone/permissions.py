"""Who may use the watch microphone, and keeping the watch's copy of that up to date.

The phone app asks the user; pypkjs grants the microphone to every app that declares it, as if the
user had allowed it, and denies it to the rest, so an app that forgets to declare it finds out in
the emulator. `pebble emu-permission` overrides that per app, for trying out a refusal or a
revocation.

The watch keeps its own copy, for local capture: a u32 the phone appends to the app's AppDB entry
(bit 0: microphone), sent only to firmware that advertises app_permissions_support.
"""

import json
import logging
import os
import struct
from uuid import UUID

from libpebble2.protocol.blobdb import BlobDatabaseID, BlobStatus

logger = logging.getLogger('pypkjs.microphone')

WATCH_MICROPHONE = 'WatchMicrophone'
APP_PERMISSIONS_CAPABILITY = 1 << 27
APP_PERMISSION_MICROPHONE = 1 << 0
LEGACY_APP_ENTRY_SIZE = 126

GRANT, DENY = 'grant', 'deny'


def declares_microphone(manifest):
    """Whether the phone app would offer this app the microphone at all: only a manifestVersion 2
    app's usesPermissions counts."""
    if int(manifest.get('manifestVersion', 0) or 0) < 2:
        return False
    for permission in manifest.get('usesPermissions') or []:
        name = permission if isinstance(permission, str) else (permission or {}).get('name')
        if name == WATCH_MICROPHONE:
            return True
    return False


class MicrophonePermissions(object):
    def __init__(self, runner):
        self.runner = runner
        # uuid -> {'override': GRANT|DENY, 'synced': the grant the watch holds (None: unknown),
        #         'metadata': hex of the app's legacy AppDB entry, for apps pypkjs installed}
        self.apps = self._load()

    @property
    def _file(self):
        persist = self.runner.persist_dir
        return os.path.join(persist, 'microphone.json') if persist is not None else None

    def _load(self):
        try:
            with open(self._file) as f:
                return {UUID(k): v for k, v in json.load(f).items()}
        except (TypeError, OSError, ValueError):
            return {}

    def _save(self):
        if self._file is None:
            return
        with open(self._file, 'w') as f:
            json.dump({str(k): v for k, v in self.apps.items()}, f)

    def decision(self, uuid):
        """(granted, why), why being a sentence for the developer."""
        pbw = self.runner.pbws.get(uuid)
        if pbw is None:
            return False, "the app isn't installed from this computer"
        name = self.runner.app_title(uuid)
        if not declares_microphone(pbw.manifest):
            if int(pbw.manifest.get('manifestVersion', 0) or 0) < 2:
                return False, ("%s is a manifestVersion 1 app, which the phone app never lets use the "
                               "microphone: build it with manifestVersion 2 and declare '%s' in "
                               "usesPermissions" % (name, WATCH_MICROPHONE))
            return False, ("%s doesn't declare '%s' in usesPermissions, so the phone app won't let it "
                           "use the microphone" % (name, WATCH_MICROPHONE))
        override = self.apps.get(uuid, {}).get('override')
        if override == DENY:
            return False, ("%s is denied the microphone, as by the user (pebble emu-permission %s --reset "
                           "undoes it)" % (name, WATCH_MICROPHONE))
        return True, "%s is allowed the microphone" % name

    def granted(self, uuid):
        return self.decision(uuid)[0]

    def set_override(self, uuid, state):
        """GRANT, DENY or None (back to the default); returns decision(uuid)."""
        pbw = self.runner.pbws.get(uuid)
        if pbw is None:
            raise ValueError("%s isn't installed from this computer" % uuid)
        if state == GRANT and not declares_microphone(pbw.manifest):
            raise ValueError(self.decision(uuid)[1])
        entry = self.apps.setdefault(uuid, {})
        was_granted = self.granted(uuid)
        if state is None:
            entry.pop('override', None)
        else:
            entry['override'] = state
        self._save()
        if self.granted(uuid) != was_granted:
            self.runner.microphone.permission_changed(uuid)
        self.sync(uuid)
        return self.decision(uuid)

    def _watch_supports_permissions(self):
        try:
            return bool(self.runner.pebble.pebble.watch_info.capabilities & APP_PERMISSIONS_CAPABILITY)
        except Exception:
            return False

    def _extend(self, uuid, metadata):
        return metadata + struct.pack('<I', APP_PERMISSION_MICROPHONE if self.granted(uuid) else 0)

    def blobdb_for_install(self, blobdb):
        """A BlobDB client for libpebble2's AppInstaller that gives the app its grant before it
        first runs, and remembers the app's entry so the grant can change later without a
        reinstall.

        The extended entry goes first, then the installer's own: on its own, an extended entry
        that only changes the grant would be a permission-only update, and the watch would keep
        running its old copy of the app."""
        permissions = self

        class InstallBlobDB(object):
            def insert(self, database, key, value, callback):
                if database != BlobDatabaseID.App or len(value) != LEGACY_APP_ENTRY_SIZE:
                    return blobdb.insert(database, key, value, callback)
                uuid = key if isinstance(key, UUID) else UUID(bytes=bytes(key))
                entry = permissions.apps.setdefault(uuid, {})
                entry['metadata'] = bytes(value).hex()
                entry['synced'] = None
                permissions._log_install(uuid)
                if not permissions._watch_supports_permissions():
                    permissions._save()
                    return blobdb.insert(database, key, value, callback)
                granted = permissions.granted(uuid)

                def granted_then_install(status):
                    if status != BlobStatus.Success:
                        permissions._save()
                        return callback(status)
                    entry['synced'] = granted
                    permissions._save()
                    blobdb.insert(database, key, value, callback)
                return blobdb.insert(database, key, permissions._extend(uuid, bytes(value)),
                                     granted_then_install)

            def __getattr__(self, name):
                return getattr(blobdb, name)

        return InstallBlobDB()

    def _log_install(self, uuid):
        pbw = self.runner.pbws.get(uuid)
        if pbw is not None and declares_microphone(pbw.manifest):
            self.runner.log_output("%s (pebble emu-permission %s changes it)"
                                   % (self.decision(uuid)[1], WATCH_MICROPHONE))

    def sync(self, uuid):
        """Send the watch the grant, if it changed, for an app pypkjs installed. Only the grant
        may differ from the stored entry: the watch takes anything else as a reinstall."""
        entry = self.apps.get(uuid)
        if entry is None or 'metadata' not in entry or not self._watch_supports_permissions():
            return
        granted = self.granted(uuid)
        if entry.get('synced') == granted:
            return
        value = self._extend(uuid, bytes.fromhex(entry['metadata']))

        def done(status):
            if status == BlobStatus.Success:
                entry['synced'] = granted
                self._save()
            else:
                logger.warning("Couldn't update the microphone grant of %s: %s", uuid, status)
        self.runner.pebble.blobdb.insert(BlobDatabaseID.App, uuid, value, done)
