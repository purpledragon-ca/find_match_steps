# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Layout

This folder contains two independent tools, each self-contained in its own subdirectory:

```
2_cluster_names/
├── cluster_names/      # Tool 1: cluster STEP components by name, edit groupings, export
├── match_positions/    # Tool 2: find component instances in an assembly & extract positions
└── resources/          # shared test STEP files (referenced via ../resources/)
```

The two tools share no Python imports — each has its own `app.py` and `static/index.html`.

## Commands

### Clustering tool (`cluster_names/`)

```bash
cd cluster_names

# Install web app dependencies
pip install -r requirements.txt   # fastapi, uvicorn, python-multipart

# Run the web app (FastAPI + browser UI)
python app.py                          # http://127.0.0.1:8000
python app.py --port 9000
python app.py --step ../resources/left_shelf.step  # pre-load a STEP file

# Run the CLI pipeline directly (no external packages required)
python cluster_names.py --names-file component_names.txt
python cluster_names.py --names-file component_names.txt --geometry position.csv
python cluster_names.py model.step
python cluster_names.py --names-file component_names.txt --use-api   # needs DEEPSEEK_API_KEY

# Extract component names from a STEP file (step 0 of pipeline)
python 0_get_step_component_names.py model.step --output component_names.txt
```

### Position-matching tool (`match_positions/`)

```bash
cd match_positions

pip install -r requirements.txt

# Match component STEP against assembly STEP
python match_step_component_positions.py component.step assembly.step
python match_step_component_positions.py component.step assembly.step --csv matches.csv
python match_step_component_positions.py component.step assembly.step --name "part name"

# Run matcher + launch the viewer UI
python match_step_component_positions.py component.step assembly.step --launch-ui

# View a pre-computed match JSON in the browser
python app.py --step assembly.step --matches output/component_matches.json

# Or via the pre-canned start.sh (fills in specific STEP paths)
./start.sh
```

## Architecture

### `cluster_names/` — clustering tool

- **`cluster_names.py`** — CLI pipeline. Load names → translate → cluster → write `output/{clusters.txt,clusters.json,clusters.md}`. Markdown output is designed to feed `1_restructure_step_hierarchy.py` in the sibling `usd_step_file_modify` repo.
- **`app.py`** — FastAPI web app. Imports from `cluster_names.py` and `step_restructure.py`. Single-user in-memory session. Endpoints: `POST /api/upload`, `POST /api/cluster`, `POST /api/move`, `POST /api/create-group`, `POST /api/rename-group`, `POST /api/delete-node`, `GET /api/export`.
- **`step_restructure.py`** — STEP file parser and rewriter. Parses the DATA section, navigates `NEXT_ASSEMBLY_USAGE_OCCURENCE` relationships to build the assembly tree, creates new assembly nodes, moves components, prunes empty assemblies, serializes back.
- **`0_get_step_component_names.py`** — standalone preprocessor. Outputs a TSV (`name\tcount\tlevel`) used as input to `cluster_names.py --names-file`.

### `match_positions/` — position matching tool

- **`match_step_component_positions.py`** — CLI matcher. Finds repeated instances of a component STEP inside an assembly STEP, writes `output/component_matches.json` with per-occurrence position, rotation matrix, anchor frame, and pose. Supports `--launch-ui` which spawns the local viewer.
- **`app.py`** — FastAPI viewer. Reads the assembly STEP and a matches JSON, exposes `GET /api/session` and `GET /api/step-file`. No editing, no clustering.
- **`static/index.html`** — 3-D viewer highlighting matched components in green, with a pose panel listing xyz / rpy / anchor origin / rotation for each match.

## Key design decisions

**Translation cache** (`cluster_names/config/translations.json`): composed translations (splitting on spaces and looking up each token) mean most names resolve without any API call.

**DeepSeek API is opt-in**: `--use-api` / `--api-key` / `$DEEPSEEK_API_KEY`. Without it, only prefix clustering and cached translations run — no network calls.

**Prefix clustering** (`prefix_cluster()` in `cluster_names.py`) is the default and the fallback when semantic clustering fails. It finds the longest common prefix across all name pairs and greedily groups by the best prefix each round.

**STEP string encoding**: component names in STEP files use `\X2\HHHH\X0\` for non-ASCII characters. `decode_step_string` / `encode_step_string` in `cluster_names/step_restructure.py` handle this; `cluster_names.py` has its own copy of the decoder.

**Clean split**: the two tools share no Python imports. `match_positions/app.py` has its own minimal viewer code; it does not import from `cluster_names/`.
