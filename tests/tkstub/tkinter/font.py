"""Font objects, inert."""

from __future__ import annotations

NORMAL, BOLD, ITALIC, ROMAN = "normal", "bold", "italic", "roman"


class Font:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def measure(self, *args, **kwargs) -> int:
        return 0

    def metrics(self, *args, **kwargs) -> dict:
        return {}


def families(*args, **kwargs) -> tuple:
    return ()


def nametofont(*args, **kwargs) -> Font:
    return Font()
