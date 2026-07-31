"""Force a non-interactive matplotlib backend before any figure is created.

Instrument figures are rendered from processing worker threads. macOS selects
the ``macosx`` GUI backend by default — Tk is present for the native folder
dialogs — and creating a figure with it off the main thread raises

    Cannot create a GUI FigureManager outside the main thread using the
    MacOS backend. Use a non-interactive backend like 'agg'.

Several bundled legacy modules pin ``Agg`` themselves, but ``opc_n3_quicklook``
and ``gremsy_full_flight_quicklook`` do not, and ``FLIR_Quick_look`` pins it
only inside a function, by which point ``pyplot`` may already be imported.
Relying on one adapter being imported first is not a guarantee, so every legacy
bridge calls this before loading its module.

Safe to call repeatedly and from any thread; it is a no-op once ``Agg`` is set.
"""

from __future__ import annotations

HEADLESS_BACKEND = "Agg"


def use_headless_backend() -> str | None:
    """Select the non-interactive backend. Returns the active backend name.

    Returns ``None`` when matplotlib is not installed, so callers that do not
    plot are unaffected — matplotlib is an optional instrument dependency.
    """
    try:
        import matplotlib
    except ImportError:
        return None
    if matplotlib.get_backend().casefold() != HEADLESS_BACKEND.casefold():
        # force=True also closes any figure created under a GUI backend, which
        # is what makes this safe to call after an accidental early import.
        matplotlib.use(HEADLESS_BACKEND, force=True)
    return matplotlib.get_backend()
