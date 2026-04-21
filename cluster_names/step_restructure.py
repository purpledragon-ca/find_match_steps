"""
Restructure the assembly hierarchy of a STEP file using a markdown config.

Produces a 3-level tree under root:

    root
    ├── Static                          ← L1  (plain text in markdown)
    │   ├── 12g Silica Column Bracket   ← L2  (## heading)
    │   │   └── [moved] 25001A…         ← components  (#### heading)
    │   └── bin slot                    ← L2  (# heading)
    │       └── [moved] 25001A…
    └── Dynamic                         ← L1
        ├── 12g Silica Column           ← L2
        │   └── [moved] 12g硅胶柱

Markdown format
---------------
  Static                      ← L1 name  (no leading #)

  ##12g Silica Column Bracket ← L2 name  (# or ## prefix)
  ####25001A.U06.P18…         ← component to move  (#### prefix)

  #bin slot                   ← L2 name
  ####25001A.U06.P12…

  Dynamic                     ← next L1

  ##fraction_waste_bin
  ####小货架 废液槽带拉手组件

Rules:
  • 0 leading #  → L1 group (child of root)
  • 1–2 leading # → L2 group (child of current L1)
  • 4 leading #   → component name to find & move under current L2
  • Blank lines   → separator (reset current L2; double blank resets L1 too)

Usage:
    python restructure_step_hierarchy.py <step_file> --config <md_file> [--output <out>]
    python restructure_step_hierarchy.py <step_file> --map L1 L2 component [--map …]
"""

import re
import sys
import argparse
from collections import OrderedDict
from pathlib import Path


# ---------------------------------------------------------------------------
# STEP string helpers
# ---------------------------------------------------------------------------

def decode_step_string(s: str) -> str:
    def replace(m):
        hex_chars = m.group(1)
        try:
            return ''.join(chr(int(hex_chars[i:i+4], 16))
                           for i in range(0, len(hex_chars), 4))
        except Exception:
            return m.group(0)
    return re.sub(r'\\X2\\([0-9A-Fa-f]+)\\X0\\', replace, s)


def encode_step_string(s: str) -> str:
    try:
        s.encode('ascii')
        return f"'{s}'"
    except UnicodeEncodeError:
        return f"'\\X2\\{''.join(f'{ord(c):04X}' for c in s)}\\X0\\'"


# ---------------------------------------------------------------------------
# Entity parsing
# ---------------------------------------------------------------------------

def parse_step_entities(text: str) -> dict:
    text_nc = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    m = re.search(r'\bDATA\s*;(.*?)\bENDSEC\b', text_nc, re.DOTALL | re.IGNORECASE)
    if not m:
        raise ValueError("No DATA section found in STEP file")
    entities: dict[str, str] = {}
    for stmt in m.group(1).split(';'):
        stmt = stmt.strip()
        if not stmt:
            continue
        m2 = re.match(r'(#\d+)\s*=\s*(.*)', stmt, re.DOTALL)
        if m2:
            entities[m2.group(1)] = ' '.join(m2.group(2).split())
    return entities


def parse_args_top(val: str) -> list[str]:
    m = re.match(r'[A-Z_0-9]*\((.*)\)\s*$', val, re.DOTALL)
    if not m:
        return []
    args, depth, cur = [], 0, []
    for ch in m.group(1):
        if ch == '(':
            depth += 1; cur.append(ch)
        elif ch == ')':
            depth -= 1; cur.append(ch)
        elif ch == ',' and depth == 0:
            args.append(''.join(cur).strip()); cur = []
        else:
            cur.append(ch)
    if cur:
        args.append(''.join(cur).strip())
    return args


def extract_refs(s: str) -> list[str]:
    return re.findall(r'#\d+', s)


# ---------------------------------------------------------------------------
# Product / hierarchy maps
# ---------------------------------------------------------------------------

