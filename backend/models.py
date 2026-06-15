"""
models.py
=========

Pydantic models describing the JSON request bodies the API accepts. Response
shapes are produced by the engine's own ``to_dict`` methods (analysis, estimate,
result), so we only need to formalise *inputs* here — the place where validating
untrusted client data actually matters.

Keeping these in one file gives the frontend a single, stable contract to code
against and gives FastAPI everything it needs to auto-generate request docs at
``/docs``.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class CustomParams(BaseModel):
    """User-tuned settings from the advanced panel.

    Every field is optional; omitted fields fall back to the chosen profile's
    value on the server. Bounds mirror the engine's safe ranges so the API
    rejects nonsense (e.g. quality 500) before it reaches the optimizer.
    """

    image_dpi: Optional[int] = Field(
        default=None, ge=36, le=600,
        description="Target effective DPI cap for images. Null = don't downsample.",
    )
    jpeg_quality: Optional[int] = Field(
        default=None, ge=1, le=100,
        description="JPEG quality for re-encoding. Null = no lossy re-encode.",
    )
    to_grayscale: Optional[bool] = Field(
        default=None, description="Convert colour images to grayscale."
    )
    recompress_lossless_to_jpeg: Optional[bool] = Field(
        default=None, description="Allow PNG/Flate images to become JPEG."
    )
    strip_metadata: Optional[bool] = Field(
        default=None, description="Remove document metadata."
    )
    subset_fonts: Optional[bool] = Field(
        default=None, description="Re-embed only glyphs in use."
    )
    ghostscript_pass: Optional[bool] = Field(
        default=None, description="Run optional final Ghostscript distillation."
    )


class EstimateRequest(BaseModel):
    """Ask for a predicted size for a given configuration.

    Exactly one of ``profile`` or ``target_bytes`` drives the prediction;
    ``custom`` may override individual levers on top of the chosen profile.
    """

    session_id: str
    profile: Optional[str] = Field(
        default=None, description="Named profile, e.g. 'balanced'."
    )
    target_bytes: Optional[int] = Field(
        default=None, ge=10_000,
        description="Desired output size in bytes. Overrides profile if set.",
    )
    custom: Optional[CustomParams] = None


class OptimizeRequest(BaseModel):
    """Run a real optimization and write the output file.

    Same selection semantics as :class:`EstimateRequest`. When ``target_bytes``
    is provided the server uses the verifying target search; otherwise it applies
    the profile (with any custom overrides) directly.
    """

    session_id: str
    profile: Optional[str] = Field(default=None)
    target_bytes: Optional[int] = Field(default=None, ge=10_000)
    custom: Optional[CustomParams] = None
