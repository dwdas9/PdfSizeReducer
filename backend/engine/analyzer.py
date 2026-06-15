"""
analyzer.py
===========

Read-only inspection of a PDF. The analyzer never writes a file; its sole job is
to build an honest, structured picture of *where the bytes live* and *what kind
of document this is*, so that:

1. the UI can show the user a real breakdown ("82% of this file is images"),
2. the estimator can predict savings without doing a full optimization pass, and
3. the classifier can recommend a sensible default profile.

Everything here is built on PyMuPDF (``fitz``) because it exposes the per-image
cross-reference table, the actual on-page placement rectangles (needed to
compute *effective* DPI), and font embedding info in a single fast pass.

Key concept: effective DPI
--------------------------
A 3000px-wide image is not inherently "high resolution" — it depends on how big
it is drawn. The same pixels shown at 2 inches wide are 1500 DPI (wasteful);
shown at 20 inches they are 150 DPI (appropriate). All downsampling decisions in
this engine are driven by *effective* DPI = pixel_width / displayed_width_inches,
which is exactly what this module computes from the placement rectangles.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
import os

import fitz  # PyMuPDF


# Points-per-inch in PDF user space. PDF coordinates are always 1/72 inch, so the
# conversion from on-page point dimensions to inches is a constant.
POINTS_PER_INCH = 72.0


@dataclass
class ImageInfo:
    """Everything we know about one embedded raster image.

    One entry per *unique* image cross-reference (xref). A single image reused on
    many pages (a letterhead logo, for instance) appears once here, with
    ``displayed_dpi`` reflecting the largest placement found anywhere.
    """

    xref: int
    width: int                       # native pixel width
    height: int                      # native pixel height
    components: int                  # colour channels (1=gray, 3=RGB, 4=CMYK)
    bpc: int                         # bits per component
    stored_bytes: int                # size of the compressed stream on disk
    ext: str                         # current encoding hint ('png','jpeg',...)
    has_alpha: bool                  # carries a soft mask / transparency
    displayed_dpi: Optional[float]   # effective resolution as drawn (None if unplaced)
    pages: List[int] = field(default_factory=list)  # pages it appears on (1-based)

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000.0


@dataclass
class FontInfo:
    """One embedded or referenced font."""

    name: str
    type: str
    embedded: bool


@dataclass
class PdfAnalysis:
    """The complete read-only report for a single PDF.

    This object is the contract between the engine and the outside world: the API
    serializes it to JSON, the estimator consumes it, and the classifier reads it
    to pick a profile. Adding a field here is the right way to expose new insight
    to the UI.
    """

    path: str
    file_size: int
    page_count: int
    images: List[ImageInfo]
    fonts: List[FontInfo]
    image_bytes_total: int           # sum of stored image streams
    font_bytes_estimate: int         # rough sum of embedded font programs
    is_probably_scanned: bool        # heuristic: full-page images, little text
    scanned_page_fraction: float     # 0..1, how "scan-like" the document is
    has_text: bool                   # any extractable text at all

    @property
    def other_bytes(self) -> int:
        """Bytes that are neither image streams nor fonts: text, vectors,
        structure, metadata. Derived so the UI's breakdown always sums to 100%.
        """
        return max(0, self.file_size - self.image_bytes_total - self.font_bytes_estimate)

    def to_dict(self) -> Dict:
        """JSON-serializable view, including the derived byte categories the UI
        renders as a stacked bar."""
        d = asdict(self)
        d["other_bytes"] = self.other_bytes
        # Convenience percentages so the frontend doesn't recompute them.
        total = max(1, self.file_size)
        d["breakdown_pct"] = {
            "images": round(100 * self.image_bytes_total / total, 1),
            "fonts": round(100 * self.font_bytes_estimate / total, 1),
            "other": round(100 * self.other_bytes / total, 1),
        }
        return d


def _effective_dpi(page: fitz.Page, xref: int, pixel_width: int) -> Optional[float]:
    """Compute the largest effective DPI at which ``xref`` is drawn on ``page``.

    We take the *widest* placement because that is the one that would look worst
    if we downsampled too aggressively — sizing decisions must respect the most
    demanding usage of the image.
    """
    rects = page.get_image_rects(xref)
    if not rects:
        return None
    widest_in = max(r.width for r in rects) / POINTS_PER_INCH
    if widest_in <= 0:
        return None
    return pixel_width / widest_in


def analyze(path: str) -> PdfAnalysis:
    """Inspect ``path`` and return a :class:`PdfAnalysis`.

    This is a single linear pass over the document. It is intentionally cheap so
    the UI can call it the instant a file is dropped, before the user has chosen
    any settings.
    """
    file_size = os.path.getsize(path)
    doc = fitz.open(path)
    page_count = doc.page_count  # captured up front; doc is closed before we return

    # We deduplicate images by xref while remembering every page each appears on
    # and tracking the maximum effective DPI across all those placements.
    images_by_xref: Dict[int, ImageInfo] = {}
    fonts_by_name: Dict[str, FontInfo] = {}
    font_bytes_estimate = 0

    scanned_pages = 0
    any_text = False

    for page_index in range(doc.page_count):
        page = doc[page_index]
        page_number = page_index + 1
        page_area = abs(page.rect.width * page.rect.height)

        # --- Text presence (drives the scanned-vs-digital heuristic) ---
        text = page.get_text("text")
        page_has_text = bool(text.strip())
        any_text = any_text or page_has_text

        # --- Images ---
        page_image_area = 0.0
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                base = doc.extract_image(xref)
            except Exception:
                # Some exotic images cannot be extracted; skip them in the
                # inventory rather than aborting the whole analysis.
                continue

            width, height = base["width"], base["height"]
            dpi = _effective_dpi(page, xref, width)

            # Accumulate the on-page area this image covers, to detect full-page
            # scans below.
            for r in page.get_image_rects(xref):
                page_image_area += abs(r.width * r.height)

            if xref in images_by_xref:
                info = images_by_xref[xref]
                info.pages.append(page_number)
                if dpi is not None:
                    info.displayed_dpi = max(info.displayed_dpi or 0, dpi)
            else:
                images_by_xref[xref] = ImageInfo(
                    xref=xref,
                    width=width,
                    height=height,
                    components=base.get("colorspace", 3) or 3,
                    bpc=base.get("bpc", 8),
                    stored_bytes=base.get("size", len(base.get("image", b""))),
                    ext=base.get("ext", ""),
                    has_alpha=bool(base.get("smask", 0)),
                    displayed_dpi=dpi,
                    pages=[page_number],
                )

        # A page is "scan-like" when one or more images blanket most of it and
        # there is little or no real text layer. 0.7 area coverage is a
        # deliberately forgiving threshold so lightly-cropped scans still count.
        coverage = page_image_area / page_area if page_area else 0
        if coverage >= 0.7 and not page_has_text:
            scanned_pages += 1

        # --- Fonts ---
        for f in page.get_fonts(full=True):
            # tuple: (xref, ext, type, basefont, name, encoding, ...)
            f_xref, f_ext, f_type, basefont = f[0], f[1], f[2], f[3]
            embedded = f_ext != "n/a" and f_ext != ""
            if basefont not in fonts_by_name:
                fonts_by_name[basefont] = FontInfo(
                    name=basefont, type=f_type, embedded=embedded
                )
                if embedded and f_xref:
                    # Estimate embedded font program size from its stream length.
                    try:
                        stream = doc.xref_stream_raw(f_xref)
                        if stream:
                            font_bytes_estimate += len(stream)
                    except Exception:
                        pass

    doc.close()

    images = sorted(
        images_by_xref.values(), key=lambda i: i.stored_bytes, reverse=True
    )
    image_bytes_total = sum(i.stored_bytes for i in images)

    scanned_fraction = scanned_pages / page_count if page_count else 0.0

    return PdfAnalysis(
        path=path,
        file_size=file_size,
        page_count=page_count,
        images=images,
        fonts=list(fonts_by_name.values()),
        image_bytes_total=image_bytes_total,
        font_bytes_estimate=font_bytes_estimate,
        # "Probably scanned" if at least half the pages look like full-page scans.
        is_probably_scanned=scanned_fraction >= 0.5,
        scanned_page_fraction=round(scanned_fraction, 3),
        has_text=any_text,
    )
