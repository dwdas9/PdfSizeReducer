"""
optimizer.py
============

The heart of the engine. Given a source PDF, a destination path, and an
:class:`OptimizeParams`, it produces a smaller PDF and a truthful report of what
changed and where the bytes went.

Pipeline (in order)
-------------------
1. **Image pass** — for each *unique* image, decide whether to touch it, and if
   so, downsample to the target effective DPI and re-encode (optionally to
   grayscale). The "never make it bigger" and "never destroy transparency we
   can't reproduce" rules live here; they are what separate a tool people trust
   from one that silently mangles logos.
2. **Save with structural cleanup** — PyMuPDF's garbage collection, object-stream
   compression, and dead-object removal happen at save time.
3. **Font subsetting** — re-embed only the glyphs actually used.
4. **Optional Ghostscript pass** — an opt-in final distillation that occasionally
   recovers structural bytes our pass leaves behind. Guarded so it can only ever
   *keep* a result that is both smaller and structurally intact.

Integrity guarantees
---------------------
The optimizer treats document fidelity as a hard constraint, not a preference:

* Page count is verified to be unchanged before any output is accepted.
* Images carrying transparency (soft masks) are never flattened to JPEG, because
  doing so would change how they composite onto the page.
* Any per-image re-encode that would *grow* that image is discarded in favour of
  the original stream.

These checks are cheap and they are the reason results are safe to trust.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Dict, List
import io
import os
import tempfile

import fitz  # PyMuPDF
from PIL import Image

from .profiles import OptimizeParams
from .analyzer import analyze
from . import gs


# A progress callback receives (fraction_complete: float in 0..1, message: str).
ProgressCallback = Callable[[float, str], None]


@dataclass
class OptimizeResult:
    """Truthful, after-the-fact report of a completed optimization.

    Distinct from :class:`~engine.estimator.Estimate`: those are predictions,
    these are measured facts about the file that now exists on disk.
    """

    original_size: int
    optimized_size: int
    page_count: int
    images_processed: int
    images_skipped: int
    # Measured byte categories of the *output*, for the analytics breakdown.
    optimized_image_bytes: int
    optimized_font_bytes: int
    optimized_other_bytes: int
    used_ghostscript: bool

    @property
    def saved_bytes(self) -> int:
        return max(0, self.original_size - self.optimized_size)

    @property
    def saved_pct(self) -> float:
        if self.original_size <= 0:
            return 0.0
        return round(100 * self.saved_bytes / self.original_size, 1)

    def to_dict(self) -> Dict:
        return {
            "original_size": self.original_size,
            "optimized_size": self.optimized_size,
            "saved_bytes": self.saved_bytes,
            "saved_pct": self.saved_pct,
            "page_count": self.page_count,
            "images_processed": self.images_processed,
            "images_skipped": self.images_skipped,
            "optimized_image_bytes": self.optimized_image_bytes,
            "optimized_font_bytes": self.optimized_font_bytes,
            "optimized_other_bytes": self.optimized_other_bytes,
            "used_ghostscript": self.used_ghostscript,
        }


def _noop(fraction: float, message: str) -> None:
    """Default progress sink used when the caller doesn't supply one."""
    return None


def _reencode_image(
    raw: bytes,
    components: int,
    has_alpha: bool,
    native_w: int,
    native_h: int,
    displayed_dpi: Optional[float],
    params: OptimizeParams,
) -> Optional[bytes]:
    """Produce the re-encoded bytes for one image, or ``None`` to leave it as is.

    Returning ``None`` is the explicit "don't touch this image" signal and is
    used for transparency-bearing images, lossless-only profiles, and cases where
    re-encoding wouldn't help.
    """
    # Lossless-only profile: never re-encode pixels.
    if params.jpeg_quality is None:
        return None

    # Preserve transparency: flattening a soft-masked image to JPEG would alter
    # how it composites. We choose fidelity over the marginal saving.
    if has_alpha:
        return None

    try:
        pil = Image.open(io.BytesIO(raw))
        pil.load()
    except Exception:
        return None

    # Decide the colour model of the output.
    want_gray = params.to_grayscale or components == 1
    target_mode = "L" if want_gray else "RGB"
    if pil.mode != target_mode:
        pil = pil.convert(target_mode)

    # Downsample to the target effective DPI, never upscaling.
    if params.image_dpi and displayed_dpi and displayed_dpi > params.image_dpi:
        scale = params.image_dpi / displayed_dpi
        new_w = max(1, int(native_w * scale))
        new_h = max(1, int(native_h * scale))
        if new_w < native_w:
            pil = pil.resize((new_w, new_h), Image.LANCZOS)

    out = io.BytesIO()
    pil.save(out, format="JPEG", quality=params.jpeg_quality, optimize=True)
    return out.getvalue()


