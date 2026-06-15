"""
profiles.py
===========

Central definition of *optimization profiles* and the low-level *parameter
object* (`OptimizeParams`) that the rest of the engine consumes.

Why this module exists
----------------------
Every other part of the engine (estimator, optimizer, target-size search) needs
to agree on exactly what a given level of compression *means*. Rather than
sprinkling magic numbers (DPI caps, JPEG quality, grayscale flags) throughout the
code base, we funnel all of those decisions through a single, well-documented
data structure. A "profile" is simply a friendly, named preset that resolves to a
concrete `OptimizeParams`.

Design decisions
----------------
* `OptimizeParams` is a frozen dataclass so that a set of parameters can be
  safely passed around, hashed, logged, and compared without fear of accidental
  mutation. The target-size searcher in `targeting.py` relies on producing many
  candidate parameter sets, so immutability keeps that logic clean.
* `None` is used deliberately to mean "do not touch this dimension". For example
  `image_dpi=None` means *never downsample images*, which is distinct from
  `image_dpi=0`. This lets the `lossless` profile express "clean up structure but
  leave every pixel untouched".
* Profiles are returned as fresh `OptimizeParams` instances from
  :func:`get_profile` so callers can use :meth:`OptimizeParams.replace` to derive
  custom variants (used by the advanced-options UI and the target-size search).
"""

from __future__ import annotations

from dataclasses import dataclass, replace, asdict
from typing import Optional, Dict


