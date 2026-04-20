"""
Cluster STEP component names using semantic understanding + geometry similarity.

Pipeline:
    1. Load names (from STEP file or TSV)
    2. Load geometry (from bounding-box CSV, optional)
    3. Translate names via DeepSeek API (cached in config/translations.json)
    4. Semantic clustering via DeepSeek (understands functional relationships)
    5. Fallback: prefix-based clustering when API is unavailable
    6. Output: tree, JSON, markdown → output/

Usage:
    # Default: cached translations + prefix clustering
    python cluster_names.py --names-file component_names.txt

    # With geometry data
    python cluster_names.py --names-file component_names.txt --geometry position.csv

    # From STEP file directly
    python cluster_names.py model.step

    # Enable DeepSeek API for semantic clustering
    python cluster_names.py --names-file component_names.txt --use-api
    python cluster_names.py --names-file component_names.txt --api-key sk-...

Environment:
    DEEPSEEK_API_KEY   Your DeepSeek API key (used when --use-api is set)
"""

import re
import os
import sys
import json
import math
import argparse
import urllib.request
import urllib.error
from collections import OrderedDict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / 'config'
OUTPUT_DIR = SCRIPT_DIR / 'output'

DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
DEEPSEEK_MODEL = 'deepseek-chat'

DEFAULT_SEED_TRANSLATIONS = SCRIPT_DIR.parent / 'usd_step_file_modify' / 'config' / 'cn_en_translations.json'


# ===================================================================
# STEP string helpers
# ===================================================================

def decode_step_string(s):
    def _replace(m):
        h = m.group(1)
        try:
            return ''.join(chr(int(h[i:i+4], 16)) for i in range(0, len(h), 4))
        except Exception:
            return m.group(0)
    return re.sub(r'\\X2\\([0-9A-Fa-f]+)\\X0\\', _replace, s)


# ===================================================================
# Name loaders
# ===================================================================

def load_names_from_step(step_path: Path) -> dict[str, int]:
    """Extract {component_name: instance_count} from a STEP file."""
    content = step_path.read_text(encoding='utf-8', errors='replace')
    records = {}
    for m in re.finditer(r'#(\d+)\s*=\s*(.+?);', content, re.DOTALL):
        records[int(m.group(1))] = m.group(2).replace('\n', ' ').strip()

    ref_pat = re.compile(r'#(\d+)')
    products = {}
    for num, body in records.items():
        if body.startswith('PRODUCT('):
            m = re.match(r"PRODUCT\s*\(\s*'([^']*)'\s*,\s*'([^']*)'", body)
            if m:
                products[num] = decode_step_string(m.group(1) or m.group(2))

    pdf_to_prod = {}
    for num, body in records.items():
        if body.startswith('PRODUCT_DEFINITION_FORMATION'):
            m = re.match(
                r"PRODUCT_DEFINITION_FORMATION[A-Z_]*\s*\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*#(\d+)",
                body)
            if m:
                pdf_to_prod[num] = int(m.group(1))

    counts: dict[str, int] = {}
    for num, body in records.items():
        if body.startswith('PRODUCT_DEFINITION('):
            for ref in [int(r) for r in ref_pat.findall(body)]:
                if ref in pdf_to_prod:
                    name = products.get(pdf_to_prod[ref], f'unknown_{ref}')
                    counts[name] = counts.get(name, 0) + 1
                    break
    return counts


def load_names_from_tsv(tsv_path: Path) -> dict[str, int]:
    """Load {name: count} from a tab-separated names file."""
    counts: dict[str, int] = {}
    for line in tsv_path.read_text(encoding='utf-8').splitlines():
        if line.startswith('name\t') or not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) >= 2:
            name = parts[0].strip()
            try:
                count = int(parts[1])
            except ValueError:
                count = 1
            if name:
                counts[name] = count
    return counts


# ===================================================================
# Geometry loader (bounding-box CSV from get_step_component_positions.py)
# ===================================================================

