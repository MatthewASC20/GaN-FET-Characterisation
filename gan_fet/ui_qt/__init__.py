"""PyQt6 front-end, built in parallel with ``gan_fet.ui`` (tkinter).

Launched with ``gan-fet --qt``. Until the operations spine is ported, the
composition root only offers this window under ``--simulate``; the Tk
interface remains the bench UI. When the Qt front-end reaches parity the flag
flips and ``gan_fet.ui`` is retired.

PyQt6 is GPLv3-licensed; installing the ``[qt]`` extra places the combined
work under GPLv3.
"""
