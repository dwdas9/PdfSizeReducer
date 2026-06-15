# Architecture

This document explains how Compact is put together, why it is built this way, and
where to extend it. It is written for the next developer (human or AI) who needs
to maintain or enhance the codebase.

---

## 1. Guiding principles

1. **The engine knows nothing about HTTP.** All optimization logic lives in the
   `engine/` package as plain Python with no web dependencies. The FastAPI layer
   is a thin adapter. This makes the engine trivially testable, scriptable, and
   reusable (CLI, batch job, another service) and keeps the interesting logic out
   of request handlers.

2. **Predict, then verify.** Two code paths describe the same operation: the
   `estimator` predicts an outcome cheaply (no file writes) so the UI can be
   live; the `optimizer` produces the real file and reports measured facts. They
   share the exact same parameter object (`OptimizeParams`) so they can never
   disagree about *direction*.

3. **Fidelity is a hard constraint, not a setting.** Page-count verification,
   transparency preservation, and the never-enlarge rule are enforced in the
   optimizer regardless of parameters. A "compressor" that can corrupt documents
   is not trustworthy, and trust is the product.

4. **One source of truth per concept.** Profiles and parameter semantics live
   only in `profiles.py`; on-disk paths live only in `storage.py`; byte
   categories are computed only in `analyzer.py`. Changing behavior means editing
   one place.

---

## 2. Module map

### Engine (`backend/engine/`)

| Module          | Responsibility | Key types / functions |
|-----------------|----------------|-----------------------|
| `profiles.py`   | Define the lever set and named presets. | `OptimizeParams` (frozen dataclass), `get_profile`, `list_profiles` |
| `analyzer.py`   | Read-only inspection; image/font inventory; scanned detection; **effective DPI**. | `analyze` → `PdfAnalysis`, `ImageInfo`, `FontInfo` |
| `classifier.py` | Recommend a starting profile with a human rationale. | `recommend` → `Recommendation` |
| `estimator.py`  | Predict output size + per-category bytes, no I/O. | `estimate` → `Estimate` |
| `optimizer.py`  | The real pipeline: image recompression, font subset, cleanup, optional GS. | `optimize` → `OptimizeResult` |
| `targeting.py`  | Choose params to hit a size, then verify and tighten. | `plan_for_target`, `optimize_to_target` |
| `gs.py`         | Optional Ghostscript distillation, fail-safe. | `available`, `try_shrink` |
| `__init__.py`   | Curated public API for the package. | re-exports the above |

### Web layer (`backend/`)

| Module       | Responsibility |
|--------------|----------------|
| `app.py`     | FastAPI routes; parameter resolution; file serving; serves the SPA. |
| `models.py`  | Pydantic request schemas (`EstimateRequest`, `OptimizeRequest`, `CustomParams`). |
| `storage.py` | `SessionStore`: one folder per upload under a temp dir; no database. |

### Frontend (`frontend/`)

| File         | Responsibility |
|--------------|----------------|
| `index.html` | App shell; loads fonts, PDF.js, and the stylesheet/script. |
| `styles.css` | The "precision instrument" design system and the size-meter styling. |
| `app.js`     | All behavior: upload, live estimate, optimize, meter, PDF.js previews. |

### Launcher (`run.py`)

Puts `backend/` on `sys.path` and starts Uvicorn, so the app's internal imports
(`from engine import ...`) resolve from any working directory.

---

## 3. Data flow

```
            ┌──────────┐   drop / pick files    ┌─────────────┐
            │ Browser  │ ─────────────────────► │  POST       │
            │ (app.js) │                        │  /api/upload│
            └──────────┘                        └─────┬───────┘
                 ▲                                     │ store original
                 │ analysis + recommendation           ▼
                 │                              storage.SessionStore
                 │                                     │
                 │                              engine.analyze ──► PdfAnalysis
                 │                              engine.recommend ─► Recommendation
                 │ ◄───────────────────────────────────┘
                 │
   slider moves  │  POST /api/estimate          engine.estimate ─► Estimate
   (debounced)   │ ───────────────────────────► (or plan_for_target for a target)
                 │ ◄─ predicted size + breakdown ─┘   [no file written]
                 │
   click Optimize│  POST /api/optimize          engine.optimize ─► OptimizeResult
                 │ ───────────────────────────► (or optimize_to_target)
                 │ ◄─ measured result ───────────┘   writes optimized.pdf
                 │
   preview/save  │  GET /api/file/{sid}/optimized  ·  GET /api/download/{sid}
                 ▼
            PDF.js renders original & optimized side by side
```

Per-file state lives in the browser (`state.files`, keyed by `session_id`) so
switching tabs is instant; the server keeps only the bytes and the cached
analysis.

---

## 4. The core idea: effective DPI

The single most important concept is **effective DPI**, computed in
`analyzer._effective_dpi`:

