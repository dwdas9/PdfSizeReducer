"""
app.py
======

The HTTP layer. It is intentionally thin: every endpoint validates input, calls
into the framework-agnostic :mod:`engine`, and serializes the result. No
compression logic lives here — that separation is what lets the engine be tested
and reused independently of FastAPI.

Endpoints
---------
* ``GET  /``                      — serve the single-page UI.
* ``GET  /api/health``            — liveness + whether Ghostscript is available.
* ``GET  /api/profiles``          — the named profiles and their parameters.
* ``POST /api/upload``            — accept one or more PDFs; analyze + recommend.
* ``POST /api/estimate``          — predict output size for a configuration.
* ``POST /api/optimize``          — run a real optimization; store the result.
* ``GET  /api/file/{sid}/{which}``— stream the original/optimized PDF (preview).
* ``GET  /api/download/{sid}``    — download the optimized PDF.
* ``DELETE /api/session/{sid}``   — discard a session and its files.

State lives in a single :class:`~storage.SessionStore`; see that module for the
on-disk layout and the (deliberate) absence of a database.
"""

from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine import (
    analyze,
    recommend,
    estimate,
    optimize,
    get_profile,
    list_profiles,
    OptimizeParams,
    gs,
)
from engine.targeting import plan_for_target, optimize_to_target
from storage import SessionStore, Session
from models import EstimateRequest, OptimizeRequest, CustomParams


# Paths are resolved relative to this file so the app runs from any working dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
_FRONTEND_DIR = os.path.normpath(os.path.join(_HERE, "..", "frontend"))

app = FastAPI(
    title="PDF Optimizer",
    description="Shrink large PDFs with precise, transparent control.",
    version="1.0.0",
)

# One process-wide store. Created at import so it survives across requests.
store = SessionStore()

# Serve CSS/JS assets. The HTML itself is served by the root route so we can
# inject nothing and keep it a plain file.
app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")


# ---------------------------------------------------------------------------
# Parameter resolution
# ---------------------------------------------------------------------------
def _resolve_params(profile: str | None, custom: CustomParams | None) -> OptimizeParams:
    """Combine a named profile with optional per-lever overrides.

    The profile supplies the baseline; any non-null field on ``custom`` wins.
    Centralising this here means /estimate and /optimize can never drift apart in
    how they interpret a request.
    """
    base = get_profile(profile or "balanced")
    if custom is None:
        return base
    overrides = {k: v for k, v in custom.model_dump().items() if v is not None}
    return base.replace(**overrides) if overrides else base