def build_pd_to_name(entities: dict) -> dict:
    product_names: dict[str, str] = {}
    for eid, val in entities.items():
        if val.startswith('PRODUCT('):
            args = parse_args_top(val)
            if args:
                product_names[eid] = decode_step_string(args[0].strip("'"))

    pdf_to_product: dict[str, str] = {}
    for eid, val in entities.items():
        if val.startswith('PRODUCT_DEFINITION_FORMATION'):
            refs = extract_refs(val)
            if refs:
                pdf_to_product[eid] = refs[-1]

    pd_to_name: dict[str, str] = {}
    for eid, val in entities.items():
        if val.startswith('PRODUCT_DEFINITION('):
            args = parse_args_top(val)
            if len(args) >= 3:
                prod_ref = pdf_to_product.get(args[2])
                if prod_ref and prod_ref in product_names:
                    pd_to_name[eid] = product_names[prod_ref]
    return pd_to_name


def find_root_pd(entities: dict, pd_to_name: dict) -> str:
    for eid, name in pd_to_name.items():
        if name == 'root':
            return eid
    raise ValueError("Could not find a PRODUCT named 'root'.")


def find_root_sr(entities: dict) -> str:
    for eid, val in entities.items():
        if val.startswith('SHAPE_REPRESENTATION('):
            args = parse_args_top(val)
            if args and args[0].strip("'") == 'root':
                return eid
    raise ValueError("Could not find a SHAPE_REPRESENTATION named 'root'.")


def build_nauo_map(entities: dict) -> dict:
    result = {}
    for eid, val in entities.items():
        if val.startswith('NEXT_ASSEMBLY_USAGE_OCCURRENCE('):
            args = parse_args_top(val)
            if len(args) >= 5:
                result[eid] = (args[3], args[4])
    return result


def build_pds_nauo_map(entities: dict, nauo_ids: set) -> dict:
    result = {}
    for eid, val in entities.items():
        if val.startswith('PRODUCT_DEFINITION_SHAPE('):
            args = parse_args_top(val)
            if len(args) >= 3 and args[2] in nauo_ids:
                result[eid] = args[2]
    return result


def build_cdsr_map(entities: dict, pds_nauo_ids: set) -> dict:
    result = {}
    for eid, val in entities.items():
        if val.startswith('CONTEXT_DEPENDENT_SHAPE_REPRESENTATION('):
            args = parse_args_top(val)
            if len(args) >= 2 and args[1] in pds_nauo_ids:
                result[eid] = (args[0], args[1])
    return result


def build_rr_map(entities: dict) -> dict:
    result = {}
    for eid, val in entities.items():
        if 'REPRESENTATION_RELATIONSHIP(' not in val:
            continue
        m = re.search(r'REPRESENTATION_RELATIONSHIP\s*\(([^)]+)\)', val)
        if m:
            parts = [p.strip() for p in m.group(1).split(',')]
            if len(parts) >= 4:
                result[eid] = (parts[2], parts[3])
    return result


# ---------------------------------------------------------------------------
# Post-restructure: prune assemblies emptied by component moves
# ---------------------------------------------------------------------------

def _pd_display_name(entities: dict, pd_id: str) -> str:
    """Return the human-readable product name for a PRODUCT_DEFINITION id."""
    pd_val = entities.get(pd_id, '')
    if not pd_val:
        return pd_id
    pd_args = parse_args_top(pd_val)
    if len(pd_args) < 3:
        return pd_id
    pdf_val = entities.get(pd_args[2], '')
    for ref in extract_refs(pdf_val):
        prod_val = entities.get(ref, '')
        if prod_val.startswith('PRODUCT('):
            prod_args = parse_args_top(prod_val)
            if prod_args:
                return decode_step_string(prod_args[0].strip("'"))
    return pd_id