```
effective_dpi = image_pixel_width / (displayed_width_in_points / 72)
```

An image's raw pixel dimensions tell you nothing about whether it is wasteful —
only its resolution *relative to how big it is drawn* does. Every downsampling
decision (in both the estimator and the optimizer) is driven by this number, and
the rule is always "downsample toward the target DPI, never upscale, never touch
images already at or below target." This is what lets Compact be aggressive on
bloat while leaving appropriately-sized images alone.

---

## 5. The optimization pipeline (`optimizer.optimize`)

1. **Collect unique images** by xref (an image reused on many pages is processed
   once; `replace_image` updates every placement because xrefs are global).
2. **Per image**, decide and act:
   - skip if smaller than `min_image_bytes` (header overhead would dominate);
   - skip if it carries transparency (never flatten a soft mask to JPEG);
   - skip if the profile is lossless (`jpeg_quality is None`);
   - otherwise convert color model (optional grayscale), downsample to target
     DPI, re-encode JPEG at the chosen quality;
   - **adopt the re-encode only if it is smaller** than the original stream.
3. **Subset fonts** (best-effort; failure never aborts).
4. **Strip metadata** if requested.
5. **Save** with structural cleanup (garbage collection, deflate, object streams)
   and optional linearization.
6. **Optional Ghostscript pass** (`gs.try_shrink`) — structure-only, adopted only
   if valid, same page count, and strictly smaller.
7. **Verify** page count and **re-analyze** the output to report honest
   per-category byte numbers.

---

## 6. Target-size search (`targeting`)

Two stages keep it both fast and reliable:

1. `plan_for_target` uses the cheap estimator to navigate the parameter space —
   lowering **quality before resolution** (quality loss is less visible) — aiming
   at `target × SAFETY_MARGIN` (currently 0.90) to leave room for estimation
   error. No files are written here, so it stays interactive.
2. `optimize_to_target` runs the chosen plan for real, then **verifies** the
   produced size and, if estimation error left it over target, tightens quality a
   few steps and re-runs (bounded by `max_refinements`). Because the product
   targets a few large files rather than bulk throughput, a couple of extra real
   passes is an acceptable price for reliably hitting the number.

---

## 7. Frontend design

The visual language is a **precision measurement instrument**: numbers are the
hero, set in a monospaced face with tabular figures like a meter. The single
signature element is the **size meter** — a horizontal bar comparing original vs
optimized bytes with a large percentage readout. It is slate while showing an
estimate and green once a real result lands, so the user always knows whether
they are looking at a prediction or a fact.

State lives in `state.files` keyed by `session_id`; "strategy" has three modes —
`profile`, `custom`, `target` — and `currentRequestBody()` is the single place
that translates the active mode into an API request, mirroring the server's
`_resolve_params`.

---

## 8. Extension points

- **New profile**: add an entry to `_PROFILES` in `profiles.py`. It automatically
  appears in `/api/profiles`; add it to `PROFILE_ORDER`/`PROFILE_DESC` in
  `app.js` to surface it in the chooser.
- **New lever**: add a field to `OptimizeParams`, honor it in `optimizer.optimize`
  and (for live prediction) in `estimator`, then expose it in `CustomParams` and
  the advanced panel.
- **Real-time progress**: `optimize` already accepts a `progress(fraction, msg)`
  callback. Wire it to Server-Sent Events or a WebSocket and stream phase updates
  to the meter; the UI already has a working/animated state to attach to.
- **OCR for scans**: the stack includes Tesseract-friendly tooling; an OCR pass
  (e.g. via OCRmyPDF) could be added as an opt-in step in the optimizer to make
  scanned output searchable.
- **MRC (Mixed Raster Content)**: the highest-end scanned-document compression
  separates text/background layers. It would slot in as an alternative image
  strategy in `optimizer`, selected by the `scanned` profile.

---

## 9. Testing

The engine is testable without the web layer:

```python
from engine import analyze, estimate, optimize, get_profile
a = analyze("sample.pdf")
assert estimate(a, get_profile("strong")).saved_pct > 0
r = optimize("sample.pdf", "out.pdf", get_profile("strong"))
assert r.page_count == a.page_count          # integrity
assert r.optimized_size < r.original_size    # it actually shrank
```

The HTTP layer can be exercised in-process with `fastapi.testclient.TestClient`
(no server needed), which is how the endpoints were validated during
development.

---

## 10. Known trade-offs / non-goals

- **Not a bulk processor.** Optimization is synchronous and tuned for a few large
  files. A queue/worker model would be a different product.
- **Preview needs a CDN** for PDF.js by default; optimization itself is fully
  local and offline-capable. Vendor PDF.js locally if you need offline previews.
- **Ghostscript is optional** and used only for a conservative structural pass; it
  is never allowed to make the file worse.
