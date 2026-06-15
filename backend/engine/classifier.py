"""
classifier.py
=============

Turns the raw :class:`~engine.analyzer.PdfAnalysis` into a single, defensible
recommendation: "for this document, start with *this* profile". This is the
"intelligent" part of the product surface — not in the sense of a neural network,
but in the sense of encoding the judgement an expert would apply when they open a
file and decide how hard to push.

We keep the rules transparent and explainable on purpose. Every recommendation
comes with a short human-readable rationale that the UI shows next to the
suggested profile, so the choice never feels like a black box. If you want to
tune behaviour, the thresholds live here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from .analyzer import PdfAnalysis


@dataclass
class Recommendation:
    profile: str
    rationale: str

    def to_dict(self) -> Dict:
        return {"profile": self.profile, "rationale": self.rationale}


# Below this, a document simply isn't big enough to be worth aggressive
# compression; we recommend the safe profile so we never degrade a small file.
SMALL_FILE_BYTES = 1_000_000

# Effective DPI above which images are clearly over-sampled for screen use and
# there is easy, safe room to downsample.
OVERSAMPLED_DPI = 220


def recommend(analysis: PdfAnalysis) -> Recommendation:
    """Pick a starting profile and explain why.

    The branches are ordered from most specific signal to least, so the first
    matching rule wins.
    """
    # 1. Scanned paper has its own dedicated, grayscale-leaning pipeline.
    if analysis.is_probably_scanned:
        return Recommendation(
            profile="scanned",
            rationale=(
                f"{int(analysis.scanned_page_fraction * 100)}% of pages look like "
                "full-page scans with no text layer. The scanned profile keeps "
                "text legible while converting to grayscale and recompressing the "
                "page images."
            ),
        )

    # 2. Very small files: don't risk quality for a trivial saving.
    if analysis.file_size < SMALL_FILE_BYTES:
        return Recommendation(
            profile="lossless",
            rationale=(
                "This file is already small, so the safe profile cleans up "
                "structural waste without altering any images."
            ),
        )

    # 3. Image-dominated files with clearly oversampled imagery: lots of safe
    #    headroom, so a strong profile pays off without visible harm.
    image_share = (
        analysis.image_bytes_total / analysis.file_size if analysis.file_size else 0
    )
    max_dpi = max(
        (i.displayed_dpi or 0 for i in analysis.images), default=0
    )
    if image_share >= 0.6 and max_dpi >= OVERSAMPLED_DPI:
        return Recommendation(
            profile="strong",
            rationale=(
                f"Images are {int(image_share * 100)}% of this file and some are "
                f"sampled at ~{int(max_dpi)} DPI — far above what screens need. "
                "Strong compression reclaims most of that without touching text."
            ),
        )

    # 4. Image-heavy but already reasonably sampled: balanced is the sweet spot.
    if image_share >= 0.3:
        return Recommendation(
            profile="balanced",
            rationale=(
                f"Images make up {int(image_share * 100)}% of this file. Balanced "
                "trims them to a print-safe resolution while preserving quality."
            ),
        )

    # 5. Text/vector-dominated files: little to gain from image work; structural
    #    cleanup and font subsetting are the levers that matter.
    return Recommendation(
        profile="lossless",
        rationale=(
            "This document is mostly text and vector content, so the safe profile "
            "focuses on structural cleanup and font subsetting."
        ),
    )
