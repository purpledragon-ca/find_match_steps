"""
Web-based STEP assembly tree editor.

Upload a STEP file -> auto-cluster components -> drag-and-drop to reorganize -> export.

Usage:
    python app.py                          # start on port 8000
    python app.py --port 9000              # custom port
    python app.py --step resources/model.step  # pre-load a STEP file
"""

import os
import re
import json
import sys
import argparse
import tempfile
from pathlib import Path
from collections import OrderedDict

from fastapi import FastAPI, UploadFile, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles

# Local imports
from step_restructure import (
    decode_step_string, encode_step_string,
    parse_step_entities, parse_args_top, extract_refs,
    build_pd_to_name, find_root_pd, find_root_sr,
    build_nauo_map, build_pds_nauo_map, build_cdsr_map, build_rr_map,
    IDPool, max_entity_id,
    _find_starting, _find_root_geom_context, _find_root_csys,
    _create_assembly_node, _move_components,
    restructure, prune_emptied_assemblies, write_step,
)
from cluster_names import (
    _load_translation_cache, _compose_translation, _clean_translation,
    prefix_cluster, _sanitize, translate_names, semantic_cluster,
)

SCRIPT_DIR = Path(__file__).resolve().parent

app = FastAPI()
app.mount('/static', StaticFiles(directory=SCRIPT_DIR / 'static'), name='static')

# ---------------------------------------------------------------------------
# Session state (single-user, in-memory)
# ---------------------------------------------------------------------------

session: dict = {
    'filename': None,
    'original_text': None,
    'entities': None,
    'pd_to_name': None,
    'nauo_map': None,
    'root_pd': None,
    'tree': None,          # full assembly tree (original)
    'groups': None,        # current group assignments: {group_name: [component_names]}
    'translations': {},
    'component_counts': {},  # {name: instance_count}
}


# ---------------------------------------------------------------------------
# Tree building from STEP entities
# ---------------------------------------------------------------------------

def _build_tree(entities: dict, pd_to_name: dict) -> dict:
    """Build assembly tree as nested dicts from STEP entities."""
    nauo_map = build_nauo_map(entities)

    # children_of: parent_pd -> [(child_pd, nauo_id)]
    children_of: dict[str, list] = {}
    for nauo_id, (parent, child) in nauo_map.items():
        children_of.setdefault(parent, []).append((child, nauo_id))

    root_pd = find_root_pd(entities, pd_to_name)

    # Count instances per name
    name_counts: dict[str, int] = {}
    for name in pd_to_name.values():
        name_counts[name] = name_counts.get(name, 0) + 1

    visited = set()

    def build_node(pd_id: str, depth: int = 0) -> dict:
        if pd_id in visited:
            return None
        visited.add(pd_id)

        name = pd_to_name.get(pd_id, pd_id)
        children = []
        for child_pd, nauo_id in children_of.get(pd_id, []):
            child_node = build_node(child_pd, depth + 1)
            if child_node:
                children.append(child_node)

        return {
            'pd_id': pd_id,
            'name': name,
            'count': name_counts.get(name, 1),
            'children': children,
        }

    return build_node(root_pd)


def _flatten_leaf_names(tree: dict) -> dict[str, int]:
    """Get all leaf component names (no children) with counts."""
    result: dict[str, int] = {}

    def walk(node):
        if not node['children']:
            name = node['name']
            result[name] = node.get('count', 1)
        for child in node['children']:
            walk(child)

    walk(tree)
    return result


def _flatten_all_names(tree: dict) -> dict[str, int]:
    """Get all component names (including assemblies) with counts."""
    result: dict[str, int] = {}

    def walk(node):
        name = node['name']
        if name != 'root':
            result[name] = node.get('count', 1)
        for child in node['children']:
            walk(child)

    walk(tree)
    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get('/')
async def index():
    html = (SCRIPT_DIR / 'static' / 'index.html').read_text(encoding='utf-8')
    return HTMLResponse(html)


@app.post('/api/upload')
async def upload(file: UploadFile):
    """Upload a STEP file, parse it, return the assembly tree."""
    text = (await file.read()).decode('utf-8', errors='replace')

    try:
        entities = parse_step_entities(text)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    pd_to_name = build_pd_to_name(entities)

    try:
        tree = _build_tree(entities, pd_to_name)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    # Load translations
    cache = _load_translation_cache()
    translations = {}
    all_names = _flatten_all_names(tree)
    for name in all_names:
        if name in cache:
            translations[name] = _clean_translation(cache[name])
        else:
            composed = _compose_translation(name, cache)
            if composed:
                translations[name] = composed
            # else: no translation available

    session.update({
        'filename': file.filename,
        'original_text': text,
        'entities': entities,
        'pd_to_name': pd_to_name,
        'nauo_map': build_nauo_map(entities),
        'root_pd': find_root_pd(entities, pd_to_name),
        'tree': tree,
        'groups': None,
        'translations': translations,
        'component_counts': all_names,
    })

    return {
        'filename': file.filename,
        'entity_count': len(entities),
        'tree': tree,
        'translations': translations,
    }