def load_geometry_csv(csv_path: Path) -> dict[str, dict]:
    """
    Load per-component geometry from a bounding-box CSV.

    Expected columns: #, name, center_x_mm, ..., xmin_mm, xmax_mm, ymin_mm, ymax_mm, zmin_mm, zmax_mm

    Returns {name: {volume_mm3, diagonal_mm, bbox}} averaged across instances.
    """
    lines = csv_path.read_text(encoding='utf-8').splitlines()
    if not lines:
        return {}

    header = [h.strip() for h in lines[0].split(',')]
    col = {h: i for i, h in enumerate(header)}

    needed = {'name', 'xmin_mm', 'xmax_mm', 'ymin_mm', 'ymax_mm', 'zmin_mm', 'zmax_mm'}
    if not needed.issubset(col):
        print(f'  WARNING: geometry CSV missing columns {needed - set(col)}; skipping.',
              file=sys.stderr)
        return {}

    # Accumulate per-name (first instance wins — geometry should be identical)
    result: dict[str, dict] = {}
    for line in lines[1:]:
        vals = line.split(',')
        if len(vals) < len(header):
            continue
        name = vals[col['name']].strip()
        if name in result:
            continue
        try:
            xmin = float(vals[col['xmin_mm']])
            xmax = float(vals[col['xmax_mm']])
            ymin = float(vals[col['ymin_mm']])
            ymax = float(vals[col['ymax_mm']])
            zmin = float(vals[col['zmin_mm']])
            zmax = float(vals[col['zmax_mm']])
        except (ValueError, IndexError):
            continue

        dx, dy, dz = xmax - xmin, ymax - ymin, zmax - zmin
        result[name] = {
            'volume_mm3': round(dx * dy * dz, 1),
            'diagonal_mm': round(math.sqrt(dx**2 + dy**2 + dz**2), 1),
            'size_x': round(dx, 1),
            'size_y': round(dy, 1),
            'size_z': round(dz, 1),
        }
    return result


# ===================================================================
# DeepSeek API
# ===================================================================

def _call_deepseek(messages: list[dict], api_key: str) -> str:
    """Call DeepSeek chat API. Returns the assistant message content."""
    payload = json.dumps({
        'model': DEEPSEEK_MODEL,
        'messages': messages,
        'response_format': {'type': 'json_object'},
        'temperature': 0.1,
    }).encode()

    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        return body['choices'][0]['message']['content']
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ''
        print(f'  DeepSeek API error {e.code}: {err_body[:200]}', file=sys.stderr)
        raise
    except Exception as e:
        print(f'  DeepSeek API error: {e}', file=sys.stderr)
        raise


# ===================================================================
# Translation with cache
# ===================================================================

def _load_translation_cache() -> dict[str, str]:
    path = CONFIG_DIR / 'translations.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return {}


def _save_translation_cache(cache: dict[str, str]):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / 'translations.json'
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def translate_names(
    names: list[str],
    api_key: str,
) -> dict[str, str]:
    """
    Translate component names to English via DeepSeek, with persistent cache.

    Only names NOT already in config/translations.json hit the API.
    Returns {original_name: english_translation} for all names.
    """
    cache = _load_translation_cache()

    # Figure out which full component names are already translated
    # Also try composing from cached sub-phrase translations
    result: dict[str, str] = {}
    untranslated: list[str] = []

    for name in names:
        if name in cache:
            result[name] = _clean_translation(cache[name])
        else:
            # Try composing: split name and translate each known token
            composed = _compose_translation(name, cache)
            if composed:
                result[name] = composed
                cache[name] = composed
            else:
                untranslated.append(name)

    if not untranslated:
        print(f'  All {len(names)} names found in cache', file=sys.stderr)
        _save_translation_cache(cache)
        return result

    print(f'  Translating {len(untranslated)} new names via DeepSeek ...', file=sys.stderr)

    # Build prompt with numbered list
    name_list = '\n'.join(f'{i+1}. {n}' for i, n in enumerate(untranslated))
    messages = [
        {'role': 'system', 'content': (
            'You are a manufacturing/CAD terminology translator.\n'
            'Rules:\n'
            '- Translate to short, clear English (2-5 words ideal)\n'
            '- Focus on the functional meaning, drop filler words\n'
            '- Move part numbers (like 25001A.U06.P12) to the END in parentheses\n'
            '- Example: "25001A.U06.P12 润柱废液桶放置槽" → "Waste Bin Placement Trough (25001A.U06.P12)"\n'
            '- Example: "小货架 废液槽带拉手组件" → "Waste Trough Handle Assembly"\n'
            '- Return a JSON object mapping each original name to its English translation.'
        )},
        {'role': 'user', 'content': (
            f'Translate these component names from a STEP assembly file:\n\n{name_list}\n\n'
            'Return JSON: {{"original_name": "Short English (PART_NUM)", ...}}'
        )},
    ]

    try:
        raw = _call_deepseek(messages, api_key)
        translations = json.loads(raw)

        for name in untranslated:
            eng = _clean_translation(translations.get(name, name))
            result[name] = eng
            cache[name] = eng

        print(f'  Got {len(translations)} translations', file=sys.stderr)
    except Exception:
        print(f'  Translation failed; using original names as fallback', file=sys.stderr)
        for name in untranslated:
            result[name] = name

    _save_translation_cache(cache)
    return result


