"""Google Drive provisioning for the optional Sheets mirror.

Creates a folder per device under the project folder and copies the master
spreadsheet template once per frequency, recording spreadsheet IDs in the
local mapping file. All functions take pre-built service objects — nothing
here authenticates or runs at import time.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from gan_fet.core.models import freq_label
from gan_fet.settings import GoogleSettings

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


def load_mappings(settings: GoogleSettings) -> dict[str, str]:
    path = Path(settings.mapping_file)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("Mapping file %s is corrupt; starting empty", path)
    return {}


def save_mappings(settings: GoogleSettings, mappings: dict[str, str]) -> None:
    Path(settings.mapping_file).write_text(
        json.dumps(mappings, indent=4), encoding="utf-8"
    )


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
    mappings = load_mappings(settings)

    folder_id = _find(drive_service, device_name, settings.project_folder_id, FOLDER_MIME)
    if not folder_id:
        folder_id = _create_folder(drive_service, device_name, settings.project_folder_id)
        log.info("Created Drive folder '%s' (%s)", device_name, folder_id)

    changed = False
    for freq in frequencies_hz:
        label = freq_label(freq)
        key = f"{device_name}_{label}"
        if key not in mappings:
            mappings[key] = _copy_master_spreadsheet(
                drive_service, settings, f"{device_name} {label}", folder_id
            )
            changed = True

    if changed:
        save_mappings(settings, mappings)
    return mappings