@app.get('/api/config')
async def get_config():
    """Return runtime configuration visible to the frontend."""
    return {'has_api_key': bool(os.environ.get('DEEPSEEK_API_KEY', ''))}


@app.get('/api/step-file')
async def get_step_file():
    """Return the raw STEP file so the browser can render it in 3D."""
    if not session['original_text']:
        return JSONResponse({'error': 'No file loaded'}, status_code=404)
    return Response(
        content=session['original_text'].encode('utf-8'),
        media_type='application/octet-stream',
        headers={'Content-Disposition': f'attachment; filename="{session["filename"]}"'},
    )


@app.post('/api/cluster')
async def cluster():
    """Auto-cluster: resolve translations from cache first, API only for unknowns."""
    if not session['entities']:
        return JSONResponse({'error': 'No STEP file loaded'}, status_code=400)

    counts = session['component_counts']
    filtered = {
        n: c for n, c in counts.items()
        if n != 'root' and not re.search(r'\.(stp|step)$', n, re.IGNORECASE)
    }

    names_list = sorted(filtered.keys())

    # ── Step 1: resolve translations without touching the API ──────────────
    # Start from what upload() already populated via the on-disk cache.
    translations = dict(session.get('translations', {}))

    # For any name still missing/untranslated, try the disk cache + composition.
    disk_cache = _load_translation_cache()
    for name in names_list:
        if translations.get(name) and translations[name] != name:
            continue                                   # already have a real translation
        if name in disk_cache:
            translations[name] = _clean_translation(disk_cache[name])
        else:
            composed = _compose_translation(name, disk_cache)
            if composed:
                translations[name] = composed

    # Names that still have no real translation (value equals the original name
    # or is empty) are the only ones that need the API.
    truly_unknown = [
        n for n in names_list
        if not translations.get(n) or translations[n] == n
    ]

    cache_hits = len(names_list) - len(truly_unknown)
    print(f'  Translations: {cache_hits}/{len(names_list)} from cache, '
          f'{len(truly_unknown)} need API', file=sys.stderr)

    # ── Step 2: call translation API only for unknown names ────────────────
    api_key  = os.environ.get('DEEPSEEK_API_KEY', '')
    used_api = False

    if api_key and truly_unknown:
        try:
            new_tr = translate_names(truly_unknown, api_key)
            translations.update(new_tr)
            used_api = True
            print(f'  API translated {len(new_tr)} new names', file=sys.stderr)
        except Exception as e:
            print(f'  Translation API failed: {e}', file=sys.stderr)

    session['translations'].update(translations)

    # ── Step 3: cluster ────────────────────────────────────────────────────
    groups = None
    if api_key:
        try:
            groups = semantic_cluster(names_list, translations, {}, filtered, api_key)
            if groups is not None:
                used_api = True
        except Exception as e:
            print(f'  Semantic clustering failed: {e}', file=sys.stderr)

    if groups is None:
        groups = prefix_cluster(filtered, min_group=2)

    session['groups'] = {k: v for k, v in groups.items()}

    return {
        'groups': session['groups'],
        'translations': session['translations'],
        'used_api': used_api,
        'cache_hits': cache_hits,
        'api_calls': len(truly_unknown) if (api_key and truly_unknown) else 0,
    }


@app.post('/api/move')
async def move(request: Request):
    """Move a component from one group to another."""
    data = await request.json()
    comp_name = data.get('component')
    target_group = data.get('target_group')

    if not session['groups']:
        return JSONResponse({'error': 'No groups defined'}, status_code=400)

    # Remove from current group
    for group_name, members in session['groups'].items():
        if comp_name in members:
            members.remove(comp_name)
            break

    # Add to target group
    if target_group not in session['groups']:
        session['groups'][target_group] = []
    session['groups'][target_group].append(comp_name)

    # Clean up empty groups (except "Other")
    session['groups'] = {
        k: v for k, v in session['groups'].items()
        if v or k == 'Other'
    }

    return {'groups': session['groups']}


@app.post('/api/create-group')
async def create_group(request: Request):
    """Create a new empty group."""
    data = await request.json()
    name = data.get('name', 'New Group')

    if not session['groups']:
        session['groups'] = {}

    if name in session['groups']:
        return JSONResponse({'error': f'Group "{name}" already exists'}, status_code=400)

    session['groups'][name] = []
    return {'groups': session['groups']}