_PART_NUMBER_RE = re.compile(
    r'^(?=.*\d)'            # must contain at least one digit
    r'[A-Za-z0-9._\-（）()]{4,}$'  # e.g. 25001A.U06.P12, AOB05-4040A-L200
)

_VERSION_RE = re.compile(r'^v\d')  # e.g. v1.0

# Short uppercase tokens that are NOT part numbers (known abbreviations)
_NOT_PART_NUMBERS = {'QR', 'UV', 'AC', 'DC', 'IO', 'ID', 'PC', 'LED', 'USB', 'CNC'}


def _is_part_number(token: str) -> bool:
    """Check if a token looks like a part number / model code."""
    if token.upper() in _NOT_PART_NUMBERS:
        return False
    return bool(_PART_NUMBER_RE.match(token))


def _clean_translation(english: str) -> str:
    """
    Post-process a translation to be short and clear:
    - Move part numbers to the end in parentheses
    - Normalize full-width parentheses
    - Strip redundant prefixes like "Copy of ..."
    """
    # Normalize full-width parens to ASCII
    english = english.replace('（', '(').replace('）', ')')

    # Strip "Copy of XXX^" prefixes — keep only the part after ^
    if english.startswith('Copy of ') and '^' in english:
        english = english.split('^', 1)[1].strip()

    words = english.split()
    meaningful = []
    part_nums = []
    for w in words:
        if _is_part_number(w) or _VERSION_RE.match(w):
            part_nums.append(w)
        else:
            meaningful.append(w)

    result = ' '.join(meaningful)
    if part_nums:
        result = f'{result} ({" ".join(part_nums)})' if result else ' '.join(part_nums)
    return result.strip()


def _compose_translation(name: str, cache: dict[str, str]) -> str | None:
    """
    Try to translate a full component name by composing from cached sub-phrase
    translations. E.g. "小货架 废液桶标准" → "Waste Bin Standard (Small Shelf)".

    Handles:
    - "Copy of XXX^YYY" → translate YYY, note Copy variant
    - Part numbers and version tags → moved to end in parentheses
    Returns the composed English string, or None if any token is unknown.
    """
    working = name

    # Handle "Copy of XXX^Real Name" pattern
    copy_prefix = ''
    if working.startswith('Copy of ') and '^' in working:
        copy_part, working = working.split('^', 1)
        # Extract variant id from "Copy of J-CXA01-60-NJ-001"
        copy_id = copy_part.replace('Copy of ', '').strip()
        copy_prefix = copy_id

    tokens = working.split()
    if not tokens:
        return None

    meaningful = []
    part_nums = []
    for token in tokens:
        if token in cache:
            meaningful.append(cache[token])
        elif _is_part_number(token) or _VERSION_RE.match(token):
            part_nums.append(token)
        elif re.match(r'^[A-Za-z0-9._\-（）()\[\]^]+$', token):
            meaningful.append(token)
        else:
            return None

    if not meaningful:
        return None

    result = ' '.join(meaningful)
    tags = part_nums.copy()
    if copy_prefix:
        tags.insert(0, copy_prefix)
    if tags:
        result = f'{result} ({" ".join(tags)})'
    return result.strip()


