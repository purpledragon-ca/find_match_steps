# STEP Component Tools

Two independent tools for working with STEP assembly files:

- [`cluster_names/`](./cluster_names/) — cluster components by name, drag-and-drop regroup in a browser, export a restructured STEP file.
- [`match_positions/`](./match_positions/) — find repeated instances of a component inside an assembly and extract per-occurrence positions, rotations, and anchor frames.

Each tool has its own `app.py` and `static/index.html`; they share no Python imports.

```
2_cluster_names/
├── cluster_names/      # Tool 1: cluster + regroup + export
├── match_positions/    # Tool 2: find instances + extract positions
└── resources/          # shared test STEP files
```

---

## Tool 1 — `cluster_names/`

A web app for visualizing STEP files in 3-D and reorganizing their assembly hierarchy into functional groups.

Upload a STEP file → components are auto-grouped by AI or prefix matching → compare component names against the 3-D mesh → drag to rearrange → export a restructured STEP file.

### Quick start

```bash
cd cluster_names

pip install -r requirements.txt

# (Optional) enable AI grouping
export DEEPSEEK_API_KEY=sk-...

# Start the server
python app.py

# Open in browser
http://127.0.0.1:8000
```

### Using the web app

**1 — Open a STEP file.** Click **Open STEP** in the toolbar and choose a `.step` / `.stp` file. The server parses the assembly hierarchy while the browser tessellates the geometry.

**2 — Auto-grouping.** Grouping runs automatically after upload. With `DEEPSEEK_API_KEY` set, components are translated to English and grouped semantically; otherwise, prefix clustering is used.

**3 — Compare names to 3-D geometry.**

| Action | Effect |
|--------|--------|
| **Click a component name** in the left panel | Mesh highlights in yellow, others dim, camera zooms |
| **Click a mesh** | Matching name highlights in the sidebar |
| **Hover a mesh** | Tooltip: original name, translation, group, instance count |
| **Hover a group header** | All meshes in that group glow |
| **⤢** (viewer top-right) | Fit all · **✕** clear selection |

Scroll to zoom · left-drag to orbit · right-drag to pan.

**4 — Reorganize groups.** Drag a component onto another group header to move it. Hover a group → ✎ to rename, ✕ to delete (members go to *Other*). Type a name into the bar above the list and click **+ Add** to create a new group.

**5 — Export.** Click **Export STEP** to download `<name>_restructured.step` with a 3-level hierarchy: `root → group → component`.

### Pre-loading a file

```bash
python app.py --step ../resources/left_shelf.step
```

### CLI pipeline (no web UI)

```bash
# Step 0 — extract names from a STEP file
python 0_get_step_component_names.py model.step
# → component_names.txt

# Step 1 — cluster and write output
python cluster_names.py --names-file component_names.txt
python cluster_names.py --names-file component_names.txt --use-api   # needs DEEPSEEK_API_KEY
python cluster_names.py model.step                                    # parse STEP directly

# Step 2 — apply the markdown config to a STEP file
python step_restructure.py model.step --config output/clusters.md
```

### `app.py` options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 8000 | HTTP port |
| `--host` | 127.0.0.1 | Bind address |
| `--step FILE` | — | Pre-load a STEP file on startup |

### `cluster_names.py` options

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

## Tool 2 — `match_positions/`

Given a **component STEP** and an **assembly STEP**, find every occurrence of the component inside the assembly and extract each occurrence's position, rotation, and anchor frame. Output is a JSON file (and optional CSV).

### Quick start

```bash
cd match_positions

pip install -r requirements.txt

# Run matcher only
python match_step_component_positions.py component.step assembly.step
# → output/component_matches.json

# Also produce a CSV side-by-side
python match_step_component_positions.py component.step assembly.step --csv matches.csv

# Restrict to a named product
python match_step_component_positions.py component.step assembly.step --name "waste bucket"

# Run matcher and launch the browser viewer
python match_step_component_positions.py component.step assembly.step --launch-ui
```

### Browser viewer

The viewer shows the assembly in 3-D with matched instances highlighted in green, plus a side panel listing each match's pose data.

```bash
# Launch standalone (viewer only, no matching)
python app.py --step assembly.step --matches output/component_matches.json
```

Controls: **⤢** fit all · **M** fit matches · **XYZ** toggle axes helpers at anchor frames · **✕** clear selection. Click a pose row (or a mesh) to focus it.

### Matching strategy

1. **Exact product name** — if the component's product name appears in the assembly, each occurrence is reported.
2. **Geometry fingerprint** — if names are not unique or not present, fall back to bounding-box dimension hashing.

For each match, the JSON includes:
- `nauo_id`, `product_name`, `occurrence_name`, parent assembly
- `pose_xyz`, `pose_rpy_deg`, `rotation_matrix` (composed from NAUO transforms)
- `anchor_origin_scene` — resolved anchor origin in scene coords
- `geometry_center`, `geometry_bottom_center` — useful when exporters bake coordinates into shapes

### `match_step_component_positions.py` key options

| Flag | Default | Description |
|------|---------|-------------|
| `component_step` | — | STEP with the component to find |
| `assembly_step` | — | STEP that may contain many instances |
| `--name` | — | Restrict to a named product |
| `--output`, `-o` | `output/component_matches.json` | JSON output path |
| `--csv` | — | Optional CSV output |
| `--target` | `root` | Which PDs to match: `root` / `children` / `leaves` / `all` |
| `--geometry-precision` | `0.01` | Bounding-box rounding precision |
| `--launch-ui` | off | Launch the browser viewer after writing matches |
| `--ui-only ASM JSON` | — | Skip matching, just view a pre-computed matches JSON |
| `--debug` | off | Print parser diagnostics to stderr |

---

## Requirements

**Python** (both tools): `fastapi`, `uvicorn[standard]`, `python-multipart`.

**CLI only** (no web UI): standard library only.

**Browser**: any modern browser with WebAssembly support. The 3-D viewer downloads `occt-import-js` (~15 MB) from CDN on first use.
