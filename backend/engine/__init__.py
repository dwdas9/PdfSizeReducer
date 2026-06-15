"""
engine
======

The PDF optimization engine: a self-contained, framework-agnostic package that
knows nothing about HTTP or the UI. Everything the web layer needs is re-exported
here so callers can simply ``from engine import analyze, optimize, ...``.

Module map
----------
* :mod:`engine.profiles`    — named presets and the ``OptimizeParams`` lever set.
* :mod:`engine.analyzer`    — read-only inspection / byte inventory.
* :mod:`engine.classifier`  — recommends a profile from an analysis.
* :mod:`engine.estimator`   — predicts output size before processing.
* :mod:`engine.optimizer`   — the actual compression pipeline.
* :mod:`engine.targeting`   — chooses parameters to hit a desired size.
* :mod:`engine.gs`          — optional Ghostscript distillation backend.

Keeping the engine decoupled from the API means it is equally usable from a CLI,
a batch script, or unit tests — and easy for a future maintainer to embed
elsewhere.
"""

from .profiles import OptimizeParams, get_profile, list_profiles
from .analyzer import analyze, PdfAnalysis
from .classifier import recommend, Recommendation
from .estimator import estimate, Estimate
from .optimizer import optimize, OptimizeResult
from .targeting import plan_for_target, optimize_to_target, TargetPlan
from . import gs

__all__ = [
    "OptimizeParams",
    "get_profile",
    "list_profiles",
    "analyze",
    "PdfAnalysis",
    "recommend",
    "Recommendation",
    "estimate",
    "Estimate",
    "optimize",
    "OptimizeResult",
    "plan_for_target",
    "optimize_to_target",
    "TargetPlan",
    "gs",
]
