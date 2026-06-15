"""
estimator.py
============

Fast, *non-destructive* prediction of how big the output will be for a given set
of :class:`OptimizeParams`, using only the inventory produced by
:mod:`analyzer`. This is what powers the headline promise of the UI: a live
"estimated new size" that updates the instant the user drags a slider, long
before any real work happens.

Honesty policy
--------------
These are estimates, and the UI presents them as such. The model is deliberately
*slightly conservative* (it tends to predict a little less saving than we
actually achieve) so that the real result, shown after processing, is a pleasant
surprise rather than a disappointment. The estimator and the optimizer share the
same parameter semantics, so the two never disagree about *direction* — lowering
quality always lowers both the estimate and the real result.

Where the numbers come from
---------------------------
JPEG size is, to first order, proportional to pixel count times a
quality-dependent "bytes per pixel" factor, only weakly dependent on channel
count thanks to chroma subsampling. We calibrate that factor with a small lookup
curve (`_BYTES_PER_PIXEL`) interpolated by quality. This is the same shape of
model professional tools use for their pre-flight estimates; it will never be
exact for a specific image, but across a whole document it is reliably close.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict
import bisect

from .analyzer import PdfAnalysis, ImageInfo
from .profiles import OptimizeParams


# Calibrated JPEG cost curve: maps quality -> approximate bytes per pixel for a
# colour image of ordinary document content. Anchored against measured output on
# representative scans and screenshots. Values between anchors are linearly
# interpolated. Grayscale images are scaled by GRAY_FACTOR below.
_Q_ANCHORS = [40, 55, 65, 75, 85, 95]
_BYTES_PER_PIXEL = [0.055, 0.080, 0.105, 0.150, 0.230, 0.430]

# Grayscale JPEGs run roughly 55-65% the size of the equivalent colour JPEG.
GRAY_FACTOR = 0.6

# Lossless re-storage (re-deflate, de-duplicate, drop unused alpha) typically
# trims a modest slice off images we are NOT allowed to re-encode lossily.
LOSSLESS_RESTORE_SAVING = 0.12

# Structural cleanup (object-stream compression, garbage collection) on the
# non-image, non-font remainder of the file.
STRUCTURE_SAVING = 0.08

# Font subsetting typically discards a large fraction of an embedded font program
# because most documents use a small subset of glyphs.
FONT_SUBSET_SAVING = 0.40


@dataclass
class Estimate:
    """Predicted outcome of applying one parameter set.

    ``predicted_size`` is the headline number; the per-category fields feed the
    analytics breakdown so the user can see *where* the savings come from.
    """

    original_size: int
    predicted_size: int
    predicted_image_bytes: int
    predicted_font_bytes: int
    predicted_other_bytes: int

    @property
    def saved_bytes(self) -> int:
        return max(0, self.original_size - self.predicted_size)

    @property
    def saved_pct(self) -> float:
        if self.original_size <= 0:
            return 0.0
        return round(100 * self.saved_bytes / self.original_size, 1)

    def to_dict(self) -> Dict:
        return {
            "original_size": self.original_size,
            "predicted_size": self.predicted_size,
            "saved_bytes": self.saved_bytes,
            "saved_pct": self.saved_pct,
            "predicted_image_bytes": self.predicted_image_bytes,
            "predicted_font_bytes": self.predicted_font_bytes,
            "predicted_other_bytes": self.predicted_other_bytes,
        }


def _bytes_per_pixel(quality: int) -> float:
    """Interpolate the JPEG cost curve at an arbitrary quality value."""
    q = max(_Q_ANCHORS[0], min(_Q_ANCHORS[-1], quality))
    i = bisect.bisect_left(_Q_ANCHORS, q)
    if i == 0:
        return _BYTES_PER_PIXEL[0]
    if i >= len(_Q_ANCHORS):
        return _BYTES_PER_PIXEL[-1]
    # Linear interpolation between the two surrounding anchors.
    q0, q1 = _Q_ANCHORS[i - 1], _Q_ANCHORS[i]
    b0, b1 = _BYTES_PER_PIXEL[i - 1], _BYTES_PER_PIXEL[i]
    t = (q - q0) / (q1 - q0)
    return b0 + t * (b1 - b0)


def _predict_one_image(img: ImageInfo, params: OptimizeParams) -> int:
    """Predict the stored size of a single image after optimization."""
    # Tiny images are never touched: re-encoding overhead outweighs any saving.
    if img.stored_bytes < params.min_image_bytes:
        return img.stored_bytes

    lossy_allowed = params.jpeg_quality is not None and (
        # We re-encode lossily either when the source is already lossy (JPEG) or
        # when the profile permits turning lossless sources into JPEG.
        img.ext in ("jpeg", "jpg") or params.recompress_lossless_to_jpeg
    )

    if not lossy_allowed:
        # Only structural/lossless gains are available for this image.
        return int(img.stored_bytes * (1 - LOSSLESS_RESTORE_SAVING))

    # Determine the downsample factor from effective DPI.
    scale = 1.0
    if params.image_dpi and img.displayed_dpi and img.displayed_dpi > params.image_dpi:
        scale = params.image_dpi / img.displayed_dpi
    new_pixels = img.width * img.height * (scale ** 2)

    bpp = _bytes_per_pixel(params.jpeg_quality)
    if params.to_grayscale or img.components == 1:
        bpp *= GRAY_FACTOR

    predicted = int(new_pixels * bpp)

    # Never *predict* growth: if our model says the re-encode would be larger
    # than the original stream, the optimizer would keep the original, so the
    # estimate should too.
    return min(predicted, img.stored_bytes)


def estimate(analysis: PdfAnalysis, params: OptimizeParams) -> Estimate:
    """Predict the output for ``analysis`` under ``params``.

    Pure function of the analysis and parameters — no file I/O — so it is cheap
    enough to call on every slider movement in the UI.
    """
    # Images
    predicted_images = sum(_predict_one_image(i, params) for i in analysis.images)

    # Fonts
    if params.subset_fonts and analysis.font_bytes_estimate:
        predicted_fonts = int(analysis.font_bytes_estimate * (1 - FONT_SUBSET_SAVING))
    else:
        predicted_fonts = analysis.font_bytes_estimate

    # Everything else (text, vectors, structure, metadata)
    other = analysis.other_bytes
    if params.clean_structure:
        other = int(other * (1 - STRUCTURE_SAVING))
    if params.strip_metadata:
        other = max(0, other - 4096)  # metadata is typically a few KB

    predicted_total = predicted_images + predicted_fonts + other

    # Guard rails: optimization should never be predicted to enlarge the file,
    # and a sane floor avoids absurd "99.9% smaller" claims on pathological input.
    predicted_total = min(predicted_total, analysis.file_size)
    predicted_total = max(predicted_total, 1024)

    return Estimate(
        original_size=analysis.file_size,
        predicted_size=predicted_total,
        predicted_image_bytes=predicted_images,
        predicted_font_bytes=predicted_fonts,
        predicted_other_bytes=other,
    )