# ---------------------------------------------------------------------------
# Basic routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the application shell."""
    index_path = os.path.join(_FRONTEND_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/health")
def health() -> dict:
    """Report liveness and optional-feature availability.

    The frontend calls this once on load to decide whether to enable the
    Ghostscript distillation toggle.
    """
    return {"status": "ok", "ghostscript": gs.available()}


@app.get("/api/profiles")
def profiles() -> dict:
    """Expose the named profiles so the UI can render and seed its controls."""
    return list_profiles()


# ---------------------------------------------------------------------------
# Upload + analysis
# ---------------------------------------------------------------------------
@app.post("/api/upload")
async def upload(files: List[UploadFile] = File(...)) -> JSONResponse:
    """Accept one or more PDFs, persist them, and return analysis + recommendation.

    Each file becomes its own session so the user can optimize them
    independently with different settings. Non-PDF uploads are rejected per-file
    rather than failing the whole batch.
    """
    results = []
    for upload_file in files:
        data = await upload_file.read()

        # Cheap, robust content check: a real PDF starts with the %PDF- marker.
        # We reject obvious non-PDFs early with a clear, per-file message.
        if not data[:5] == b"%PDF-":
            results.append({
                "name": upload_file.filename,
                "error": "Not a PDF file (missing %PDF- header).",
            })
            continue

        session = store.create(upload_file.filename or "document.pdf", data)
        try:
            analysis = analyze(session.original_path)
        except Exception as exc:
            store.delete(session.id)
            results.append({
                "name": upload_file.filename,
                "error": f"Could not read PDF: {exc}",
            })
            continue

        # Cache the analysis on the session; /estimate and /optimize reuse it so
        # we never re-parse the document just to predict a size.
        session.meta["analysis"] = analysis
        rec = recommend(analysis)

        results.append({
            "session_id": session.id,
            "name": session.original_name,
            "analysis": analysis.to_dict(),
            "recommendation": rec.to_dict(),
        })

    return JSONResponse({"files": results})


# ---------------------------------------------------------------------------
# Estimate
# ---------------------------------------------------------------------------
@app.post("/api/estimate")
def estimate_endpoint(req: EstimateRequest) -> dict:
    """Predict output size for a profile, custom settings, or a target size.

    Pure prediction — no file is written — so the UI can call this freely as the
    user drags sliders. For a target-size request it returns the chosen plan so
    the UI can preview which settings will be used.
    """
    session = _require_session(req.session_id)
    analysis = session.meta["analysis"]

    if req.target_bytes is not None:
        plan = plan_for_target(analysis, req.target_bytes)
        return {"mode": "target", **plan.to_dict()}

    params = _resolve_params(req.profile, req.custom)
    est = estimate(analysis, params)
    return {"mode": "profile", "params": params.to_dict(), "estimate": est.to_dict()}


# ---------------------------------------------------------------------------
# Optimize
# ---------------------------------------------------------------------------
@app.post("/api/optimize")
def optimize_endpoint(req: OptimizeRequest) -> dict:
    """Run a real optimization and store the output for preview/download.

    Synchronous by design: this product handles a few large files, not a
    high-throughput queue, so a blocking call that returns the finished result is
    the simplest correct behaviour. (A streaming/SSE progress channel is a clean
    future extension — the optimizer already accepts a progress callback.)
    """
    session = _require_session(req.session_id)
    analysis = session.meta["analysis"]
    dst = store.optimized_target_path(session)

    try:
        if req.target_bytes is not None:
            result, used_params, met = optimize_to_target(
                session.original_path, dst, analysis, req.target_bytes
            )
            payload = {
                "result": result.to_dict(),
                "params": used_params.to_dict(),
                "target_bytes": req.target_bytes,
                "target_met": met,
            }
        else:
            params = _resolve_params(req.profile, req.custom)
            result = optimize(session.original_path, dst, params)
            payload = {"result": result.to_dict(), "params": params.to_dict()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Optimization failed: {exc}")

    session.optimized_path = dst
    session.meta["last_result"] = payload
    return payload


# ---------------------------------------------------------------------------
# File serving (preview + download)
# ---------------------------------------------------------------------------
@app.get("/api/file/{sid}/{which}")
def serve_file(sid: str, which: str) -> FileResponse:
    """Stream a session's original or optimized PDF for in-browser preview.

    ``which`` is constrained to a fixed allow-list; paths come from the store,
    never from the URL, so there is no way to request an arbitrary file.
    """
    session = _require_session(sid)
    if which == "original":
        path = session.original_path
    elif which == "optimized":
        path = session.optimized_path
    else:
        raise HTTPException(status_code=400, detail="which must be 'original' or 'optimized'.")

    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not available yet.")
    # inline so the browser's PDF viewer (and pdf.js) can render it directly.
    return FileResponse(path, media_type="application/pdf")


@app.get("/api/download/{sid}")
def download(sid: str) -> FileResponse:
    """Download the optimized PDF with a friendly, derived filename."""
    session = _require_session(sid)
    if not session.optimized_path or not os.path.exists(session.optimized_path):
        raise HTTPException(status_code=404, detail="Nothing optimized to download yet.")
    stem, _ = os.path.splitext(session.original_name)
    download_name = f"{stem}-optimized.pdf"
    return FileResponse(
        session.optimized_path,
        media_type="application/pdf",
        filename=download_name,
    )


@app.delete("/api/session/{sid}")
def delete_session(sid: str) -> dict:
    """Discard a session and remove its files from disk."""
    ok = store.delete(sid)
    if not ok:
        raise HTTPException(status_code=404, detail="No such session.")
    return {"deleted": sid}


@app.on_event("shutdown")
def _shutdown() -> None:
    """Best-effort cleanup of all working files when the server stops."""
    store.cleanup_all()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_session(sid: str) -> Session:
    """Fetch a session or raise a clean 404.

    Also guards the common case where the user reloads the page (clearing client
    state) but references a session the server has since cleaned up.
    """
    session = store.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired.")
    if "analysis" not in session.meta:
        # Re-analyze on demand if the cached analysis is somehow gone.
        session.meta["analysis"] = analyze(session.original_path)
    return session