# ===================================================================
# Semantic clustering via DeepSeek
# ===================================================================

def semantic_cluster(
    names: list[str],
    translations: dict[str, str],
    geometry: dict[str, dict],
    counts: dict[str, int],
    api_key: str,
) -> OrderedDict[str, list[str]] | None:
    """
    Ask DeepSeek to group components into functional categories.

    Returns {group_name: [original_name, ...]} or None on failure.
    """
    print(f'  Semantic clustering {len(names)} names via DeepSeek ...', file=sys.stderr)

    # Build component descriptions
    comp_lines = []
    for i, name in enumerate(names):
        eng = translations.get(name, name)
        count = counts.get(name, 1)
        parts = [f'{i+1}. "{name}" — {eng} (×{count})']

        geo = geometry.get(name)
        if geo:
            parts.append(
                f'   [size: {geo["size_x"]}×{geo["size_y"]}×{geo["size_z"]} mm, '
                f'volume: {geo["volume_mm3"]} mm³]'
            )
        comp_lines.append('\n'.join(parts))

    comp_text = '\n'.join(comp_lines)

    messages = [
        {'role': 'system', 'content': (
            'You are a manufacturing engineer organizing CAD assembly components.\n'
            'Group components into functional categories based on:\n'
            '  1. Functional purpose (waste management, structural support, QR codes, etc.)\n'
            '  2. Components that form a natural set (e.g. a trough + its cover + its stop block)\n'
            '  3. Geometry similarity — similar dimensions suggest similar parts\n'
            '  4. Part number families (same prefix = same family)\n\n'
            'Rules:\n'
            '  - Every component must appear in exactly one group\n'
            '  - Use the ORIGINAL Chinese name as the key (not the English translation)\n'
            '  - Group names should be in English, concise, and descriptive\n'
            '  - Prefer fewer, larger groups over many tiny groups\n'
            '  - Components that are clearly a set (bin + trough + cover) go together\n'
            '  - Structural aluminum extrusions (AOB05-*) should group together\n'
        )},
        {'role': 'user', 'content': (
            f'Group these {len(names)} components:\n\n{comp_text}\n\n'
            'Return JSON:\n'
            '{\n'
            '  "Group Name": {\n'
            '    "members": ["original_name_1", "original_name_2"],\n'
            '    "description": "Brief description"\n'
            '  }\n'
            '}'
        )},
    ]

    try:
        raw = _call_deepseek(messages, api_key)
        data = json.loads(raw)

        result = OrderedDict()
        assigned = set()
        for group_name, info in data.items():
            members = info.get('members', []) if isinstance(info, dict) else info
            # Validate: only keep members that are actual component names
            valid = [m for m in members if m in counts]
            if valid:
                result[group_name] = valid
                assigned.update(valid)

        # Check for any names the LLM missed
        missed = [n for n in names if n not in assigned]
        if missed:
            result['Other'] = missed
            print(f'  {len(missed)} components not assigned; added to "Other"',
                  file=sys.stderr)

        print(f'  Created {len(result)} semantic groups', file=sys.stderr)
        return result

    except Exception as e:
        print(f'  Semantic clustering failed: {e}', file=sys.stderr)
        return None


# ===================================================================
# Geometry-based similarity (post-processing)
# ===================================================================