@app.post('/api/rename-group')
async def rename_group(request: Request):
    """Rename a group."""
    data = await request.json()
    old_name = data.get('old_name')
    new_name = data.get('new_name')

    if old_name not in session['groups']:
        return JSONResponse({'error': f'Group "{old_name}" not found'}, status_code=404)
    if new_name in session['groups']:
        return JSONResponse({'error': f'Group "{new_name}" already exists'}, status_code=400)

    # Preserve order
    new_groups = {}
    for k, v in session['groups'].items():
        if k == old_name:
            new_groups[new_name] = v
        else:
            new_groups[k] = v
    session['groups'] = new_groups

    return {'groups': session['groups']}


@app.post('/api/delete-node')
async def delete_node(request: Request):
    """Delete a component or an entire group."""
    data = await request.json()
    node_name = data.get('name')
    node_type = data.get('type', 'component')  # 'component' or 'group'

    if not session['groups']:
        return JSONResponse({'error': 'No groups defined'}, status_code=400)

    if node_type == 'group':
        if node_name in session['groups']:
            # Move members to Other
            orphans = session['groups'].pop(node_name)
            if orphans:
                session['groups'].setdefault('Other', []).extend(orphans)
        return {'groups': session['groups']}

    # Delete component from all groups
    for members in session['groups'].values():
        if node_name in members:
            members.remove(node_name)

    return {'groups': session['groups']}


@app.get('/api/export')
async def export():
    """Export the modified STEP file."""
    if not session['original_text'] or not session['groups']:
        return JSONResponse({'error': 'No data to export'}, status_code=400)

    entities = session['entities']
    original_text = session['original_text']
    groups = session['groups']

    # Convert groups to restructure tree format: {L1: {L2: [components]}}
    # Each group becomes an L1 node; components go directly under it as L2
    tree = OrderedDict()
    for group_name, members in groups.items():
        if not members:
            continue
        l2_map = OrderedDict()
        for comp_name in members:
            l2_map[comp_name] = [comp_name]
        tree[group_name] = l2_map

    if not tree:
        return JSONResponse({'error': 'No groups with components to export'}, status_code=400)

    try:
        modified_entities, new_lines = restructure(entities, tree)
        modified_entities, deleted_ids = prune_emptied_assemblies(
            entities, modified_entities)

        result_text = write_step(
            original_text, modified_entities, new_lines,
            output_path=None, deleted_ids=deleted_ids)

        filename = session.get('filename', 'output.step')
        stem = Path(filename).stem
        export_name = f'{stem}_restructured.step'

        return Response(
            content=result_text.encode('utf-8'),
            media_type='application/octet-stream',
            headers={'Content-Disposition': f'attachment; filename="{export_name}"'},
        )

    except Exception as e:
        return JSONResponse({'error': f'Export failed: {e}'}, status_code=500)


@app.get('/api/session')
async def get_session():
    """Get current session state (for page reload)."""
    if not session['entities']:
        return {'loaded': False}

    return {
        'loaded': True,
        'filename': session['filename'],
        'tree': session['tree'],
        'groups': session['groups'],
        'translations': session['translations'],
        'component_counts': session['component_counts'],
    }


# ---------------------------------------------------------------------------
# Pre-load a STEP file at startup (optional)
# ---------------------------------------------------------------------------

def preload_step(step_path: Path):
    """Pre-load a STEP file into the session."""
    text = step_path.read_text(encoding='utf-8', errors='replace')
    entities = parse_step_entities(text)
    pd_to_name = build_pd_to_name(entities)
    tree = _build_tree(entities, pd_to_name)

    cache = _load_translation_cache()
    translations = {}
    all_names = _flatten_all_names(tree)
    for name in all_names:
        if name in cache:
            translations[name] = _clean_translation(cache[name])
        else:
            composed = _compose_translation(name, cache)
            if composed:
                translations[name] = composed

    session.update({
        'filename': step_path.name,
        'original_text': text,
        'entities': entities,
        'pd_to_name': pd_to_name,
        'nauo_map': build_nauo_map(entities),
        'root_pd': find_root_pd(entities, pd_to_name),
        'tree': tree,
        'groups': None,
        'translations': translations,
        'component_counts': all_names,
    })
    print(f'Pre-loaded: {step_path.name} ({len(entities)} entities)', file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import uvicorn

    parser = argparse.ArgumentParser(description='STEP assembly tree editor')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--step', metavar='FILE', help='Pre-load a STEP file')
    args = parser.parse_args()

    if args.step:
        preload_step(Path(args.step))

    print(f'Starting server at http://{args.host}:{args.port}', file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level='warning')