def _collect_pd_own_ids(entities: dict, pd_id: str) -> set[str]:
    """
    Collect entity IDs that make up a PD node's own definition:
    PRODUCT_DEFINITION, PRODUCT_DEFINITION_FORMATION, PRODUCT,
    own PRODUCT_DEFINITION_SHAPE (args[2]==pd_id), SHAPE_DEFINITION_REPRESENTATION,
    and the SHAPE_REPRESENTATION it points to.
    Placement entities (NAUO/PDS/CDSR/RR) are NOT included here.
    """
    ids: set[str] = {pd_id}
    pd_val = entities.get(pd_id, '')
    if not pd_val:
        return ids

    pd_args = parse_args_top(pd_val)
    if len(pd_args) >= 3:
        pdf_id = pd_args[2]
        pdf_val = entities.get(pdf_id, '')
        if pdf_val:
            ids.add(pdf_id)
            for ref in extract_refs(pdf_val):
                if entities.get(ref, '').startswith('PRODUCT('):
                    ids.add(ref)
                    break

    # Own PDS: PRODUCT_DEFINITION_SHAPE where args[2] == pd_id (not a NAUO ref)
    for eid, val in entities.items():
        if not val.startswith('PRODUCT_DEFINITION_SHAPE('):
            continue
        args = parse_args_top(val)
        if len(args) >= 3 and args[2] == pd_id:
            ids.add(eid)
            # SDR that references this PDS → also get the SR it points to
            for eid2, val2 in entities.items():
                if not val2.startswith('SHAPE_DEFINITION_REPRESENTATION('):
                    continue
                sdr_args = parse_args_top(val2)
                if sdr_args and sdr_args[0] == eid:
                    ids.add(eid2)
                    if len(sdr_args) >= 2:
                        ids.add(sdr_args[1])   # SHAPE_REPRESENTATION
                    break
            break

    return ids


def prune_emptied_assemblies(
    entities: dict, modified_entities: dict
) -> tuple[dict, set[str]]:
    """
    After restructuring, remove assembly nodes whose children were all moved out.

    Iterates bottom-up: each pass finds assembly nodes that now have zero NAUO
    children and deletes them (placement entities + own-definition entities).
    Repeats until no more can be removed (handles cascading empty parents).

    Parameters
    ----------
    entities          : original, unmodified entity dict (used to identify which
                        PDs were ever assembly parents)
    modified_entities : post-restructure entity dict; a copy is pruned and returned

    Returns
    -------
    (pruned_entities, deleted_ids)
        deleted_ids must be passed to write_step so it can strip the original
        text for those entities (they still exist in the raw STEP text but are
        no longer in modified_entities).
    """
    original_nauo    = build_nauo_map(entities)
    original_parents = {parent for _, (parent, _) in original_nauo.items()}

    result      = dict(modified_entities)
    deleted_ids: set[str] = set()

    while True:
        current_nauo     = build_nauo_map(result)
        current_parents  = {parent for _, (parent, _) in current_nauo.items()}
        current_children = {child  for _, (_, child) in current_nauo.items()}

        # Assembly nodes still in the tree but now childless
        emptied = (original_parents & current_children) - current_parents
        if not emptied:
            break

        pds_nauo_map = build_pds_nauo_map(result, set(current_nauo))
        cdsr_map     = build_cdsr_map(result, set(pds_nauo_map))
        nauo_to_pds  = {nauo: pds for pds, nauo in pds_nauo_map.items()}
        pds_to_cdsr  = {pds: (cdsr, rr) for cdsr, (rr, pds) in cdsr_map.items()}

        for pd_id in emptied:
            name = _pd_display_name(result, pd_id)
            print(f"  pruning emptied assembly: {name!r} ({pd_id})", file=sys.stderr)

            to_delete = _collect_pd_own_ids(result, pd_id)

            # Placement entities for every instance of this node
            for nauo_id, (_, child) in current_nauo.items():
                if child != pd_id:
                    continue
                to_delete.add(nauo_id)
                pds_eid = nauo_to_pds.get(nauo_id)
                if pds_eid:
                    to_delete.add(pds_eid)
                    cdsr_info = pds_to_cdsr.get(pds_eid)
                    if cdsr_info:
                        cdsr_id, rr_id = cdsr_info
                        to_delete.add(cdsr_id)
                        to_delete.add(rr_id)

            deleted_ids.update(to_delete)
            for eid in to_delete:
                result.pop(eid, None)

    return result, deleted_ids


# ---------------------------------------------------------------------------
# ID pool
# ---------------------------------------------------------------------------

class IDPool:
    def __init__(self, start: int):
        self._next = start

    def next(self) -> str:
        eid = f'#{self._next}'
        self._next += 1
        return eid


def max_entity_id(entities: dict) -> int:
    return max(int(eid[1:]) for eid in entities)


# ---------------------------------------------------------------------------
# Context entity finders
# ---------------------------------------------------------------------------

def _find_starting(entities: dict, prefix: str) -> str:
    for eid, val in entities.items():
        if val.startswith(prefix):
            return eid
    raise ValueError(f"Entity starting with {prefix!r} not found.")


