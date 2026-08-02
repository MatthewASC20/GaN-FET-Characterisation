"""Google Drive provisioning for the optional Sheets mirror.

Creates a folder per device under the project folder and copies the master
spreadsheet template once per frequency, recording spreadsheet IDs in the
local mapping file. All functions take pre-built service objects — nothing
here authenticates or runs at import time.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from gan_fet.core.models import freq_label
from gan_fet.settings import GoogleSettings

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
_MAPPINGS_LOCK = threading.RLock()


def mapping_key(device_name: str, frequency_hz: int) -> str:
    """Collision-free mapping key; JSON encoding keeps device boundaries exact."""
    return json.dumps(
        [device_name, int(frequency_hz)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def frequency_title(frequency_hz: int) -> str:
    """Human title which remains exact for non-integer-MHz frequencies."""
    frequency_hz = int(frequency_hz)
    if frequency_hz % 1_000_000 == 0:
        return f"{frequency_hz // 1_000_000}MHz"
    return f"{frequency_hz}Hz"


def load_mappings(settings: GoogleSettings) -> dict[str, str]:
    path = Path(settings.mapping_file)
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in payload.items()
            ):
                raise ValueError("expected an object containing string IDs")
            return payload
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("Mapping file %s is invalid; starting empty: %s", path, exc)
    return {}


def save_mappings(settings: GoogleSettings, mappings: dict[str, str]) -> None:
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in mappings.items()
    ):
        raise ValueError("mapping keys and spreadsheet IDs must be strings")
    path = Path(settings.mapping_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
        encoding="utf-8",
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(mappings, handle, indent=4, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _escape(name: str) -> str:
    return name.replace("'", "\\'")


def _find(drive_service, name: str, parent_id: str, mime: str) -> str | None:
    query = (
        f"mimeType='{mime}' and name='{_escape(name)}' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    results = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def _create_folder(drive_service, name: str, parent_id: str) -> str:
    folder = drive_service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return folder["id"]


def _copy_master_spreadsheet(
    drive_service, settings: GoogleSettings, title: str, dest_folder_id: str
) -> str:
    existing = _find(drive_service, title, dest_folder_id, SPREADSHEET_MIME)
    if existing:
        return existing
    copied = drive_service.files().copy(
        fileId=settings.master_spreadsheet_id,
        body={"name": title, "parents": [dest_folder_id]},
        fields="id",
        supportsAllDrives=True,
    ).execute()
    log.info("Created spreadsheet '%s' (%s)", title, copied["id"])
    return copied["id"]


def ensure_device_setup(
    drive_service,
    settings: GoogleSettings,
    device_name: str,
    frequencies_hz: list[int],
) -> dict[str, str]:
    """Ensure folder + per-frequency spreadsheets exist; returns updated mappings."""
    with _MAPPINGS_LOCK:
        mappings = load_mappings(settings)

        folder_id = _find(
            drive_service, device_name, settings.project_folder_id, FOLDER_MIME
        )
        if not folder_id:
            folder_id = _create_folder(
                drive_service, device_name, settings.project_folder_id
            )
            log.info("Created Drive folder '%s' (%s)", device_name, folder_id)

        changed = False
        for freq in dict.fromkeys(int(value) for value in frequencies_hz):
            key = mapping_key(device_name, freq)
            if key in mappings:
                continue

            # Promote the legacy whole-MHz mapping without provisioning a
            # duplicate workbook. Never use it for fractional-MHz frequencies.
            legacy_key = f"{device_name}_{freq_label(freq)}"
            if freq % 1_000_000 == 0 and legacy_key in mappings:
                mappings[key] = mappings[legacy_key]
            else:
                label = frequency_title(freq)
                mappings[key] = _copy_master_spreadsheet(
                    drive_service, settings, f"{device_name} {label}", folder_id
                )
            changed = True

        if changed:
            save_mappings(settings, mappings)
        return mappings
