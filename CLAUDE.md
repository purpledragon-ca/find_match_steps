# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install web app dependencies
pip install -r requirements.txt   # fastapi, uvicorn, python-multipart

# Run the web app (FastAPI + browser UI)
python app.py                          # http://127.0.0.1:8000
python app.py --port 9000
python app.py --step resources/model.step  # pre-load a STEP file

# Run the CLI pipeline directly (no external packages required)
python cluster_names.py --names-file component_names.txt
python cluster_names.py --names-file component_names.txt --geometry position.csv
python cluster_names.py model.step
python cluster_names.py --names-file component_names.txt --use-api   # needs DEEPSEEK_API_KEY

# Extract component names from a STEP file (step 0 of pipeline)
python 0_get_step_component_names.py model.step --output component_names.txt
```

## Architecture

There are two entry points that share a common library:

**`cluster_names.py`** — CLI pipeline. Runs end-to-end: load names → translate → cluster → write `output/{clusters.txt,clusters.json,clusters.md}`. The markdown output is designed to feed `1_restructure_step_hierarchy.py` in the sibling `usd_step_file_modify` repo.

**`app.py`** — FastAPI web app wrapping the same logic. Imports core functions from both `cluster_names.py` and `step_restructure.py`. Holds all state in a single in-memory `session` dict (single-user). REST API:
- `POST /api/upload` — parse STEP, build tree, load translations
- `POST /api/cluster` — prefix-cluster the loaded components
- `POST /api/move`, `/api/create-group`, `/api/rename-group`, `/api/delete-node` — edit groupings
- `GET /api/export` — run `restructure()` and return modified STEP file

**`step_restructure.py`** — STEP file parser and rewriter. Parses the DATA section into entity dicts, navigates `NEXT_ASSEMBLY_USAGE_OCCURENCE` relationships to build the assembly tree, creates new assembly nodes, moves components, prunes empty assemblies, and serializes back to STEP.

**`0_get_step_component_names.py`** — standalone preprocessor. Outputs a TSV (`name\tcount\tlevel`) used as input to `cluster_names.py --names-file`.

## Key design decisions

**Translation cache** (`config/translations.json`): auto-seeded from `../usd_step_file_modify/config/cn_en_translations.json` on first run. Composed translations (splitting on spaces and looking up each token) mean most names resolve without any API call.

**DeepSeek API is opt-in**: `--use-api` / `--api-key` / `$DEEPSEEK_API_KEY`. Without it, only prefix clustering and cached translations run — no network calls.

**Prefix clustering** (`prefix_cluster()` in `cluster_names.py`) is the default and the fallback when semantic clustering fails. It finds the longest common prefix across all name pairs and greedily groups by the best prefix each round.

**STEP string encoding**: component names in STEP files use `\X2\HHHH\X0\` for non-ASCII characters. `decode_step_string` / `encode_step_string` in `step_restructure.py` handle this; `cluster_names.py` has its own copy of the decoder.