def _find_root_geom_context(entities: dict) -> str:
    for eid, val in entities.items():
        if 'REPRESENTATION_CONTEXT(' in val and ("'root'" in val or ",'root')" in val):
            return eid
    for eid, val in entities.items():
        if 'GEOMETRIC_REPRESENTATION_CONTEXT(' in val:
            return eid
    raise ValueError("Cannot find geometric representation context for root.")


def _find_root_csys(entities: dict) -> str:
    for eid, val in entities.items():
        if val.startswith("AXIS2_PLACEMENT_3D('TS3D_PRODUCT_CSYS'"):
            return eid
    for eid, val in entities.items():
        if val.startswith('AXIS2_PLACEMENT_3D('):
            return eid
    raise ValueError("Cannot find root coordinate system (AXIS2_PLACEMENT_3D).")


# ---------------------------------------------------------------------------
# Entity mutation helpers
# ---------------------------------------------------------------------------

def _split_args(inner: str) -> list[str]:
    args, depth, cur = [], 0, []
    for ch in inner:
        if ch == '(':
            depth += 1; cur.append(ch)
        elif ch == ')':
            depth -= 1; cur.append(ch)
        elif ch == ',' and depth == 0:
            args.append(''.join(cur)); cur = []
        else:
            cur.append(ch)
    if cur:
        args.append(''.join(cur))
    return args


def _replace_nauo_parent(val: str, old_parent: str, new_parent: str) -> str:
    prefix = 'NEXT_ASSEMBLY_USAGE_OCCURRENCE('
    inner = val[len(prefix):-1]
    args = _split_args(inner)
    if len(args) < 5 or args[3] != old_parent:
        return val
    args[3] = new_parent
    return prefix + ','.join(args) + ')'


def _replace_rr_parent_sr(val: str, old_parent_sr: str, new_parent_sr: str) -> str:
    pattern = re.compile(
        r'(REPRESENTATION_RELATIONSHIP\s*\(\s*\'[^\']*\'\s*,\s*\'[^\']*\'\s*,\s*'
        r'(#\d+)\s*,\s*)'
        r'(#\d+)'
        r'(\s*\))'
    )
    def replacer(m):
        if m.group(3) == old_parent_sr:
            return m.group(1) + new_parent_sr + m.group(4)
        return m.group(0)
    return pattern.sub(replacer, val)


# ---------------------------------------------------------------------------
# Core: create one assembly node + link it to a parent
# ---------------------------------------------------------------------------

def _create_assembly_node(
    name: str,
    parent_pd: str,
    parent_sr: str,
    pool: IDPool,
    new_entity_map: dict,
    prod_context: str,
    pd_context: str,
    geom_context: str,
    root_csys: str,
) -> tuple[str, str, list[str]]:
    """
    Adds new entities for an assembly node linked under parent_pd/parent_sr.
    Returns (new_pd_id, new_sr_id, all_created_ids).
    """
    enc = encode_step_string(name)

    prod_id   = pool.next()
    pdf_id    = pool.next()
    pd_id     = pool.next()
    pds_pd_id = pool.next()
    sr_id     = pool.next()
    sdr_id    = pool.next()

    new_entity_map[prod_id]   = f"PRODUCT({enc},{enc},'',({prod_context}))"
    new_entity_map[pdf_id]    = (f"PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE"
                                 f"('','',{prod_id},.NOT_KNOWN.)")
    new_entity_map[pd_id]     = f"PRODUCT_DEFINITION('design','',{pdf_id},{pd_context})"
    new_entity_map[pds_pd_id] = f"PRODUCT_DEFINITION_SHAPE('','',{pd_id})"
    new_entity_map[sr_id]     = f"SHAPE_REPRESENTATION({enc},({root_csys}),{geom_context})"
    new_entity_map[sdr_id]    = f"SHAPE_DEFINITION_REPRESENTATION({pds_pd_id},{sr_id})"

    # Identity transform + NAUO placement under parent
    cp_id   = pool.next()
    d1_id   = pool.next()
    d2_id   = pool.next()
    ax_id   = pool.next()
    idt_id  = pool.next()
    rr_id   = pool.next()
    nauo_id = pool.next()
    pds_id  = pool.next()
    cdsr_id = pool.next()

    new_entity_map[cp_id]   = "CARTESIAN_POINT('',(0.,0.,0.))"
    new_entity_map[d1_id]   = "DIRECTION('',(0.,0.,1.))"
    new_entity_map[d2_id]   = "DIRECTION('',(1.,0.,0.))"
    new_entity_map[ax_id]   = f"AXIS2_PLACEMENT_3D('',{cp_id},{d1_id},{d2_id})"
    new_entity_map[idt_id]  = f"ITEM_DEFINED_TRANSFORMATION('','',{ax_id},{root_csys})"
    new_entity_map[rr_id]   = (f"(REPRESENTATION_RELATIONSHIP('','',{sr_id},{parent_sr})"
                               f"REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION({idt_id})"
                               f"SHAPE_REPRESENTATION_RELATIONSHIP())")
    new_entity_map[nauo_id] = (f"NEXT_ASSEMBLY_USAGE_OCCURRENCE("
                               f"{enc},{enc},{enc},{parent_pd},{pd_id},$)")
    new_entity_map[pds_id]  = f"PRODUCT_DEFINITION_SHAPE('','',{nauo_id})"
    new_entity_map[cdsr_id] = f"CONTEXT_DEPENDENT_SHAPE_REPRESENTATION({rr_id},{pds_id})"

    all_ids = [prod_id, pdf_id, pd_id, pds_pd_id, sr_id, sdr_id,
               cp_id, d1_id, d2_id, ax_id, idt_id, rr_id, nauo_id, pds_id, cdsr_id]
    return pd_id, sr_id, all_ids


