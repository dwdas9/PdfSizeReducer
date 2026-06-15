#!/usr/bin/env python3
"""
run.py
======

Single command to launch the PDF Optimizer locally::

    python run.py

It starts a Uvicorn server hosting the FastAPI app (which also serves the web
UI) and opens on http://127.0.0.1:8000 by default. Host/port can be overridden
with the HOST and PORT environment variables.

Why a launcher instead of `uvicorn backend.app:app`?
----------------------------------------------------
Two reasons: (1) it puts the ``backend`` directory on ``sys.path`` so the app's
``from engine import ...`` and ``from storage import ...`` imports resolve
whether you launch from the repo root or elsewhere; (2) it gives non-Python users
one obvious thing to run.
"""

import os
import sys

# Make the backend package importable regardless of the current directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "backend"))

import uvicorn  # noqa: E402  (import after sys.path tweak, on purpose)


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  PDF Optimizer running at  http://{host}:{port}\n")
    # We pass the app as an import string so Uvicorn can manage it cleanly; the
    # sys.path insertion above makes "app:app" resolvable.
    uvicorn.run("app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
