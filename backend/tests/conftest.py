from __future__ import annotations

# aiosqlite.Connection inherits from threading.Thread without setting daemon=True.
# Tests that exercise the FastAPI app via ASGITransport never run the lifespan
# shutdown, so db.close() is not called and the non-daemon connection threads
# block process exit. Marking the thread daemon here keeps tests fast without
# changing production behavior.
import aiosqlite.core as _aiosqlite_core

_orig_aiosqlite_init = _aiosqlite_core.Connection.__init__


def _aiosqlite_connection_init(self, *args, **kwargs):
    _orig_aiosqlite_init(self, *args, **kwargs)
    self.daemon = True


_aiosqlite_core.Connection.__init__ = _aiosqlite_connection_init