# ---------------------------------------------------------------------------
# Core: move all instances of a component under a new parent
# ---------------------------------------------------------------------------

def _move_components(
    target_name: str,
    new_pd: str,
    new_sr: str,
    name_to_pds: dict,
    nauo_map: dict,
    rr_map: dict,
    pds_to_nauo_rev: dict,
    pds_to_cdsr: dict,
    modified_entities: dict,
) -> int:
    target_pd_set = set(name_to_pds.get(target_name, []))
    if not target_pd_set:
        print(f"    WARNING: no component named {target_name!r} found; skipping.",
              file=sys.stderr)
        return 0

    matching = [
        nauo_id for nauo_id, (parent, child) in nauo_map.items()
        if child in target_pd_set
    ]
    if not matching:
        print(f"    WARNING: no instances of {target_name!r} found in tree; skipping.",
              file=sys.stderr)
        return 0

    for nauo_eid in matching:
        current_parent = nauo_map[nauo_eid][0]
        modified_entities[nauo_eid] = _replace_nauo_parent(
            modified_entities[nauo_eid], current_parent, new_pd)

    for nauo_eid in matching:
        pds_eid = pds_to_nauo_rev.get(nauo_eid)
        if not pds_eid:
            continue
        cdsr_info = pds_to_cdsr.get(pds_eid)
        if not cdsr_info:
            continue
        _, rr_eid = cdsr_info
        if rr_eid not in modified_entities:
            continue
        current_parent_sr = rr_map.get(rr_eid, (None, None))[1]
        if not current_parent_sr:
            continue
        modified_entities[rr_eid] = _replace_rr_parent_sr(
            modified_entities[rr_eid], current_parent_sr, new_sr)

    return len(matching)


# ---------------------------------------------------------------------------
# Main restructure entry point
# ---------------------------------------------------------------------------