def _measure_output_categories(path: str) -> Dict[str, int]:
    """Re-analyze the finished file to report honest byte categories.

    We deliberately re-read the output rather than trusting our own bookkeeping;
    the file on disk is the ground truth the user cares about.
    """
    a = analyze(path)
    return {
        "image": a.image_bytes_total,
        "font": a.font_bytes_estimate,
        "other": a.other_bytes,
    }


def optimize(
    src_path: str,
    dst_path: str,
    params: OptimizeParams,
    progress: Optional[ProgressCallback] = None,
) -> OptimizeResult:
    """Optimize ``src_path`` into ``dst_path`` according to ``params``.

    Parameters
    ----------
    src_path, dst_path:
        Input and output file paths. ``dst_path`` is overwritten if it exists.
    params:
        The levers to apply (see :class:`OptimizeParams`).
    progress:
        Optional callback for UI progress. Called with a 0..1 fraction and a
        short status string at each phase and on each image.
    """
    progress = progress or _noop
    original_size = os.path.getsize(src_path)

    doc = fitz.open(src_path)
    original_page_count = doc.page_count

    # --- Phase 1: image pass -------------------------------------------------
    progress(0.05, "Inspecting images")

    # Collect each unique image once, remembering one page it lives on so we can
    # compute its displayed DPI. Replacing by xref updates every placement.
    seen: Dict[int, dict] = {}
    for page_index in range(doc.page_count):
        page = doc[page_index]
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            rects = page.get_image_rects(xref)
            seen[xref] = {
                "page": page_index,
                "widest_in": (max((r.width for r in rects), default=0) / 72.0),
            }

    processed = 0
    skipped = 0
    total = max(1, len(seen))
    for n, (xref, meta) in enumerate(seen.items(), start=1):
        progress(0.05 + 0.65 * (n / total), f"Optimizing image {n} of {total}")
        try:
            base = doc.extract_image(xref)
        except Exception:
            skipped += 1
            continue

        stored_bytes = base.get("size", len(base.get("image", b"")))
        if stored_bytes < params.min_image_bytes:
            skipped += 1
            continue

        widest_in = meta["widest_in"]
        displayed_dpi = (base["width"] / widest_in) if widest_in > 0 else None

        new_bytes = _reencode_image(
            raw=base["image"],
            components=base.get("colorspace", 3) or 3,
            has_alpha=bool(base.get("smask", 0)),
            native_w=base["width"],
            native_h=base["height"],
            displayed_dpi=displayed_dpi,
            params=params,
        )

        # Only adopt the re-encode if it actually shrinks this image. This single
        # check prevents the classic failure where "compression" enlarges a file.
        if new_bytes is not None and len(new_bytes) < stored_bytes:
            try:
                # replace_image is a Page method but operates on the global xref,
                # so calling it once via any page updates every placement.
                doc[meta["page"]].replace_image(xref, stream=new_bytes)
                processed += 1
            except Exception:
                skipped += 1
        else:
            skipped += 1

    # --- Phase 2: font subsetting -------------------------------------------
    if params.subset_fonts:
        progress(0.75, "Subsetting fonts")
        try:
            doc.subset_fonts()
        except Exception:
            # Subsetting is best-effort; a failure must never abort the run.
            pass

    # --- Phase 3: metadata ---------------------------------------------------
    if params.strip_metadata:
        progress(0.8, "Removing metadata")
        try:
            doc.set_metadata({})
            doc.del_xml_metadata()
        except Exception:
            pass

    # --- Phase 4: save with structural cleanup ------------------------------
    progress(0.85, "Rebuilding document")
    save_opts = dict(
        garbage=4 if params.clean_structure else 1,  # 4 = full dead-object sweep
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
        clean=params.clean_structure,
        linear=params.linearize,
    )
    doc.save(dst_path, **save_opts)
    doc.close()

    # --- Phase 5: optional Ghostscript distillation -------------------------
    used_gs = False
    if params.ghostscript_pass and gs.available():
        progress(0.92, "Final distillation pass")
        used_gs = gs.try_shrink(dst_path, original_page_count)

    # --- Verify integrity and report ----------------------------------------
    progress(0.97, "Verifying output")
    out_doc = fitz.open(dst_path)
    out_pages = out_doc.page_count
    out_doc.close()
    if out_pages != original_page_count:
        # Should be impossible given our guards, but if anything upstream changed
        # the page count we refuse to hand back a corrupted document.
        raise RuntimeError(
            f"Integrity check failed: page count changed "
            f"{original_page_count} -> {out_pages}."
        )

    cats = _measure_output_categories(dst_path)
    progress(1.0, "Done")

    return OptimizeResult(
        original_size=original_size,
        optimized_size=os.path.getsize(dst_path),
        page_count=out_pages,
        images_processed=processed,
        images_skipped=skipped,
        optimized_image_bytes=cats["image"],
        optimized_font_bytes=cats["font"],
        optimized_other_bytes=cats["other"],
        used_ghostscript=used_gs,
    )