def find_geometry_sets(
    geometry: dict[str, dict],
    tolerance: float = 0.05,
) -> list[set[str]]:
    """
    Find sets of components with near-identical geometry (within tolerance).

    Returns list of sets, each containing names of geometrically similar components.
    """
    names = list(geometry.keys())
    sets: list[set[str]] = []
    used = set()

    for i, a in enumerate(names):
        if a in used:
            continue
        ga = geometry[a]
        group = {a}
        for b in names[i+1:]:
            if b in used:
                continue
            gb = geometry[b]
            if ga['volume_mm3'] == 0 or gb['volume_mm3'] == 0:
                continue
            vol_ratio = abs(ga['volume_mm3'] - gb['volume_mm3']) / max(ga['volume_mm3'], gb['volume_mm3'])
            diag_ratio = abs(ga['diagonal_mm'] - gb['diagonal_mm']) / max(ga['diagonal_mm'], gb['diagonal_mm'])
            if vol_ratio <= tolerance and diag_ratio <= tolerance:
                group.add(b)
        if len(group) >= 2:
            sets.append(group)
            used.update(group)

    return sets


# ===================================================================
# Prefix clustering (fallback)
# ===================================================================

_SEPARATORS = [' ', '_', '-', '.']


def _find_prefix(names: list[str], sep: str | None) -> str | None:
    if len(names) < 2:
        return None
    shortest = min(names, key=len)
    lcp_len = 0
    for i, ch in enumerate(shortest):
        if all(n[i] == ch for n in names):
            lcp_len = i + 1
        else:
            break
    if lcp_len == 0:
        return None

    lcp = shortest[:lcp_len]
    seps = [sep] if sep else _SEPARATORS
    best_cut = max((lcp.rfind(s) for s in seps), default=-1)
    if best_cut <= 0:
        return None
    prefix = lcp[:best_cut].rstrip()
    return prefix if len(prefix) >= 2 else None


def prefix_cluster(
    name_counts: dict[str, int],
    min_group: int = 2,
) -> OrderedDict[str, list[str]]:
    """Fallback prefix-based clustering."""
    names = list(name_counts.keys())
    remaining = set(names)
    clusters: dict[str, list[str]] = {}

    while len(remaining) >= min_group:
        rem_list = sorted(remaining)
        candidates: dict[str, list[str]] = {}
        for i, a in enumerate(rem_list):
            for b in rem_list[i+1:]:
                pfx = _find_prefix([a, b], None)
                if pfx and pfx not in candidates:
                    candidates[pfx] = []
        for pfx in candidates:
            candidates[pfx] = [n for n in rem_list if n.startswith(pfx)]

        best_pfx, best_members = None, []
        for pfx, members in sorted(candidates.items(), key=lambda kv: (-len(kv[1]), -len(kv[0]))):
            if len(members) >= min_group and len(members) > len(best_members):
                best_pfx, best_members = pfx, members

        if not best_pfx:
            break
        clusters[best_pfx] = best_members
        remaining -= set(best_members)

    result = OrderedDict()
    for pfx in sorted(clusters, key=lambda p: -sum(name_counts.get(n, 0) for n in clusters[p])):
        result[pfx] = clusters[pfx]
    if remaining:
        result['Other'] = sorted(remaining)
    return result


# ===================================================================
# Output formatters
# ===================================================================

def _sanitize(name: str) -> str:
    s = name.replace(' ', '_').replace('/', '_').strip('_')
    return s or 'unnamed'


def format_tree(
    groups: OrderedDict[str, list[str]],
    counts: dict[str, int],
    translations: dict[str, str],
    geometry: dict[str, dict],
) -> str:
    lines = []
    items = list(groups.items())

    for gi, (group_name, members) in enumerate(items):
        is_last_group = (gi == len(items) - 1)
        branch = '\u2514\u2500\u2500 ' if is_last_group else '\u251c\u2500\u2500 '
        lines.append(f'{branch}{group_name}/')

        for mi, name in enumerate(sorted(members)):
            is_last = (mi == len(members) - 1)
            indent = '    ' if is_last_group else '\u2502   '
            sub_branch = '\u2514\u2500\u2500 ' if is_last else '\u251c\u2500\u2500 '

            eng = translations.get(name, '')
            count = counts.get(name, 1)
            count_str = f'  (\u00d7{count})' if count > 1 else ''
            eng_str = f'  [{eng}]' if eng and eng != name else ''
            geo = geometry.get(name)
            geo_str = ''
            if geo:
                geo_str = f'  {geo["size_x"]}\u00d7{geo["size_y"]}\u00d7{geo["size_z"]}mm'

            lines.append(f'{indent}{sub_branch}{name}{count_str}{eng_str}{geo_str}')

    return '\n'.join(lines)


