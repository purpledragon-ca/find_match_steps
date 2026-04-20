# STEP Assembly Editor

A web app for visualizing STEP files in 3-D and reorganizing their assembly hierarchy into functional groups.

Upload a STEP file → components are auto-grouped by AI or prefix matching → compare component names against the 3-D mesh → drag to rearrange → export a restructured STEP file.

![layout: left = group tree, right = 3-D viewer]

---

## Quick start

```bash
# Install dependencies
pip install fastapi "uvicorn[standard]" python-multipart

# (Optional) enable AI grouping
export DEEPSEEK_API_KEY=sk-...

# Start the server
python app.py

# Open in browser
http://127.0.0.1:8000
```

The browser will show a 3-D viewer on the right and a component group tree on the left.

---

## Using the web app

### 1 — Open a STEP file

Click **Open STEP** in the toolbar and choose a `.step` / `.stp` file.  
The app immediately does two things in parallel:

| What | Where |
|------|-------|
| Parses the assembly hierarchy | Server — builds component tree |
| Tessellates the geometry | Browser — renders 3-D mesh via WebAssembly |

A spinner shows progress for each step.

### 2 — Auto-grouping

Grouping runs automatically after upload. The toolbar badge shows which mode was used:

| Badge | Meaning |
|-------|---------|
| 🟢 **DeepSeek AI ✓** | `DEEPSEEK_API_KEY` is set. Components are translated to English and grouped by function (e.g. "Waste Management", "QR Code Components"). |
| 🔴 **No AI key** | Prefix-based clustering only — components that share a name prefix are grouped together. |

To use AI grouping, set the environment variable before starting the server:

```bash
export DEEPSEEK_API_KEY=sk-...
python app.py
```

### 3 — Compare names to 3-D geometry

The 3-D viewer on the right lets you match a component name to its physical shape:

| Action | Effect |
|--------|--------|
| **Click a component name** in the left panel | That component's mesh highlights in yellow, all others dim, and the camera zooms to it |
| **Click a mesh** in the 3-D viewer | The matching name highlights in the left panel and scrolls into view |
| **Hover over a mesh** | Tooltip shows the original name, English translation, group, and instance count |
| **Hover a group header** | All meshes belonging to that group glow in the viewer |
| **Click ⤢** (top-right of viewer) | Fit the whole model back in view |
| **Click ✕** (top-right of viewer) | Clear the current selection |

Each group has a distinct color applied in both the sidebar dot and the 3-D mesh color.

**3-D navigation:** scroll to zoom · left-drag to orbit · right-drag to pan

### 4 — Reorganize groups

| Action | How |
|--------|-----|
| Move a component to another group | Drag the component row and drop it onto a group header |
| Rename a group | Hover the group → click ✎ → type → Enter |
| Delete a group | Hover the group → click ✕ (members move to *Other*) |
| Create a new group | Type a name in the bar above the group list → **+ Add** |

### 5 — Export

Click **Export STEP**. The browser downloads `<original_name>_restructured.step` — a valid STEP file with the new 3-level hierarchy: `root → group → component`.

---

## Pre-loading a file at startup

```bash
python app.py --step resources/model.step
```

The file is parsed on startup. Open `http://127.0.0.1:8000` and the 3-D view loads automatically.

---

## Command-line pipeline (no web UI)

```bash
# Step 0 — extract component names from a STEP file
python 0_get_step_component_names.py model.step
# → component_names.txt  (name / count / level TSV)

# Step 1 — cluster names and write output files
python cluster_names.py --names-file component_names.txt
python cluster_names.py --names-file component_names.txt --use-api   # needs DEEPSEEK_API_KEY
python cluster_names.py model.step                                    # parse STEP directly

# Output in ./output/
#   clusters.txt   — ASCII tree with translations + geometry annotations
#   clusters.json  — full data with prim paths
#   clusters.md    — config for the restructure script

# Step 2 — apply the config to the STEP file
python step_restructure.py model.step --config output/clusters.md
# → model_restructured.step
```

---

## Options reference

### `app.py`

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 8000 | HTTP port |
| `--host` | 127.0.0.1 | Bind address |
| `--step FILE` | — | Pre-load a STEP file on startup |

### `cluster_names.py`

| Flag | Default | Description |
|------|---------|-------------|
| `--names-file FILE` | — | TSV from `0_get_step_component_names.py` |
| `--geometry CSV` | — | Bounding-box CSV for dimension annotations |
| `--output-dir`, `-o` | `./output/` | Where to write results |
| `--use-api` | off | Enable DeepSeek for semantic clustering |
| `--api-key KEY` | `$DEEPSEEK_API_KEY` | DeepSeek key (implies `--use-api`) |
| `--min-group` | 2 | Minimum names to form a prefix cluster |
| `--skip` | `root` | Names to exclude |

---

## Folder structure

```
cluster_names/
├── app.py                  # FastAPI web app + 3-D viewer
├── cluster_names.py        # CLI pipeline (translate, cluster, output)
├── step_restructure.py     # STEP parser and hierarchy rewriter
├── 0_get_step_component_names.py  # Extract names from STEP
├── static/
│   └── index.html          # Single-page app (Three.js + occt-import-js)
├── config/
│   └── translations.json   # Persistent Chinese→English cache (auto-seeded)
├── output/                 # Generated on each CLI run
│   ├── clusters.txt
│   ├── clusters.json
│   └── clusters.md
└── resources/              # Put your STEP files here
```

---

## Requirements

**Python** (web app): `fastapi`, `uvicorn[standard]`, `python-multipart`

**CLI pipeline**: stdlib only — no extra packages.

**Browser**: any modern browser with WebAssembly support (Chrome, Firefox, Edge, Safari).  
The 3-D viewer downloads `occt-import-js` (~15 MB WASM) from CDN on first use; subsequent loads are cached by the browser.