def restructure(
    entities: dict,
    tree: 'OrderedDict[str, OrderedDict[str, list[str]]]',
) -> tuple[dict, list[str]]:
    """
    tree  –  {L1_name: {L2_name: [component_name, …]}}

    Creates root → L1 → L2, then moves every named component under its L2.
    """
    pd_to_name = build_pd_to_name(entities)
    root_pd    = find_root_pd(entities, pd_to_name)
    root_sr    = find_root_sr(entities)

    name_to_pds: dict[str, list[str]] = {}
    for pd_id, name in pd_to_name.items():
        name_to_pds.setdefault(name, []).append(pd_id)

    nauo_map     = build_nauo_map(entities)
    pds_nauo_map = build_pds_nauo_map(entities, set(nauo_map))
    cdsr_map     = build_cdsr_map(entities, set(pds_nauo_map))
    rr_map       = build_rr_map(entities)

    pds_to_nauo_rev = {v: k for k, v in pds_nauo_map.items()}
    pds_to_cdsr: dict[str, tuple] = {}
    for cdsr_id, (rr_id, pds_id) in cdsr_map.items():
        pds_to_cdsr[pds_id] = (cdsr_id, rr_id)

    prod_context  = _find_starting(entities, 'PRODUCT_CONTEXT(')
    pd_context    = _find_starting(entities, 'PRODUCT_DEFINITION_CONTEXT(')
    geom_context  = _find_root_geom_context(entities)
    root_csys     = _find_root_csys(entities)

    pool = IDPool(max_entity_id(entities) + 1)
    modified_entities = dict(entities)
    new_entity_map: dict[str, str] = {}

    ctx = dict(
        pool=pool, new_entity_map=new_entity_map,
        prod_context=prod_context, pd_context=pd_context,
        geom_context=geom_context, root_csys=root_csys,
    )

    # Collect per-node info for later pruning: list of (l1_ids, {l2_pd: (l2_ids, move_count)})
    node_info: list[tuple[list[str], dict]] = []

    for l1_name, l2_groups in tree.items():
        print(f"  L1: {l1_name!r}", file=sys.stderr)
        l1_pd, l1_sr, l1_ids = _create_assembly_node(l1_name, root_pd, root_sr, **ctx)
        l2_map: dict[str, tuple[list[str], int]] = {}

        for l2_name, components in l2_groups.items():
            print(f"    L2: {l2_name!r}", file=sys.stderr)
            l2_pd, l2_sr, l2_ids = _create_assembly_node(l2_name, l1_pd, l1_sr, **ctx)
            total_moved = 0

            for comp_name in components:
                count = _move_components(
                    comp_name, l2_pd, l2_sr,
                    name_to_pds, nauo_map, rr_map,
                    pds_to_nauo_rev, pds_to_cdsr, modified_entities,
                )
                total_moved += count
                print(f"      moved {count} × {comp_name!r}", file=sys.stderr)

            l2_map[l2_pd] = (l2_ids, total_moved)

        node_info.append((l1_ids, l2_map))

    # Prune empty L2 nodes, then empty L1 nodes
    for l1_ids, l2_map in node_info:
        l2_kept = 0
        for l2_pd, (l2_ids, move_count) in l2_map.items():
            if move_count == 0:
                print(f"  pruning empty L2 {l2_pd}", file=sys.stderr)
                for eid in l2_ids:
                    new_entity_map.pop(eid, None)
            else:
                l2_kept += 1
        if l2_kept == 0:
            print(f"  pruning empty L1 (no populated L2 children)", file=sys.stderr)
            for eid in l1_ids:
                new_entity_map.pop(eid, None)

    new_lines = [f"{eid}={val};" for eid, val in new_entity_map.items()]
    return modified_entities, new_lines


# ---------------------------------------------------------------------------
# Write STEP output
# ---------------------------------------------------------------------------

def write_step(original_text: str, modified_entities: dict,
               new_entity_lines: list[str], output_path: Path,
               deleted_ids: set | None = None):
    m = re.search(r'\bDATA\s*;', original_text, re.IGNORECASE)
    data_start = m.end()
    m2 = re.search(r'\bENDSEC\b', original_text[data_start:], re.IGNORECASE)
    endsec_pos = data_start + m2.start()

    header    = original_text[:data_start]
    footer    = original_text[endsec_pos:]
    data_body = original_text[data_start:endsec_pos]
    _deleted  = deleted_ids or set()

    def entity_replacer(match):
        eid = match.group(1)
        if eid in _deleted:
            return ''                              # strip deleted entities
        if eid in modified_entities:
            return f'{eid}={modified_entities[eid]};'
        return match.group(0)

    new_data_body = re.sub(
        r'(#\d+)\s*=\s*(?:[^;]|\n)*?;',
        entity_replacer,
        data_body,
        flags=re.DOTALL,
    )

    result_text = header + new_data_body + '\n' + '\n'.join(new_entity_lines) + '\n' + footer

    if output_path is not None:
        output_path.write_text(result_text, encoding='utf-8')

    return result_text


