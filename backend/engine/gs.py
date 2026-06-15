"""
gs.py
=====

Thin, defensive wrapper around the optional Ghostscript dependency.

Ghostscript is *not required* for the application to work — the PyMuPDF/Pillow
pipeline in :mod:`optimizer` is the primary engine. Ghostscript is offered only
as an opt-in "final distillation" that can occasionally squeeze out structural
bytes the primary pass leaves behind (it rebuilds the PDF with its own
object-stream and cross-reference packing).

Everything here is written to *fail safe*: if Ghostscript is missing, errors, or
produces a worse or structurally-different file, the original output is kept
untouched. The function contract is "never make things worse".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import fitz  # PyMuPDF, used only to verify the distilled file is intact


def available() -> bool:
    """Return True if a Ghostscript binary is on PATH.

    The UI uses this to grey out the "final distillation" option when the binary
    isn't installed, so the feature is never offered when it can't run.
    """
    return shutil.which("gs") is not None


def try_shrink(path: str, expected_pages: int, timeout: int = 600) -> bool:
    """Attempt an in-place Ghostscript distillation of ``path``.

    The distilled file replaces ``path`` *only if* it is (a) produced without
    error, (b) structurally valid with the expected page count, and (c) strictly
    smaller than the current file. Otherwise ``path`` is left exactly as it was.

    We intentionally disable Ghostscript's own image downsampling here
    (``-dDownsample*Images=false``). Our Pillow pass has already handled images
    with DPI-aware precision; letting Ghostscript re-touch them would risk a
    second lossy generation for little gain. This pass is about *structure*.

    Returns
    -------
    bool
        True if the distilled file was adopted, False if the original was kept.
    """
    if not available():
        return False

    fd, tmp = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        cmd = [
            "gs",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.7",
            "-dPDFSETTINGS=/default",
            # Leave already-optimized images alone; only repack structure.
            "-dDownsampleColorImages=false",
            "-dDownsampleGrayImages=false",
            "-dDownsampleMonoImages=false",
            "-dDetectDuplicateImages=true",
            "-dNOPAUSE",
            "-dQUIET",
            "-dBATCH",
            f"-sOutputFile={tmp}",
            path,
        ]
        subprocess.run(cmd, check=True, timeout=timeout)

        # Validate: must open, must have the right page count.
        try:
            d = fitz.open(tmp)
            ok_pages = d.page_count == expected_pages
            d.close()
        except Exception:
            return False
        if not ok_pages:
            return False

        # Only adopt if it actually helped.
        if os.path.getsize(tmp) < os.path.getsize(path):
            shutil.move(tmp, path)
            return True
        return False
    except Exception:
        # Any failure (missing binary mid-run, timeout, malformed output) leaves
        # the original file in place. The caller treats False as "no GS used".
        return False
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
