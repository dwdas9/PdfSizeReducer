"""
targeting.py
============

Implements the "I need this under 10 MB" workflow. Given a desired output size,
it searches the parameter space for the *gentlest* settings that still meet the
target — that is, it preserves as much quality as possible while satisfying the
constraint.

Strategy
--------
Image data dominates size and responds smoothly to two levers — JPEG quality and
DPI — so we search those. We use the cheap :mod:`estimator` to navigate (no file
writes), and only run the real optimizer once at the end to produce the actual
file. This keeps the interaction fast: a target search is essentially free until
the final pass.

The search prefers lowering *quality* before *resolution*, because moderate JPEG
quality reduction is far less visible than resolution loss on typical document
imagery. Only when quality alone cannot reach the target do we start reducing
DPI. This ordering mirrors how a careful human operator would approach it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict

from .analyzer import PdfAnalysis
from .profiles import OptimizeParams, get_profile
from .estimator import estimate, Estimate


# Bounds for the search. We never drop below these because output past this point
# is rarely useful and the quality cost is severe.
MIN_QUALITY = 30
MAX_QUALITY = 90
MIN_DPI = 72
MAX_DPI = 300

# Quality/DPI step granularity for the coarse search.
QUALITY_STEPS = list(range(MAX_QUALITY, MIN_QUALITY - 1, -5))  # 90,85,...,30
DPI_STEPS = [250, 220, 200, 175, 150, 125, 110, 96, 85, 72]

# The estimator is good but not exact for any single image. We aim the *search*
# at a fraction of the real target so estimation error has somewhere to absorb
# into. The verification pass in `optimize_to_target` then corrects any residual
# overshoot. 0.90 means "plan to land 10% under the requested size".
SAFETY_MARGIN = 0.90


@dataclass
class TargetPlan:
    """The chosen parameters and the estimate that justifies them."""

    params: OptimizeParams
    estimate: Estimate
    target_bytes: int
    achievable: bool  # False if even the most aggressive settings miss the target

    def to_dict(self) -> Dict:
        return {
            "params": self.params.to_dict(),
            "estimate": self.estimate.to_dict(),
            "target_bytes": self.target_bytes,
            "achievable": self.achievable,
        }


def plan_for_target(analysis: PdfAnalysis, target_bytes: int) -> TargetPlan:
    """Find the gentlest parameters whose *estimated* size is <= ``target_bytes``.

    Returns a :class:`TargetPlan`; if the target is unreachable even at the most
    aggressive settings, ``achievable`` is False and the plan carries those most
    aggressive settings (the best we can do), so the UI can say "we'll get as
    close as possible" rather than silently failing.
    """
    # Start from the "strong" profile shape but drive quality/DPI ourselves.
    base = get_profile("strong").replace(
        recompress_lossless_to_jpeg=True,
        subset_fonts=True,
        clean_structure=True,
    )

    # Aim the estimate-driven search slightly under the real target (see
    # SAFETY_MARGIN) to leave room for per-image estimation error.
    search_target = int(target_bytes * SAFETY_MARGIN)

    best_under: Optional[TargetPlan] = None

    # Phase 1: hold DPI at a generous 200 and lower quality. Lowering quality is
    # the least visible lever, so we exhaust it first.
    for q in QUALITY_STEPS:
        params = base.replace(image_dpi=200, jpeg_quality=q)
        est = estimate(analysis, params)
        if est.predicted_size <= search_target:
            # Found a quality-only solution; this is the gentlest kind, return it.
            return TargetPlan(params, est, target_bytes, achievable=True)
        best_under = TargetPlan(params, est, target_bytes, achievable=False)

    # Phase 2: quality alone wasn't enough. Now step DPI down, and at each DPI try
    # quality from high to low, taking the first that fits.
    most_aggressive: Optional[TargetPlan] = None
    for dpi in DPI_STEPS:
        for q in QUALITY_STEPS:
            params = base.replace(image_dpi=dpi, jpeg_quality=q)
            est = estimate(analysis, params)
            most_aggressive = TargetPlan(params, est, target_bytes, achievable=False)
            if est.predicted_size <= search_target:
                return TargetPlan(params, est, target_bytes, achievable=True)

    # Nothing met the target. Return the most aggressive plan we evaluated so the
    # caller can still produce the smallest possible file and report honestly.
    if most_aggressive is None:  # pragma: no cover - defensive
        most_aggressive = best_under
    return most_aggressive


def optimize_to_target(
    src_path: str,
    dst_path: str,
    analysis: PdfAnalysis,
    target_bytes: int,
    progress=None,
    max_refinements: int = 2,
):
    """Produce a file at or under ``target_bytes`` when achievable.

    This is the reliable, "actually hits the number" entry point. It works in two
    stages:

    1. Use :func:`plan_for_target` (estimate-driven, no file writes) to choose a
       sensible starting point quickly.
    2. Run the real optimizer, then *verify* the produced size. If estimation
       error left us over the target, tighten quality a few steps and re-run,
       up to ``max_refinements`` times. Each refinement is a real pass, which is
       affordable because this product targets a handful of large files, not
       bulk throughput.

    Returns
    -------
    (OptimizeResult, OptimizeParams, bool)
        The final result, the parameters actually used, and whether the target
        was met. The import of :mod:`optimizer` is local to avoid any import-order
        coupling between the two modules.
    """
    from .optimizer import optimize  # local import: keeps module graph acyclic

    plan = plan_for_target(analysis, target_bytes)
    params = plan.params

    result = optimize(src_path, dst_path, params, progress)

    refinements = 0
    # While we're over target and still have quality headroom, tighten and retry.
    while (
        result.optimized_size > target_bytes
        and refinements < max_refinements
        and (params.jpeg_quality or MAX_QUALITY) > MIN_QUALITY
    ):
        refinements += 1
        # Estimate how far over we are and drop quality proportionally (at least
        # one step). This converges quickly without an exhaustive re-search.
        overshoot = result.optimized_size / target_bytes
        drop = max(5, int(10 * overshoot))
        new_q = max(MIN_QUALITY, (params.jpeg_quality or MAX_QUALITY) - drop)
        params = params.replace(jpeg_quality=new_q)
        result = optimize(src_path, dst_path, params, progress)

    met = result.optimized_size <= target_bytes
    return result, params, met
