"""
storage.py
==========

Tiny, dependency-free session store for the files moving through the app.

The application is single-user and desktop-oriented ("optimize a handful of large
files quickly"), so we deliberately avoid a database. Each uploaded file gets a
random id and a folder under a temp working directory holding the original and,
once produced, the optimized copy plus the analysis. Everything lives on local
disk and is cleaned up on demand or when the process exits.

This module is the *only* place that knows where bytes live on disk, so the API
layer never builds paths by hand — it asks the store. That keeps path handling
(and the security checks around it) in one auditable spot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional
import os
import shutil
import tempfile
import threading
import uuid


@dataclass
class Session:
    """One uploaded document and everything derived from it."""

    id: str
    folder: str
    original_name: str
    original_path: str
    optimized_path: Optional[str] = None
    # Free-form bag for the analysis dict and the last result, so the API can
    # stash and retrieve them without a second store.
    meta: Dict = field(default_factory=dict)


class SessionStore:
    """Thread-safe in-memory registry of :class:`Session` objects.

    A lock guards the dict because Uvicorn may serve requests on multiple worker
    threads; the critical sections are tiny so contention is a non-issue.
    """

    def __init__(self, root: Optional[str] = None):
        # All working files live under one root we control, created lazily.
        self._root = root or os.path.join(tempfile.gettempdir(), "pdf_optimizer")
        os.makedirs(self._root, exist_ok=True)
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, original_name: str, data: bytes) -> Session:
        """Persist an uploaded file and return its session.

        The original filename is used only for display and the eventual download
        name; the on-disk name is fixed (``original.pdf``) so a hostile filename
        can never escape the session folder.
        """
        sid = uuid.uuid4().hex
        folder = os.path.join(self._root, sid)
        os.makedirs(folder, exist_ok=True)
        original_path = os.path.join(folder, "original.pdf")
        with open(original_path, "wb") as f:
            f.write(data)
        session = Session(
            id=sid,
            folder=folder,
            original_name=_safe_display_name(original_name),
            original_path=original_path,
        )
        with self._lock:
            self._sessions[sid] = session
        return session

    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(sid)

    def optimized_target_path(self, session: Session) -> str:
        """Canonical path the optimizer should write its output to."""
        return os.path.join(session.folder, "optimized.pdf")

    def delete(self, sid: str) -> bool:
        """Remove a session and its files from disk."""
        with self._lock:
            session = self._sessions.pop(sid, None)
        if not session:
            return False
        shutil.rmtree(session.folder, ignore_errors=True)
        return True

    def cleanup_all(self) -> None:
        """Wipe the entire working directory. Called on shutdown."""
        with self._lock:
            self._sessions.clear()
        shutil.rmtree(self._root, ignore_errors=True)
        os.makedirs(self._root, exist_ok=True)


def _safe_display_name(name: str) -> str:
    """Strip any path components from an uploaded filename, keep the basename.

    Defence in depth: even though we never use this name to build a path, we
    normalize it so it can't carry directory traversal into logs or the download
    header.
    """
    base = os.path.basename(name or "document.pdf")
    return base or "document.pdf"
