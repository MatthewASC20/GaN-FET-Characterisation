"""Safe, stable filenames for user-provided device labels."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def device_output_path(dest_dir: Path, device_name: str, suffix: str) -> Path:
    """Return a contained output path without trusting ``device_name`` as a path."""
    original = device_name.strip()
    stem = _UNSAFE.sub("_", original).strip(" .")
    while ".." in stem:
        stem = stem.replace("..", "_")
    if not stem:
        stem = "device"
    stem = stem[:100]
    if stem != original or original in {".", ".."} or Path(original).is_absolute():
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
        stem = f"{stem}-{digest}"

    root = Path(dest_dir).resolve()
    candidate = (root / f"{stem}_{suffix}").resolve()
    if candidate.parent != root:
        raise ValueError("output filename escaped its destination directory")
    return candidate
