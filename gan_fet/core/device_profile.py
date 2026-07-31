"""Per-device parameter profiles: load, apply and persist option sets.

Sits between the UI and the database so the UI never has to know how a
device's options are stored. Owning the "active device" here is what stops
one device's edits being written to another's row when the selection changes
(the load/persist ordering used to live inline in the window).
"""

from __future__ import annotations

import logging
from typing import Mapping, Optional, Sequence

from gan_fet.core import param_options
from gan_fet.core.models import sanitize_device_name
from gan_fet.storage.db import Database

log = logging.getLogger(__name__)


class DeviceProfileStore:
    def __init__(
        self,
        db: Database,
        defaults: Mapping[str, Sequence[param_options.Option]],
    ):
        self.db = db
        self.defaults = {key: list(value) for key, value in defaults.items()}
        self._active_device: str = ""
        self.options: dict[str, list[param_options.Option]] = (
            param_options.deserialize(None, self.defaults)
        )

    @property
    def active_device(self) -> str:
        return self._active_device

    def values(self, key: str) -> list:
        return param_options.values(self.options.get(key, []))

    def first_value(self, key: str, fallback=None):
        options = self.options.get(key) or []
        return options[0][0] if options else fallback

    # -- device switching ------------------------------------------------

    def activate(self, device_name: str, *, persist_previous: bool = True) -> bool:
        """Make `device_name` current, loading its stored options.

        Persists the outgoing device's options first so edits are never
        attributed to the incoming one. Returns True when the active device
        actually changed.
        """
        name = device_name.strip()
        if not name or name == self._active_device:
            return False

        if persist_previous and self._active_device:
            self.persist()

        self.db.get_or_create_device(name)
        self._active_device = name
        self.options = param_options.deserialize(
            self.db.get_device_options(name), self.defaults
        )
        return True

    def reload(self) -> None:
        if self._active_device:
            self.options = param_options.deserialize(
                self.db.get_device_options(self._active_device), self.defaults
            )

    # -- editing ----------------------------------------------------------

    def set_options(
        self, key: str, options: Sequence[param_options.Option]
    ) -> Optional[list[param_options.Option]]:
        """Validate and store one option list. None means rejected (empty, or
        no device selected) and the caller should leave the UI unchanged."""
        if not self._active_device:
            return None
        normalized = param_options.normalize(key, [list(o) for o in options])
        if not normalized:
            return None
        self.options[key] = normalized
        self.persist()
        return normalized

    def persist(self) -> None:
        if not sanitize_device_name(self._active_device):
            return
        try:
            self.db.set_device_options(
                self._active_device, param_options.serialize(self.options)
            )
        except Exception:
            log.exception("could not persist options for %s", self._active_device)

    def known_devices(self) -> list[str]:
        return self.db.list_devices()