@dataclass(frozen=True)
class OptimizeParams:
    """Concrete, machine-level instructions for a single optimization run.

    Each field maps to one lever the optimizer can pull. Keeping them flat and
    explicit (rather than nested config) makes the estimator's job — predicting
    the effect of each lever — straightforward.

    Attributes
    ----------
    image_dpi:
        Target resolution cap for *raster* images, in dots per inch measured at
        the size the image is actually displayed on the page. Images whose
        effective DPI already sits at or below this value are left at their
        native resolution (we never upscale). ``None`` disables downsampling.
    jpeg_quality:
        JPEG quality (1-100) used when re-encoding raster images. ``None``
        disables lossy re-encoding entirely, in which case images are only ever
        re-stored losslessly.
    recompress_lossless_to_jpeg:
        When ``True``, images stored in lossless formats (PNG/Flate/LZW) are
        permitted to be re-encoded as JPEG. This is where the biggest wins on
        "born-digital but image-heavy" PDFs come from, but it is lossy, so the
        ``lossless`` profile leaves it ``False``.
    to_grayscale:
        Convert color raster images to 8-bit grayscale. Roughly a 3x reduction
        in raw image data and ideal for black-ink scanned documents.
    min_image_bytes:
        Images whose stored stream is smaller than this are skipped. Tiny images
        (icons, logos, 1px spacers) cost more in JPEG header overhead than they
        save, and re-encoding them can actually *grow* the file.
    strip_metadata:
        Remove document information dictionary and XMP metadata. Small absolute
        saving, but some users want it for privacy as much as for size.
    subset_fonts:
        Re-embed only the glyphs actually used, discarding the rest of each font
        program. Large wins on documents that embed full CJK or icon fonts.
    clean_structure:
        Run the structural pass (garbage-collect unused objects, compress object
        streams, drop duplicate resources). Almost always safe and worthwhile.
    linearize:
        Produce a "fast web view" (linearized) PDF whose first page renders
        before the whole file downloads. Marginal size effect; quality-of-life.
    ghostscript_pass:
        Run a final Ghostscript distillation pass. Ghostscript occasionally
        squeezes out structural bytes our Python pass leaves behind, at the cost
        of time and a small risk of re-rendering quirks. Off by default; the
        engine offers it as an opt-in "maximum" step.
    """

    image_dpi: Optional[int] = 150
    jpeg_quality: Optional[int] = 75
    recompress_lossless_to_jpeg: bool = True
    to_grayscale: bool = False
    min_image_bytes: int = 4096
    strip_metadata: bool = False
    subset_fonts: bool = True
    clean_structure: bool = True
    linearize: bool = False
    ghostscript_pass: bool = False

    def replace(self, **changes) -> "OptimizeParams":
        """Return a copy with the given fields overridden.

        Thin wrapper over :func:`dataclasses.replace` so call sites read
        naturally, e.g. ``params.replace(jpeg_quality=60)``. Used heavily by the
        custom-options endpoint and the target-size search, which generate many
        near-identical parameter sets.
        """
        return replace(self, **changes)

    def to_dict(self) -> Dict:
        """Serialize to a plain dict for JSON responses and logging."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Named profiles
# ---------------------------------------------------------------------------
# The dictionary below is the single source of truth for what each user-facing
# preset means. The UI shows these by key; the API resolves them here. When in
# doubt about "what does Strong actually do", this table is the answer.
#
# The ordering (lossless -> balanced -> strong -> extreme) is intentional: it is
# a monotonic ramp from "touch nothing but bytes" to "smallest reasonable file".
# `scanned` sits apart because it is tuned for a document *type* rather than a
# point on the size/quality ramp.
_PROFILES: Dict[str, OptimizeParams] = {
    # Safe mode: never alters a single pixel. Only removes structural waste and
    # subsets fonts. Use when fidelity is non-negotiable (legal exhibits,
    # archival masters) but the file still carries obvious bloat.
    "lossless": OptimizeParams(
        image_dpi=None,
        jpeg_quality=None,
        recompress_lossless_to_jpeg=False,
        to_grayscale=False,
        strip_metadata=False,
        subset_fonts=True,
        clean_structure=True,
    ),
    # The recommended default for most documents: a resolution and quality that
    # survives printing and close on-screen reading while removing the bulk of
    # the waste in over-scanned or screenshot-heavy PDFs.
    "balanced": OptimizeParams(
        image_dpi=200,
        jpeg_quality=80,
        recompress_lossless_to_jpeg=True,
        subset_fonts=True,
        clean_structure=True,
    ),
    # Noticeably smaller, still very readable on screen. The everyday choice when
    # the goal is "fits under the email limit".
    "strong": OptimizeParams(
        image_dpi=150,
        jpeg_quality=70,
        recompress_lossless_to_jpeg=True,
        subset_fonts=True,
        clean_structure=True,
    ),
    # Smallest sensible output. Visible softening of images up close, but text
    # stays crisp because text is vector, not raster. For sharing/preview copies.
    "extreme": OptimizeParams(
        image_dpi=110,
        jpeg_quality=55,
        recompress_lossless_to_jpeg=True,
        subset_fonts=True,
        clean_structure=True,
        strip_metadata=True,
    ),
    # Tuned for scanned paper: keeps enough DPI for OCR/readability, leans on
    # grayscale because most scans are black ink on white, and recompresses the
    # full-page raster that scanners emit.
    "scanned": OptimizeParams(
        image_dpi=200,
        jpeg_quality=65,
        recompress_lossless_to_jpeg=True,
        to_grayscale=True,
        subset_fonts=True,
        clean_structure=True,
    ),
}


def list_profiles() -> Dict[str, Dict]:
    """Return all profiles as JSON-serializable dicts, for the API/UI.

    The frontend uses this to render the profile chooser and to seed the advanced
    sliders when a user switches a profile into "custom" mode.
    """
    return {name: params.to_dict() for name, params in _PROFILES.items()}


def get_profile(name: str) -> OptimizeParams:
    """Resolve a profile name to a fresh :class:`OptimizeParams`.

    Raises
    ------
    KeyError
        If the name is not a known profile. Callers (the API layer) translate
        this into a 400 so the client gets a clear message rather than a 500.
    """
    if name not in _PROFILES:
        raise KeyError(
            f"Unknown profile '{name}'. Valid profiles: {', '.join(_PROFILES)}"
        )
    # Return a copy so downstream mutation-by-replace never edits the canonical
    # preset (dataclasses are frozen, but returning a distinct object keeps the
    # contract obvious to readers).
    return _PROFILES[name].replace()
