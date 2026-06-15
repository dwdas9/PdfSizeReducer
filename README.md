# Compact - PDF Optimization Workbench

Shrink large PDFs dramatically without an Adobe Acrobat subscription. Compact is
a small, self-hosted web app focused on doing one thing exceptionally well:
taking a handful of big PDFs and making them small **quickly, intelligently, and
without wrecking them** — with full visibility into what changed and why.

On a typical image-heavy document it reduces size by **95–98%** while preserving
every page, the text layer, and document structure.

---

## Why it exists

Most "PDF compressors" are either a paid desktop suite or a website you upload
confidential documents to. Compact is neither: it runs on your own machine, the
files never leave it, and the compression engine is open, documented, and tuned
to match or beat Acrobat's "Reduce File Size" on the cases that actually matter
(scans, screenshots, image-heavy reports).

It is deliberately **not** a bulk pipeline for thousands of files. It is a
precision instrument for the few large files that are giving you trouble right
now.

---

## What it does

- **Drag-and-drop** one or several PDFs; each gets its own tab and settings.
- **Recommends** a starting strategy by analyzing the document (scanned vs
  digital, how much is images, how over-sampled they are).
- **Estimates the new size before you commit**, live, as you change settings —
  the signature "size meter" updates on every slider move.
- **Profiles** from `Lossless` (touch no pixels) to `Extreme` (smallest sensible
  file), plus a `Scanned` profile tuned for paper.
- **Target a size** ("get this under 5 MB") and it finds the gentlest settings
  that fit, then verifies the real result.
- **Side-by-side preview** of the original and optimized PDF, page by page.
- **A breakdown** of where the bytes are (images / fonts / everything else).
- **Advanced controls**: image resolution (DPI), JPEG quality, grayscale,
  metadata stripping, font subsetting, and an optional Ghostscript pass.
- **Integrity guarantees**: page count is verified, transparent images are never
  flattened, and any change that would enlarge an image is discarded.

---

## How it works (the short version)

The win on large PDFs is almost always the **images**. Compact:

1. Inventories every embedded image and computes its *effective* DPI — its pixel
   resolution relative to the size it is actually drawn on the page. A 3000px
   image shown two inches wide is 1500 DPI and hugely wasteful; the same image
   shown full-page might be a reasonable 200 DPI.
2. Downsamples only the over-sampled images to a sensible target DPI (never
   upscaling), and re-encodes them as JPEG at a chosen quality — optionally in
   grayscale.
3. Subsets embedded fonts to just the glyphs in use and rebuilds the document
   with structural cleanup (garbage collection, object-stream compression).
4. Optionally runs a final Ghostscript distillation pass for a little extra
   structural squeeze.

Because text and vector graphics stay vector, **text remains perfectly crisp**
no matter how hard the images are compressed.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design.

---

## Requirements

- **Python 3.10+**
- Python packages in [`requirements.txt`](requirements.txt) (PyMuPDF, pikepdf,
  Pillow, FastAPI, Uvicorn, python-multipart).
- **Optional:** Ghostscript, for the final distillation pass. The app runs fully
  without it; the option simply greys out. Install via your OS package manager
  (`apt install ghostscript`, `brew install ghostscript`, etc.).
- A modern browser. PDF previews use PDF.js loaded from a CDN; if you are fully
  offline the app still optimizes — only the preview panes degrade to a message.

---

## Install & run

```bash
# 1. (recommended) create a virtual environment
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 2. install dependencies
pip install -r requirements.txt

# 3. launch
python run.py
```

Then open <http://127.0.0.1:8000>.

Override host/port with environment variables:

```bash
HOST=0.0.0.0 PORT=9000 python run.py
```

---

## Usage

1. **Drop a PDF** (or several) onto the page.
2. Compact analyzes each file and highlights a **recommended profile** with a
   one-line reason.
3. Pick a profile, or type a **target size** and click *Aim*, or open
   **Advanced controls** to set DPI/quality/grayscale yourself. The **size
   meter** shows the estimated result the whole time.
4. Click **Optimize**. The meter switches from the slate "estimate" to the green
   measured result, and the optimized PDF appears in the right preview pane.
5. **Download** the result, or tweak and re-run.

---

## Project layout

```
pdf-optimizer/
├── run.py                  # launcher: python run.py
├── requirements.txt
├── README.md               # this file
├── ARCHITECTURE.md         # design + module map + extension guide
├── backend/
│   ├── app.py              # FastAPI HTTP layer (thin)
│   ├── models.py           # request schemas (pydantic)
│   ├── storage.py          # on-disk session management (no database)
│   └── engine/             # the framework-agnostic optimization engine
│       ├── __init__.py     # public API surface
│       ├── profiles.py     # OptimizeParams + named profiles
│       ├── analyzer.py     # read-only inspection / byte inventory
│       ├── classifier.py   # recommends a profile from an analysis
│       ├── estimator.py    # predicts output size before processing
│       ├── optimizer.py    # the core compression pipeline
│       ├── targeting.py    # target-size search + verification
│       └── gs.py           # optional Ghostscript backend
└── frontend/
    ├── index.html          # app shell
    ├── styles.css          # design system ("precision instrument")
    └── app.js              # all client behaviour (vanilla JS, no build step)
```

---

## HTTP API (for scripting / integration)

The engine is also usable headless; every UI action maps to one endpoint.

| Method & path                  | Purpose                                         |
|--------------------------------|-------------------------------------------------|
| `GET  /api/health`             | Liveness + whether Ghostscript is available.    |
| `GET  /api/profiles`           | The named profiles and their parameters.        |
| `POST /api/upload`             | Multipart upload; returns analysis + reco.      |
| `POST /api/estimate`           | Predict size for a profile / custom / target.   |
| `POST /api/optimize`           | Run optimization; stores the result.            |
| `GET  /api/file/{sid}/{which}` | Stream `original` or `optimized` for preview.   |
| `GET  /api/download/{sid}`     | Download the optimized PDF.                      |
| `DELETE /api/session/{sid}`    | Discard a session and its files.                |

Interactive request docs are available at `/docs` while the server runs.

The engine can also be imported directly:

```python
from engine import analyze, recommend, estimate, optimize, get_profile

a = analyze("big.pdf")
print(recommend(a).rationale)
optimize("big.pdf", "small.pdf", get_profile("strong"))
```

---

## Notes on fidelity

- **Text is never rasterized.** Only embedded raster images are recompressed.
- **Transparency is preserved.** Images with soft masks are left untouched rather
  than flattened to JPEG.
- **Page count is verified** after every run; a mismatch aborts rather than
  hands back a broken file.
- **No image is ever enlarged.** If a re-encode would grow an image, the original
  stream is kept.

---

## License

Provided as-is for you to run, modify, and extend. Bundled third-party libraries
retain their own licenses.
