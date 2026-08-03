"""Self-contained regions of the main window.

Each panel owns its widgets *and* their refresh, so construction and update
travel together. Splitting those apart across a window class is what produced
the tangle these modules exist to undo.
"""