# ---------------------------------------------------------------------------
# Markdown config parser
# ---------------------------------------------------------------------------

def parse_markdown_config(md_path: Path) -> 'OrderedDict[str, OrderedDict[str, list[str]]]':
    """
    Parse the markdown file into a 3-level OrderedDict:
        {L1_name: {L2_name: [component_name, …]}}

    Heading rules:
        plain text  → L1 node (child of root)
        # or ##     → L2 node (child of current L1)
        ####        → component to move under current L2
        blank line  → separator
    """
    lines = md_path.read_text(encoding='utf-8').splitlines()
    tree: OrderedDict = OrderedDict()
    current_l1: str | None = None
    current_l2: str | None = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        hashes = len(line) - len(line.lstrip('#'))
        text   = line.lstrip('#').strip()

        if hashes == 0:
            # L1 group
            current_l1 = text
            current_l2 = None
            if current_l1 not in tree:
                tree[current_l1] = OrderedDict()

        elif hashes in (1, 2):
            # L2 group
            if current_l1 is None:
                print(f"  WARNING: L2 heading {line!r} has no L1 parent; skipping.",
                      file=sys.stderr)
                continue
            current_l2 = text
            if current_l2 not in tree[current_l1]:
                tree[current_l1][current_l2] = []

        elif hashes == 4:
            # Component reference
            if current_l1 is None or current_l2 is None:
                print(f"  WARNING: component {text!r} has no L1/L2 parent; skipping.",
                      file=sys.stderr)
                continue
            tree[current_l1][current_l2].append(text)

        else:
            # h3 / h5+ – treat as a comment / ignored
            pass

    return tree


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Restructure STEP assembly into a 3-level hierarchy via markdown config.')
    parser.add_argument('step_file', help='Input .step file')

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        '--config', metavar='MD_FILE',
        help='Markdown file defining the 3-level hierarchy',
    )
    source.add_argument(
        '--map', nargs=3,
        metavar=('L1_NAME', 'L2_NAME', 'COMPONENT_NAME'),
        action='append', dest='mappings',
        help='Inline mapping (repeatable): L1 L2 component',
    )

    parser.add_argument('--output', metavar='FILE',
                        help='Output file (default: <input>_restructured.step)')
    args = parser.parse_args()

    in_path = Path(args.step_file)
    if not in_path.exists():
        print(f"Error: {in_path} not found.", file=sys.stderr)
        sys.exit(1)

    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"Error: config file {config_path} not found.", file=sys.stderr)
            sys.exit(1)
        print(f"Loading config from {config_path.name} …", file=sys.stderr)
        tree = parse_markdown_config(config_path)
        for l1, l2s in tree.items():
            print(f"  {l1}", file=sys.stderr)
            for l2, comps in l2s.items():
                for c in comps:
                    print(f"    {l2}  ←  {c!r}", file=sys.stderr)
    else:
        tree = OrderedDict()
        for l1_name, l2_name, comp_name in args.mappings:
            tree.setdefault(l1_name, OrderedDict()).setdefault(l2_name, []).append(comp_name)

    out_path = Path(args.output) if args.output \
        else in_path.with_name(in_path.stem + '_restructured' + in_path.suffix)

    print(f"Reading {in_path.name} …", file=sys.stderr)
    original_text = in_path.read_text(encoding='utf-8', errors='replace')

    print("Parsing entities …", file=sys.stderr)
    entities = parse_step_entities(original_text)
    print(f"  {len(entities)} entities loaded", file=sys.stderr)

    print("Applying restructuring …", file=sys.stderr)
    modified_entities, new_lines = restructure(entities, tree)
    print(f"  {len(new_lines)} new entity lines generated", file=sys.stderr)

    print("Pruning assemblies emptied by component moves …", file=sys.stderr)
    before = len(modified_entities)
    modified_entities, deleted_ids = prune_emptied_assemblies(entities, modified_entities)
    print(f"  removed {before - len(modified_entities)} entities "
          f"({len(deleted_ids)} entity IDs deleted)", file=sys.stderr)

    print(f"Writing {out_path} …", file=sys.stderr)
    write_step(original_text, modified_entities, new_lines, out_path,
               deleted_ids=deleted_ids)
    print("Done.", file=sys.stderr)


if __name__ == '__main__':
    main()