def format_json(
    groups: OrderedDict[str, list[str]],
    counts: dict[str, int],
    translations: dict[str, str],
    geometry: dict[str, dict],
) -> str:
    output = OrderedDict()
    for group_name, members in groups.items():
        group_data = []
        for name in sorted(members):
            entry = {
                'original_name': name,
                'english': translations.get(name, name),
                'prim_path': f'/{_sanitize(group_name)}/{_sanitize(name)}',
                'count': counts.get(name, 1),
            }
            geo = geometry.get(name)
            if geo:
                entry['geometry'] = geo
            group_data.append(entry)
        output[group_name] = group_data

    return json.dumps(output, ensure_ascii=False, indent=2)


def format_markdown(
    groups: OrderedDict[str, list[str]],
) -> str:
    lines = []
    for group_name, members in groups.items():
        lines.append(group_name)
        lines.append('')
        for name in sorted(members):
            lines.append(f'##{name}')
            lines.append(f'####{name}')
            lines.append('')
        lines.append('')
    return '\n'.join(lines)


# ===================================================================
# Main pipeline
# ===================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Cluster STEP component names using semantic + geometry similarity.',
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('step_file', nargs='?',
                        help='Path to .step file')
    source.add_argument('--names-file', metavar='FILE',
                        help='TSV file with component names (from script 0)')

    parser.add_argument('--geometry', metavar='CSV',
                        help='Bounding-box CSV (from get_step_component_positions.py)')
    parser.add_argument('--output-dir', '-o', metavar='DIR',
                        help=f'Output directory (default: {OUTPUT_DIR})')
    parser.add_argument('--use-api', action='store_true',
                        help='Enable DeepSeek API for semantic clustering + translation')
    parser.add_argument('--api-key', metavar='KEY',
                        help='DeepSeek API key (default: $DEEPSEEK_API_KEY, implies --use-api)')
    parser.add_argument('--seed-translations', metavar='JSON',
                        default=str(DEFAULT_SEED_TRANSLATIONS),
                        help=f'Seed translation cache from a JSON file '
                             f'(default: {DEFAULT_SEED_TRANSLATIONS})')
    parser.add_argument('--min-group', type=int, default=2,
                        help='Min names to form a prefix cluster (default: 2)')
    parser.add_argument('--skip', nargs='*', default=['root'],
                        help='Names to skip (default: root)')

    args = parser.parse_args()

    # --- Step 1: Load names ---
    print('Step 1: Loading names ...', file=sys.stderr)
    if args.names_file:
        path = Path(args.names_file)
        if not path.exists():
            print(f'Error: {path} not found.', file=sys.stderr); sys.exit(1)
        name_counts = load_names_from_tsv(path)
    elif args.step_file:
        path = Path(args.step_file)
        if not path.exists():
            print(f'Error: {path} not found.', file=sys.stderr); sys.exit(1)
        name_counts = load_names_from_step(path)
    else:
        parser.error('Provide a STEP file or --names-file')

    skip = set(args.skip or [])
    # Filter out root, file references, etc.
    name_counts = {
        n: c for n, c in name_counts.items()
        if n not in skip and not re.search(r'\.(stp|step)$', n, re.IGNORECASE)
    }
    names = sorted(name_counts.keys())
    print(f'  {len(names)} unique names, {sum(name_counts.values())} total instances',
          file=sys.stderr)

    # --- Step 2: Load geometry (optional) ---
    geometry: dict[str, dict] = {}
    if args.geometry:
        geo_path = Path(args.geometry)
        if geo_path.exists():
            print('Step 2: Loading geometry ...', file=sys.stderr)
            geometry = load_geometry_csv(geo_path)
            matched = sum(1 for n in names if n in geometry)
            print(f'  {matched}/{len(names)} names have geometry data', file=sys.stderr)
        else:
            print(f'  WARNING: geometry file {geo_path} not found; skipping.',
                  file=sys.stderr)
    else:
        print('Step 2: No geometry CSV provided; skipping.', file=sys.stderr)

    # --- Resolve API key ---
    api_key = args.api_key or os.environ.get('DEEPSEEK_API_KEY', '')
    use_api = args.use_api or bool(args.api_key)  # opt-in only

    if use_api and not api_key:
        print('  WARNING: --use-api set but no API key found.\n'
              '  Set DEEPSEEK_API_KEY or use --api-key KEY.',
              file=sys.stderr)
        use_api = False

    # --- Seed translation cache if requested (always, regardless of API) ---
    if args.seed_translations:
        seed_path = Path(args.seed_translations)
        if seed_path.exists():
            cache = _load_translation_cache()
            seed_data = json.loads(seed_path.read_text(encoding='utf-8'))
            new_count = sum(1 for k in seed_data if k not in cache)
            cache.update(seed_data)
            _save_translation_cache(cache)
            print(f'  Seeded {new_count} new entries from {seed_path.name} '
                  f'({len(cache)} total cached)', file=sys.stderr)

    # --- Step 3: Translate ---
    translations: dict[str, str] = {}
    if use_api:
        print('Step 3: Translating names ...', file=sys.stderr)
        translations = translate_names(names, api_key)
    else:
        print('Step 3: Loading cached translations ...', file=sys.stderr)
        cache = _load_translation_cache()
        # Try composing translations from cached sub-phrases
        for n in names:
            if n in cache:
                translations[n] = _clean_translation(cache[n])
            else:
                composed = _compose_translation(n, cache)
                translations[n] = composed if composed else n
        cached_count = sum(1 for n in names if translations[n] != n)
        print(f'  {cached_count}/{len(names)} names translated from cache',
              file=sys.stderr)

    # --- Step 4: Cluster ---
    groups: OrderedDict[str, list[str]]

    if use_api:
        print('Step 4: Semantic clustering ...', file=sys.stderr)
        result = semantic_cluster(names, translations, geometry, name_counts, api_key)
        if result:
            groups = result
        else:
            print('  Falling back to prefix clustering ...', file=sys.stderr)
            groups = prefix_cluster(name_counts, min_group=args.min_group)
    else:
        print('Step 4: Prefix clustering ...', file=sys.stderr)
        groups = prefix_cluster(name_counts, min_group=args.min_group)

    # --- Geometry similarity annotations ---
    if geometry:
        geo_sets = find_geometry_sets(geometry)
        if geo_sets:
            print(f'\n  Geometry similarity sets ({len(geo_sets)} found):',
                  file=sys.stderr)
            for s in geo_sets:
                names_str = ', '.join(sorted(s))
                geo0 = geometry[next(iter(s))]
                print(f'    [{geo0["size_x"]}x{geo0["size_y"]}x{geo0["size_z"]}mm] '
                      f'{names_str}', file=sys.stderr)

    # --- Step 5: Output ---
    out_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'\nStep 5: Writing output to {out_dir}/', file=sys.stderr)

    tree_text = format_tree(groups, name_counts, translations, geometry)
    json_text = format_json(groups, name_counts, translations, geometry)
    md_text = format_markdown(groups)

    (out_dir / 'clusters.txt').write_text(tree_text + '\n', encoding='utf-8')
    (out_dir / 'clusters.json').write_text(json_text + '\n', encoding='utf-8')
    (out_dir / 'clusters.md').write_text(md_text + '\n', encoding='utf-8')

    print(f'  clusters.txt   (tree view)', file=sys.stderr)
    print(f'  clusters.json  (full data + prim paths)', file=sys.stderr)
    print(f'  clusters.md    (markdown config for restructure script)', file=sys.stderr)

    print(f'\n{len(groups)} groups, {len(names)} components:\n', file=sys.stderr)
    print(tree_text)


if __name__ == '__main__':
    main()
