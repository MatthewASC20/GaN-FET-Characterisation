"""Parameter option sets: normalisation, labelling and (de)serialisation.

Pure logic, deliberately free of tkinter and of any database dependency so it
can be unit-tested directly. An "option" is a ``(value, label)`` pair; a
device's option set is a mapping from an ``OPTION_KEYS`` name to a list of
those pairs.

Stored option lists come from two places and must both round-trip:
* the database (``devices.param_options_json``), written by this app;
* legacy ``device_config.json`` files imported from v1, which used the same
  ``[[value, label], ...]`` shape.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from gan_fet.core.models import freq_label

OPTION_KEYS = ("configurations", "frequencies", "duties", "temperatures", "voltages")

#: Keys whose values are integers; "configurations" holds strings instead.
_NUMERIC_KEYS = frozenset(OPTION_KEYS) - {"configurations"}

Option = tuple[Any, str]


def is_numeric_key(key: str) -> bool:
    return key in _NUMERIC_KEYS


def default_label(key: str, value: Any) -> str:
    """Human label for a bare option value."""
    if key == "frequencies":
        return freq_label(value)
    if key == "duties":
        return f"{value}%"
    if key == "temperatures":
        return f"{value}°C"
    if key == "voltages":
        return f"{value}V"
    return str(value)


def parse_value(key: str, raw: str) -> Any:
    """Parse user-entered text for `key`. Raises ValueError when unusable."""
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("value is empty")
    if not is_numeric_key(key):
        return cleaned
    cleaned = cleaned.replace(",", "")
    if any(ch in cleaned.lower() for ch in ("e", ".")):
        return int(float(cleaned))
    return int(cleaned, 10)


def _coerce_entry(entry: Any) -> tuple[Any, str]:
    """Unpack one stored entry into (value, label); label may be blank."""
    if isinstance(entry, Mapping):
        return entry.get("value"), str(entry.get("label", ""))
    if isinstance(entry, (list, tuple)):
        if not entry:
            return None, ""
        return entry[0], str(entry[1]) if len(entry) > 1 else ""
    return entry, ""


def normalize(key: str, stored: Optional[Iterable[Any]]) -> list[Option]:
    """Clean a stored option list: coerce types, fill labels, drop duplicates
    and unusable entries, then sort. Returns [] when nothing survives, which
    callers treat as "fall back to defaults"."""
    if not stored:
        return []

    cleaned: list[Option] = []
    seen: set[Any] = set()

    for entry in stored:
        value, label = _coerce_entry(entry)
        if value is None:
            continue

        if is_numeric_key(key):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        else:
            value = str(value).strip()
            if not value:
                continue

        if value in seen:
            continue
        seen.add(value)
        cleaned.append((value, label.strip() or default_label(key, value)))

    return sort_options(key, cleaned)


def sort_options(key: str, options: Sequence[Option]) -> list[Option]:
    """Numeric keys sort by value; configurations sort by label."""
    if is_numeric_key(key):
        return sorted(options, key=lambda item: item[0])
    return sorted(options, key=lambda item: item[1].lower())


def merge(options: Sequence[Option], value: Any, label: str) -> list[Option]:
    """Add or replace one option, preserving the rest."""
    remaining = [(v, l) for v, l in options if v != value]
    return [*remaining, (value, label)]


def serialize(options: Mapping[str, Sequence[Option]]) -> dict[str, list[list[Any]]]:
    """JSON-friendly form, matching the legacy device_config.json shape."""
    return {key: [[value, label] for value, label in entries]
            for key, entries in options.items()}


def deserialize(
    stored: Optional[Mapping[str, Any]],
    defaults: Mapping[str, Sequence[Option]],
) -> dict[str, list[Option]]:
    """Full option set from storage, falling back per-key to `defaults`."""
    stored = stored or {}
    result: dict[str, list[Option]] = {}
    for key in OPTION_KEYS:
        normalized = normalize(key, stored.get(key))
        result[key] = normalized or [tuple(o) for o in defaults.get(key, [])]
    return result


def defaults_from_values(
    values_by_key: Mapping[str, Sequence[Any]],
) -> dict[str, list[Option]]:
    """Build labelled defaults from the bare value lists held in settings."""
    return {
        key: [(value, default_label(key, value)) for value in values]
        for key, values in values_by_key.items()
    }


def values(options: Sequence[Option]) -> list[Any]:
    return [value for value, _label in options]
