"""
Match a component STEP file against repeated instances in an assembly STEP file.

The matcher tries exact component/product names first. If names are not unique or
not present, it falls back to a simple geometry fingerprint based on bounding-box
dimensions extracted from each product's SHAPE_REPRESENTATION and nearby
CARTESIAN_POINT records.

For each matched assembly occurrence it records:
  - product / occurrence ids and names
  - assembly parent
  - explicit STEP transform position, when available
  - geometry center position, useful when exporters bake coordinates into shapes
  - position relative to the parent geometry center and to the assembly root

Usage:
    python match_step_component_positions.py component.step assembly.step
    python match_step_component_positions.py component.step assembly.step --csv matches.csv
    python match_step_component_positions.py component.step assembly.step --name "part name"
    python match_step_component_positions.py component.step assembly.step --target leaves
    python match_step_component_positions.py component.step assembly.step --debug
    python match_step_component_positions.py component.step assembly.step --no-promote-single-child-root
    python match_step_component_positions.py component.step assembly.step --launch-ui
    python match_step_component_positions.py --ui-only assembly.step output/component_matches.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent

Vector3 = tuple[float, float, float]
BBox = tuple[Vector3, Vector3]


@dataclass
class Occurrence:
    nauo_id: str
    parent_pd: str
    child_pd: str
    occurrence_name: str
    rr_id: str | None = None
    transform_id: str | None = None
    transform_position: Vector3 | None = None


@dataclass
class StepModel:
    path: Path
    entities: dict[str, str]
    pd_to_name: dict[str, str]
    pd_to_sr: dict[str, str]
    occurrences: list[Occurrence]
    children_of: dict[str, list[Occurrence]]
    parents_of: dict[str, list[Occurrence]]
    sr_bbox_cache: dict[str, BBox | None]
    pd_bbox_cache: dict[str, BBox | None]
    pd_range_bbox_cache: dict[str, BBox | None]

    def product_defs_by_name(self, name: str) -> list[str]:
        return [pd for pd, pd_name in self.pd_to_name.items() if pd_name == name]

    def root_product_defs(self) -> list[str]:
        children = {occ.child_pd for occ in self.occurrences}
        roots = [pd for pd in self.pd_to_name if pd not in children]
        return roots or list(self.pd_to_name)


def decode_step_string(s: str) -> str:
    def replace(match: re.Match[str]) -> str:
        hex_chars = match.group(1)
        try:
            return ''.join(
                chr(int(hex_chars[i:i + 4], 16))
                for i in range(0, len(hex_chars), 4)
            )
        except ValueError:
            return match.group(0)

    return re.sub(r'\\X2\\([0-9A-Fa-f]+)\\X0\\', replace, s)


def strip_step_string(value: str) -> str:
    value = value.strip()
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1]
    return decode_step_string(value)


def parse_step_entities(text: str) -> dict[str, str]:
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    data = re.search(r'\bDATA\s*;(.*?)\bENDSEC\b', text, re.DOTALL | re.IGNORECASE)
    if not data:
        raise ValueError("No DATA section found")

    entities: dict[str, str] = {}
    for stmt in data.group(1).split(';'):
        stmt = stmt.strip()
        if not stmt:
            continue
        match = re.match(r'(#\d+)\s*=\s*(.*)', stmt, re.DOTALL)
        if match:
            entities[match.group(1)] = ' '.join(match.group(2).split())
    return entities


def parse_args_top(value: str) -> list[str]:
    match = re.match(r'[A-Z_0-9]*\((.*)\)\s*$', value, re.DOTALL)
    if not match:
        return []
    return split_top_level(match.group(1))


def split_top_level(value: str) -> list[str]:
    args: list[str] = []
    depth = 0
    in_string = False
    cur: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "'":
            in_string = not in_string
            cur.append(ch)
        elif not in_string and ch == '(':
            depth += 1
            cur.append(ch)
        elif not in_string and ch == ')':
            depth -= 1
            cur.append(ch)
        elif not in_string and ch == ',' and depth == 0:
            args.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        args.append(''.join(cur).strip())
    return args


def extract_refs(value: str) -> list[str]:
    return re.findall(r'#\d+', value)


def parse_vector(value: str) -> Vector3 | None:
    match = re.search(r'\(([-+0-9.Ee,\s]+)\)', value)
    if not match:
        return None
    parts = [p.strip() for p in match.group(1).split(',')]
    if len(parts) != 3:
        return None
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return None


def vector_sub(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vector_add(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vector_round(v: Vector3 | None, digits: int = 6) -> list[float] | None:
    if v is None:
        return None
    return [round(v[0], digits), round(v[1], digits), round(v[2], digits)]


def bbox_center(bbox: BBox | None) -> Vector3 | None:
    if bbox is None:
        return None
    lo, hi = bbox
    return (
        (lo[0] + hi[0]) / 2.0,
        (lo[1] + hi[1]) / 2.0,
        (lo[2] + hi[2]) / 2.0,
    )


def bbox_size(bbox: BBox | None) -> Vector3 | None:
    if bbox is None:
        return None
    lo, hi = bbox
    return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])


def bbox_union(boxes: Iterable[BBox | None]) -> BBox | None:
    valid = [box for box in boxes if box is not None]
    if not valid:
        return None
    mins = tuple(min(box[0][axis] for box in valid) for axis in range(3))
    maxs = tuple(max(box[1][axis] for box in valid) for axis in range(3))
    return mins, maxs  # type: ignore[return-value]


def bbox_from_points(points: Iterable[Vector3]) -> BBox | None:
    pts = list(points)
    if not pts:
        return None
    mins = tuple(min(point[axis] for point in pts) for axis in range(3))
    maxs = tuple(max(point[axis] for point in pts) for axis in range(3))
    return mins, maxs  # type: ignore[return-value]


def geometry_key(bbox: BBox | None, precision: float) -> tuple[int, int, int] | None:
    size = bbox_size(bbox)
    if size is None:
        return None
    dims = sorted(abs(v) for v in size)
    return tuple(int(round(dim / precision)) for dim in dims)  # type: ignore[return-value]


def debug_print(enabled: bool, message: str = '') -> None:
    if enabled:
        print(message, file=sys.stderr)


def format_vec(value: Vector3 | None) -> str:
    if value is None:
        return 'None'
    return f'({value[0]:.6g}, {value[1]:.6g}, {value[2]:.6g})'


def format_bbox(value: BBox | None) -> str:
    if value is None:
        return 'None'
    return f'min={format_vec(value[0])} max={format_vec(value[1])} size={format_vec(bbox_size(value))}'


def build_pd_to_name(entities: dict[str, str]) -> dict[str, str]:
    product_names: dict[str, str] = {}
    for eid, value in entities.items():
        if value.startswith('PRODUCT('):
            args = parse_args_top(value)
            if args:
                product_names[eid] = strip_step_string(args[0])

    pdf_to_product: dict[str, str] = {}
    for eid, value in entities.items():
        if value.startswith('PRODUCT_DEFINITION_FORMATION'):
            refs = extract_refs(value)
            if refs:
                pdf_to_product[eid] = refs[-1]

    pd_to_name: dict[str, str] = {}
    for eid, value in entities.items():
        if value.startswith('PRODUCT_DEFINITION('):
            args = parse_args_top(value)
            if len(args) >= 3:
                product_id = pdf_to_product.get(args[2])
                if product_id:
                    pd_to_name[eid] = product_names.get(product_id, product_id)
    return pd_to_name


def build_pd_to_sr(entities: dict[str, str]) -> dict[str, str]:
    own_pds_to_pd: dict[str, str] = {}
    for eid, value in entities.items():
        if not value.startswith('PRODUCT_DEFINITION_SHAPE('):
            continue
        args = parse_args_top(value)
        if len(args) >= 3 and args[2].startswith('#'):
            target = args[2]
            target_value = entities.get(target, '')
            if target_value.startswith('PRODUCT_DEFINITION('):
                own_pds_to_pd[eid] = target

    result: dict[str, str] = {}
    for eid, value in entities.items():
        if not value.startswith('SHAPE_DEFINITION_REPRESENTATION('):
            continue
        args = parse_args_top(value)
        if len(args) >= 2 and args[0] in own_pds_to_pd:
            result[own_pds_to_pd[args[0]]] = args[1]
    return result


def parse_axis_origin(entities: dict[str, str], axis_id: str | None) -> Vector3 | None:
    if axis_id is None:
        return None
    value = entities.get(axis_id, '')
    if not value.startswith('AXIS2_PLACEMENT_3D('):
        return None
    args = parse_args_top(value)
    if len(args) < 2:
        return None
    point_value = entities.get(args[1], '')
    if not point_value.startswith('CARTESIAN_POINT('):
        return None
    return parse_vector(point_value)


def parse_transform_position(entities: dict[str, str], transform_id: str | None) -> Vector3 | None:
    if transform_id is None:
        return None
    value = entities.get(transform_id, '')
    if not value.startswith('ITEM_DEFINED_TRANSFORMATION('):
        return None
    args = parse_args_top(value)
    if len(args) < 4:
        return None
    from_origin = parse_axis_origin(entities, args[2])
    to_origin = parse_axis_origin(entities, args[3])
    if from_origin is None:
        return None
    if to_origin is None:
        return from_origin
    return vector_sub(from_origin, to_origin)


def build_occurrences(entities: dict[str, str]) -> list[Occurrence]:
    nauo: dict[str, Occurrence] = {}
    for eid, value in entities.items():
        if not value.startswith('NEXT_ASSEMBLY_USAGE_OCCURRENCE('):
            continue
        args = parse_args_top(value)
        if len(args) >= 5:
            nauo[eid] = Occurrence(
                nauo_id=eid,
                parent_pd=args[3],
                child_pd=args[4],
                occurrence_name=strip_step_string(args[1]) if args[1] != '$' else '',
            )

    pds_to_nauo: dict[str, str] = {}
    for eid, value in entities.items():
        if not value.startswith('PRODUCT_DEFINITION_SHAPE('):
            continue
        args = parse_args_top(value)
        if len(args) >= 3 and args[2] in nauo:
            pds_to_nauo[eid] = args[2]

    rr_transform: dict[str, str | None] = {}
    for eid, value in entities.items():
        if 'REPRESENTATION_RELATIONSHIP(' not in value:
            continue
        transform_match = re.search(
            r'REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION\s*\((#\d+)\)',
            value,
        )
        rr_transform[eid] = transform_match.group(1) if transform_match else None

    for eid, value in entities.items():
        if not value.startswith('CONTEXT_DEPENDENT_SHAPE_REPRESENTATION('):
            continue
        args = parse_args_top(value)
        if len(args) < 2:
            continue
        rr_id, pds_id = args[0], args[1]
        nauo_id = pds_to_nauo.get(pds_id)
        if not nauo_id or nauo_id not in nauo:
            continue
        occ = nauo[nauo_id]
        occ.rr_id = rr_id
        occ.transform_id = rr_transform.get(rr_id)
        occ.transform_position = parse_transform_position(entities, occ.transform_id)

    return list(nauo.values())


def load_step_model(path: Path) -> StepModel:
    text = path.read_text(encoding='utf-8', errors='replace')
    entities = parse_step_entities(text)
    occurrences = build_occurrences(entities)
    children_of: dict[str, list[Occurrence]] = defaultdict(list)
    parents_of: dict[str, list[Occurrence]] = defaultdict(list)
    for occ in occurrences:
        children_of[occ.parent_pd].append(occ)
        parents_of[occ.child_pd].append(occ)
    return StepModel(
        path=path,
        entities=entities,
        pd_to_name=build_pd_to_name(entities),
        pd_to_sr=build_pd_to_sr(entities),
        occurrences=occurrences,
        children_of=dict(children_of),
        parents_of=dict(parents_of),
        sr_bbox_cache={},
        pd_bbox_cache={},
        pd_range_bbox_cache={},
    )


def debug_model_summary(label: str, model: StepModel, limit: int) -> None:
    roots = model.root_product_defs()
    transforms = [occ for occ in model.occurrences if occ.transform_id]
    nonzero_transforms = [
        occ for occ in model.occurrences
        if occ.transform_position is not None and any(abs(v) > 1e-9 for v in occ.transform_position)
    ]
    names: dict[str, int] = defaultdict(int)
    for name in model.pd_to_name.values():
        names[name] += 1

    print(f'\n[debug] {label}: {model.path}', file=sys.stderr)
    print(f'  entities: {len(model.entities)}', file=sys.stderr)
    print(f'  product definitions: {len(model.pd_to_name)}', file=sys.stderr)
    print(f'  shape representations linked to products: {len(model.pd_to_sr)}', file=sys.stderr)
    print(f'  assembly occurrences: {len(model.occurrences)}', file=sys.stderr)
    print(f'  occurrences with transform ids: {len(transforms)}', file=sys.stderr)
    print(f'  occurrences with nonzero transform positions: {len(nonzero_transforms)}', file=sys.stderr)
    print(f'  root product defs: {", ".join(f"{pd}:{model.pd_to_name.get(pd, pd)}" for pd in roots)}', file=sys.stderr)

    repeated = [(name, count) for name, count in names.items() if count > 1]
    repeated.sort(key=lambda item: (-item[1], item[0]))
    if repeated:
        print(f'  repeated product names, first {limit}:', file=sys.stderr)
        for name, count in repeated[:limit]:
            print(f'    {count} x {name}', file=sys.stderr)

    print(f'  product samples, first {limit}:', file=sys.stderr)
    for pd, name in list(model.pd_to_name.items())[:limit]:
        sr = model.pd_to_sr.get(pd)
        children = len(model.children_of.get(pd, []))
        parents = len(model.parents_of.get(pd, []))
        print(f'    {pd}: {name!r}, sr={sr}, parents={parents}, children={children}', file=sys.stderr)


def shape_representation_bbox(model: StepModel, sr_id: str) -> BBox | None:
    if sr_id in model.sr_bbox_cache:
        return model.sr_bbox_cache[sr_id]

    points: list[Vector3] = []
    seen: set[str] = set()
    queue: deque[str] = deque([sr_id])

    while queue:
        eid = queue.popleft()
        if eid in seen:
            continue
        seen.add(eid)
        value = model.entities.get(eid, '')
        if value.startswith('CARTESIAN_POINT('):
            point = parse_vector(value)
            if point is not None:
                points.append(point)
        for ref in extract_refs(value):
            if ref not in seen and ref in model.entities:
                queue.append(ref)

    bbox = bbox_from_points(points)
    model.sr_bbox_cache[sr_id] = bbox
    return bbox


def product_bbox(model: StepModel, pd_id: str) -> BBox | None:
    if pd_id in model.pd_bbox_cache:
        return model.pd_bbox_cache[pd_id]

    boxes: list[BBox | None] = []
    own_sr = model.pd_to_sr.get(pd_id)
    if own_sr:
        boxes.append(shape_representation_bbox(model, own_sr))
    boxes.append(product_definition_record_bbox(model, pd_id))
    for occ in model.children_of.get(pd_id, []):
        boxes.append(product_bbox(model, occ.child_pd))

    non_degenerate = [
        box for box in boxes
        if box is not None and any(abs(v) > 1e-9 for v in (bbox_size(box) or (0.0, 0.0, 0.0)))
    ]
    bbox = bbox_union(non_degenerate or boxes)
    model.pd_bbox_cache[pd_id] = bbox
    return bbox


def product_definition_record_bbox(model: StepModel, pd_id: str) -> BBox | None:
    """
    Fallback geometry bbox for STEP exports where SHAPE_REPRESENTATION lists
    only placement items and the detailed solids are emitted nearby in record
    order. This intentionally scans from this PRODUCT_DEFINITION to the next
    PRODUCT_DEFINITION; assembly nodes still get child bboxes through recursion.
    """
    if pd_id in model.pd_range_bbox_cache:
        return model.pd_range_bbox_cache[pd_id]

    try:
        start = int(pd_id[1:])
    except ValueError:
        return None

    pd_numbers = sorted(
        int(eid[1:])
        for eid, value in model.entities.items()
        if value.startswith('PRODUCT_DEFINITION(')
    )
    end = next((num for num in pd_numbers if num > start), None)
    points: list[Vector3] = []
    for eid, value in model.entities.items():
        try:
            num = int(eid[1:])
        except ValueError:
            continue
        if num <= start or (end is not None and num >= end):
            continue
        if value.startswith('CARTESIAN_POINT('):
            point = parse_vector(value)
            if point is not None:
                points.append(point)

    non_origin = [p for p in points if any(abs(axis) > 1e-9 for axis in p)]
    if non_origin:
        points = non_origin
    bbox = bbox_from_points(points)
    model.pd_range_bbox_cache[pd_id] = bbox
    return bbox


def leaf_product_defs(model: StepModel) -> list[str]:
    leaves = [pd for pd in model.pd_to_name if pd not in model.children_of]
    return leaves or list(model.pd_to_name)


def promoted_single_child_roots(model: StepModel, roots: list[str]) -> list[str]:
    promoted: list[str] = []
    for root in roots:
        current = root
        seen = {current}
        while True:
            children = model.children_of.get(current, [])
            if len(children) != 1:
                break
            child = children[0].child_pd
            if child in seen:
                break
            current = child
            seen.add(current)
        promoted.append(current)
    return promoted


def choose_component_targets(
    model: StepModel,
    explicit_name: str | None,
    target_mode: str,
    promote_single_child_root: bool,
) -> list[str]:
    if explicit_name:
        matches = model.product_defs_by_name(explicit_name)
        if not matches:
            raise ValueError(f"No product named {explicit_name!r} in {model.path}")
        return matches

    roots = model.root_product_defs()
    if target_mode == 'root':
        if promote_single_child_root:
            return promoted_single_child_roots(model, roots)
        return roots

    root_children = [
        occ.child_pd
        for root in roots
        for occ in model.children_of.get(root, [])
        if model.pd_to_name.get(occ.child_pd, '').lower() != 'root'
    ]
    if target_mode == 'children':
        return root_children or roots

    non_root_named = [
        pd for pd in model.pd_to_name
        if pd not in roots and model.pd_to_name[pd].lower() != 'root'
    ]
    leaves = [pd for pd in leaf_product_defs(model) if model.pd_to_name[pd].lower() != 'root']
    if target_mode == 'leaves':
        return leaves or non_root_named or roots
    if target_mode == 'all':
        return non_root_named or leaves or roots
    raise ValueError(f"Unknown target mode: {target_mode}")


def find_matching_product_defs(
    component: StepModel,
    assembly: StepModel,
    component_targets: list[str],
    precision: float,
    debug: bool = False,
    debug_limit: int = 20,
) -> tuple[set[str], list[str], dict[str, list[str]]]:
    reasons: list[str] = []
    matched: set[str] = set()
    name_matches: dict[str, list[str]] = {}

    target_names = sorted({component.pd_to_name[pd] for pd in component_targets})
    debug_print(debug, '\n[debug] name matching')
    debug_print(debug, f'  target names: {target_names[:debug_limit]}')
    for name in target_names:
        if name.lower() == 'root':
            debug_print(debug, f'  skip root name: {name!r}')
            continue
        pds = assembly.product_defs_by_name(name)
        if pds:
            matched.update(pds)
            name_matches[name] = pds
            debug_print(debug, f'  name matched {name!r}: {pds[:debug_limit]}')
        else:
            debug_print(debug, f'  no name match for {name!r}')

    if matched:
        reasons.append('name')

    target_key_by_pd = {
        pd: geometry_key(product_bbox(component, pd), precision)
        for pd in component_targets
    }
    target_keys = set(target_key_by_pd.values())
    target_keys.discard(None)

    debug_print(debug, '\n[debug] geometry matching')
    for pd, key in list(target_key_by_pd.items())[:debug_limit]:
        bbox = product_bbox(component, pd)
        debug_print(
            debug,
            f'  target {pd} {component.pd_to_name.get(pd, pd)!r}: key={key}, bbox={format_bbox(bbox)}',
        )
    if len(target_key_by_pd) > debug_limit:
        debug_print(debug, f'  ... {len(target_key_by_pd) - debug_limit} more target geometry keys')

    geometry_matched = 0
    checked = 0
    if target_keys:
        for pd in assembly.pd_to_name:
            if pd in matched or assembly.pd_to_name[pd].lower() == 'root':
                continue
            key = geometry_key(product_bbox(assembly, pd), precision)
            checked += 1
            if key in target_keys:
                matched.add(pd)
                geometry_matched += 1
                debug_print(
                    debug and geometry_matched <= debug_limit,
                    f'  geometry matched {pd} {assembly.pd_to_name[pd]!r}: key={key}, bbox={format_bbox(product_bbox(assembly, pd))}',
                )
        if geometry_matched:
            reasons.append('geometry')
    debug_print(debug, f'  geometry target key count: {len(target_keys)}')
    debug_print(debug, f'  assembly products checked by geometry: {checked}')
    debug_print(debug, f'  geometry product definition matches: {geometry_matched}')

    return matched, reasons, name_matches


# ==== Pose estimation via shared bottom-circle anchor ====
# Strategy: pick the lowest-Z CIRCLE with horizontal normal and the largest
# radius as the anchor (bucket bottom rim). That fixes origin + Z axis. To
# resolve yaw we project the centroid of all geometry points onto the anchor's
# XY plane and use the direction from origin to the projected centroid as X.


def vec_normalize(v: Vector3) -> Vector3:
    m = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
    if m < 1e-12:
        return (1.0, 0.0, 0.0)
    return (v[0] / m, v[1] / m, v[2] / m)


def vec_dot(a: Vector3, b: Vector3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vec_cross(a: Vector3, b: Vector3) -> Vector3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def vec_orthogonalize(x: Vector3, z: Vector3) -> Vector3:
    d = vec_dot(x, z)
    return vec_normalize((x[0] - d * z[0], x[1] - d * z[1], x[2] - d * z[2]))


def parse_axis_placement_frame(
    model: StepModel, ap_id: str
) -> tuple[Vector3, Vector3, Vector3] | None:
    value = model.entities.get(ap_id, '')
    if not value.startswith('AXIS2_PLACEMENT_3D('):
        return None
    args = parse_args_top(value)
    if len(args) < 2 or not args[1].startswith('#'):
        return None
    point_value = model.entities.get(args[1], '')
    if not point_value.startswith('CARTESIAN_POINT('):
        return None
    origin = parse_vector(point_value)
    if origin is None:
        return None
    z_axis: Vector3 = (0.0, 0.0, 1.0)
    x_axis: Vector3 = (1.0, 0.0, 0.0)
    if len(args) >= 3 and args[2].startswith('#'):
        dir_value = model.entities.get(args[2], '')
        if dir_value.startswith('DIRECTION('):
            parsed = parse_vector(dir_value)
            if parsed is not None:
                z_axis = vec_normalize(parsed)
    if len(args) >= 4 and args[3].startswith('#'):
        dir_value = model.entities.get(args[3], '')
        if dir_value.startswith('DIRECTION('):
            parsed = parse_vector(dir_value)
            if parsed is not None:
                x_axis = vec_normalize(parsed)
    x_axis = vec_orthogonalize(x_axis, z_axis)
    return origin, z_axis, x_axis


_sr_relation_cache: dict[int, dict[str, set[str]]] = {}


def _build_sr_relations(model: StepModel) -> dict[str, set[str]]:
    """Map SR id -> SR ids reachable via SHAPE_REPRESENTATION_RELATIONSHIP.

    STEP emits two flavours of SRR:
      - Plain `SHAPE_REPRESENTATION_RELATIONSHIP('','',A,B)` — an equivalence
        (e.g. SR <-> ADVANCED_BREP_SHAPE_REPRESENTATION of the same part).
        Walked BOTH directions.
      - Combined `(REPRESENTATION_RELATIONSHIP('','',A,B)REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION(...)SHAPE_REPRESENTATION_RELATIONSHIP())` — a placement:
        A (source/body) is transformed into B's (assembly) coordinate system.
        Walked TARGET -> SOURCE only, so the scope walk descends from a PD's
        assembly SR into the body without leaking upward into sibling PDs.

    Cached per StepModel instance; IDs are stable for the lifetime of the object.
    """
    cache = _sr_relation_cache.get(id(model))
    if cache is not None:
        return cache
    rel: dict[str, set[str]] = defaultdict(set)
    def looks_like_sr(eid: str) -> bool:
        v = model.entities.get(eid, '')
        if not v:
            return False
        if v.startswith((
            'SHAPE_REPRESENTATION(',
            'ADVANCED_BREP_SHAPE_REPRESENTATION(',
            'MANIFOLD_SURFACE_SHAPE_REPRESENTATION(',
            'FACETED_BREP_SHAPE_REPRESENTATION(',
        )):
            return True
        return 'REPRESENTATION' in v and '_REPRESENTATION(' in v and not v.startswith(
            ('PRODUCT_DEFINITION', 'REPRESENTATION_RELATIONSHIP')
        )
    for eid, value in model.entities.items():
        if 'SHAPE_REPRESENTATION_RELATIONSHIP' not in value:
            continue
        rr_match = re.search(
            r"REPRESENTATION_RELATIONSHIP\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*(#\d+)\s*,\s*(#\d+)\s*\)",
            value,
        )
        if not rr_match:
            continue
        source = rr_match.group(1)
        target = rr_match.group(2)
        if not (looks_like_sr(source) and looks_like_sr(target)):
            continue
        has_transform = 'REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION' in value
        rel[target].add(source)
        if not has_transform:
            rel[source].add(target)
    _sr_relation_cache[id(model)] = dict(rel)
    return _sr_relation_cache[id(model)]


def collect_pd_scope_entities(model: StepModel, pd_id: str) -> set[str]:
    """Walk SR subtrees for this PD and all descendant PDs, return entity ids reached.

    Follows SHAPE_REPRESENTATION_RELATIONSHIP edges so brep geometry parked in a
    sibling SR is included in the scope.
    """
    sr_relations = _build_sr_relations(model)
    visited_pd: set[str] = set()
    seen: set[str] = set()
    visited_sr: set[str] = set()
    pd_stack = [pd_id]
    while pd_stack:
        cur = pd_stack.pop()
        if cur in visited_pd:
            continue
        visited_pd.add(cur)
        sr = model.pd_to_sr.get(cur)
        if sr:
            sr_queue: deque[str] = deque([sr])
            while sr_queue:
                cur_sr = sr_queue.popleft()
                if cur_sr in visited_sr:
                    continue
                visited_sr.add(cur_sr)
                q: deque[str] = deque([cur_sr])
                while q:
                    eid = q.popleft()
                    if eid in seen:
                        continue
                    seen.add(eid)
                    value = model.entities.get(eid, '')
                    for ref in extract_refs(value):
                        if ref in model.entities and ref not in seen:
                            q.append(ref)
                for sib in sr_relations.get(cur_sr, ()):  # type: ignore[arg-type]
                    if sib not in visited_sr:
                        sr_queue.append(sib)
        for occ in model.children_of.get(cur, []):
            pd_stack.append(occ.child_pd)
    return seen


def _own_sr_entities(model: StepModel, pd_id: str) -> set[str]:
    """Entities reachable from this PD's own SR (and SR-related siblings),
    stopping at descendant PDs' SRs so each PD's local geometry is isolated."""
    sr = model.pd_to_sr.get(pd_id)
    if not sr:
        return set()
    sr_relations = _build_sr_relations(model)
    # SRs directly owned by any descendant PD — never cross into those.
    descendant_srs: set[str] = set()
    stack = [pd_id]
    seen_pds = {pd_id}
    while stack:
        cur = stack.pop()
        for occ in model.children_of.get(cur, []):
            if occ.child_pd in seen_pds:
                continue
            seen_pds.add(occ.child_pd)
            child_sr = model.pd_to_sr.get(occ.child_pd)
            if child_sr:
                descendant_srs.add(child_sr)
            stack.append(occ.child_pd)

    visited_sr: set[str] = set()
    seen: set[str] = set()
    sr_queue: deque[str] = deque([sr])
    while sr_queue:
        cur_sr = sr_queue.popleft()
        if cur_sr in visited_sr or cur_sr in descendant_srs:
            continue
        visited_sr.add(cur_sr)
        q: deque[str] = deque([cur_sr])
        while q:
            eid = q.popleft()
            if eid in seen:
                continue
            seen.add(eid)
            value = model.entities.get(eid, '')
            for ref in extract_refs(value):
                if ref in model.entities and ref not in seen:
                    q.append(ref)
        for sib in sr_relations.get(cur_sr, ()):  # type: ignore[arg-type]
            if sib not in visited_sr and sib not in descendant_srs:
                sr_queue.append(sib)
    return seen


def _circular_features_in_own_sr(model: StepModel, pd_id: str) -> list[dict]:
    """Read circle/cylinder features that live in this PD's own representation,
    without recursing into child PDs."""
    features: list[dict] = []
    for eid in _own_sr_entities(model, pd_id):
        value = model.entities.get(eid, '')
        kind: str | None = None
        if value.startswith('CIRCLE('):
            kind = 'circle'
        elif value.startswith('CYLINDRICAL_SURFACE('):
            kind = 'cylinder'
        else:
            continue
        args = parse_args_top(value)
        if len(args) < 3:
            continue
        refs = extract_refs(value)
        if not refs:
            continue
        placement_id = refs[0]
        try:
            radius = float(args[-1])
        except ValueError:
            continue
        frame = parse_axis_placement_frame(model, placement_id)
        if not frame:
            continue
        origin, z_axis, x_axis = frame
        features.append({
            'entity_id': eid,
            'kind': kind,
            'placement_id': placement_id,
            'origin': origin,
            'z_axis': z_axis,
            'x_axis': x_axis,
            'radius': radius,
        })
    return features


def _points_in_own_sr(model: StepModel, pd_id: str) -> list[Vector3]:
    result: list[Vector3] = []
    for eid in _own_sr_entities(model, pd_id):
        value = model.entities.get(eid, '')
        if value.startswith('CARTESIAN_POINT('):
            p = parse_vector(value)
            if p is not None:
                result.append(p)
    return result


def walk_pd_features_with_transforms(
    model: StepModel, root_pd: str,
) -> tuple[list[dict], list[Vector3]]:
    """Walk every PD under ``root_pd`` and transform its local circles/points
    into ``root_pd``'s frame by composing per-NAUO IDT placements.

    This is what STEP importers do when they render the assembly — each
    sub-part's geometry stays in source coordinates in the file, and the
    assembly placement is composed in at import time. Replicating that here
    recovers the centered pose even when the transform lives in the IDT chain
    instead of baked into CARTESIAN_POINTs.
    """
    features: list[dict] = []
    points: list[Vector3] = []
    visited: set[str] = set()
    stack: list[tuple[str, list[list[float]], Vector3]] = [
        (root_pd, _IDENTITY_R, _ZERO_T),
    ]
    while stack:
        pd, R, t = stack.pop()
        if pd in visited:
            continue
        visited.add(pd)
        for feat in _circular_features_in_own_sr(model, pd):
            features.append({
                **feat,
                'origin': vector_add(mat_apply(R, feat['origin']), t),
                'z_axis': vec_normalize(mat_apply(R, feat['z_axis'])),
                'x_axis': vec_normalize(mat_apply(R, feat['x_axis'])),
                'owner_pd': pd,
            })
        for p in _points_in_own_sr(model, pd):
            points.append(vector_add(mat_apply(R, p), t))
        for occ in model.children_of.get(pd, []):
            if occ.child_pd in visited:
                continue
            edge_R, edge_t = _idt_local_to_parent(model, occ.transform_id)
            new_R = mat_mul(R, edge_R)
            new_t = vector_add(t, mat_apply(R, edge_t))
            stack.append((occ.child_pd, new_R, new_t))
    return features, points


def collect_local_transforms(
    model: StepModel, root_pd: str,
) -> dict[str, tuple[list[list[float]], Vector3]]:
    """Return ``{pd_id: (R, t)}`` for every PD reachable below ``root_pd``
    via NAUO children. Each ``(R, t)`` maps a point expressed in ``pd_id``'s
    local frame into ``root_pd``'s local frame: ``p_root = R @ p_pd + t``.
    """
    out: dict[str, tuple[list[list[float]], Vector3]] = {
        root_pd: (_IDENTITY_R, _ZERO_T),
    }
    stack: list[str] = [root_pd]
    while stack:
        pd = stack.pop()
        R, t = out[pd]
        for occ in model.children_of.get(pd, []):
            child = occ.child_pd
            if child in out:
                continue
            edge_R, edge_t = _idt_local_to_parent(model, occ.transform_id)
            new_R = mat_mul(R, edge_R)
            new_t = vector_add(t, mat_apply(R, edge_t))
            out[child] = (new_R, new_t)
            stack.append(child)
    return out


def walk_pd_points_with_transforms(
    model: StepModel, root_pd: str,
) -> list[Vector3]:
    """All CARTESIAN_POINTs reachable under ``root_pd``, each transformed
    into ``root_pd``'s local frame via composed NAUO/IDT placements."""
    local_tx = collect_local_transforms(model, root_pd)
    out: list[Vector3] = []
    for pd, (R, t) in local_tx.items():
        for p in _points_in_own_sr(model, pd):
            out.append(vector_add(mat_apply(R, p), t))
    return out


def collect_circular_features_in_pd(model: StepModel, pd_id: str) -> list[dict]:
    """Collect CIRCLE and CYLINDRICAL_SURFACE primitives within a PD's scope."""
    scope = collect_pd_scope_entities(model, pd_id)
    features: list[dict] = []
    for eid in scope:
        value = model.entities.get(eid, '')
        kind: str | None = None
        if value.startswith('CIRCLE('):
            kind = 'circle'
        elif value.startswith('CYLINDRICAL_SURFACE('):
            kind = 'cylinder'
        else:
            continue
        args = parse_args_top(value)
        if len(args) < 3:
            continue
        refs = extract_refs(value)
        if not refs:
            continue
        placement_id = refs[0]
        try:
            radius = float(args[-1])
        except ValueError:
            continue
        frame = parse_axis_placement_frame(model, placement_id)
        if not frame:
            continue
        origin, z_axis, x_axis = frame
        features.append({
            'entity_id': eid,
            'kind': kind,
            'placement_id': placement_id,
            'origin': origin,
            'z_axis': z_axis,
            'x_axis': x_axis,
            'radius': radius,
        })
    return features


def collect_points_in_pd(model: StepModel, pd_id: str) -> list[Vector3]:
    scope = collect_pd_scope_entities(model, pd_id)
    points: list[Vector3] = []
    for eid in scope:
        value = model.entities.get(eid, '')
        if value.startswith('CARTESIAN_POINT('):
            p = parse_vector(value)
            if p is not None:
                points.append(p)
    return points


def choose_primary_anchor(features: list[dict]) -> dict | None:
    """Pick the widest vertical-axis circle sitting near the geometry's bottom.

    The previous heuristic (largest-radius first) confused bucket openings,
    lid rims, and bottom rims whenever the opening was the widest circle.
    Selecting from a band near min-Z biases toward the object's true bottom;
    within that band we still prefer the widest rim to avoid tiny holes or
    drain features.
    """
    if not features:
        return None
    horizontal = [f for f in features if abs(f['z_axis'][2]) > 0.9]
    candidates = horizontal or features
    z_min = min(f['origin'][2] for f in candidates)
    z_max = max(f['origin'][2] for f in candidates)
    band = max(20.0, (z_max - z_min) * 0.10)
    near_bottom = [f for f in candidates if f['origin'][2] <= z_min + band]
    max_r = max(f['radius'] for f in near_bottom)
    tol_r = max(1.0, max_r * 0.05)
    top = [f for f in near_bottom if f['radius'] >= max_r - tol_r]
    top.sort(key=lambda f: (f['origin'][2], -f['radius']))
    return top[0]


def points_centroid(points: list[Vector3]) -> Vector3 | None:
    if not points:
        return None
    n = len(points)
    return (
        sum(p[0] for p in points) / n,
        sum(p[1] for p in points) / n,
        sum(p[2] for p in points) / n,
    )


def resolve_yaw_direction(
    anchor_origin: Vector3, anchor_z: Vector3, centroid_pt: Vector3 | None
) -> Vector3 | None:
    if centroid_pt is None:
        return None
    d = vector_sub(centroid_pt, anchor_origin)
    dz = vec_dot(d, anchor_z)
    d_proj = (
        d[0] - dz * anchor_z[0],
        d[1] - dz * anchor_z[1],
        d[2] - dz * anchor_z[2],
    )
    m = (d_proj[0] ** 2 + d_proj[1] ** 2 + d_proj[2] ** 2) ** 0.5
    if m < 1e-6:
        return None
    return (d_proj[0] / m, d_proj[1] / m, d_proj[2] / m)


def build_frame_matrix(x: Vector3, z: Vector3) -> list[list[float]]:
    y = vec_cross(z, x)
    return [
        [x[0], y[0], z[0]],
        [x[1], y[1], z[1]],
        [x[2], y[2], z[2]],
    ]


def mat_mul(A: list[list[float]], B: list[list[float]]) -> list[list[float]]:
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def mat_transpose(A: list[list[float]]) -> list[list[float]]:
    return [[A[j][i] for j in range(3)] for i in range(3)]


def mat_apply(A: list[list[float]], v: Vector3) -> Vector3:
    return (
        A[0][0] * v[0] + A[0][1] * v[1] + A[0][2] * v[2],
        A[1][0] * v[0] + A[1][1] * v[1] + A[1][2] * v[2],
        A[2][0] * v[0] + A[2][1] * v[1] + A[2][2] * v[2],
    )


def rotation_to_rpy(R: list[list[float]]) -> Vector3:
    """Return (roll, pitch, yaw) in radians under ZYX convention:
    R = Rz(yaw) @ Ry(pitch) @ Rx(roll)."""
    sy = (R[0][0] ** 2 + R[1][0] ** 2) ** 0.5
    if sy < 1e-6:
        roll = math.atan2(-R[1][2], R[1][1])
        pitch = math.atan2(-R[2][0], sy)
        yaw = 0.0
    else:
        roll = math.atan2(R[2][1], R[2][2])
        pitch = math.atan2(-R[2][0], sy)
        yaw = math.atan2(R[1][0], R[0][0])
    return (roll, pitch, yaw)


_IDENTITY_R: list[list[float]] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
_ZERO_T: Vector3 = (0.0, 0.0, 0.0)


def _idt_local_to_parent(
    model: StepModel, transform_id: str | None,
) -> tuple[list[list[float]], Vector3]:
    """Return (R, t) encoding an ITEM_DEFINED_TRANSFORMATION so that a point
    expressed in the child representation's frame maps into the parent's
    frame via ``p_parent = R @ p_child + t``.

    STEP's ITEM_DEFINED_TRANSFORMATION carries two AXIS2_PLACEMENT_3D items:
    ``transform_item_1`` is the reference datum in the parent (typically the
    parent's identity placement) and ``transform_item_2`` is the child's
    placement expressed in the parent's frame. The transform maps points
    from the child's local coords (= ``transform_item_2``'s local coords)
    into the parent's local coords (= ``transform_item_1``'s local coords):
    ``p1 = R1^T @ R2 @ p2 + R1^T @ (o2 - o1)``.
    """
    if not transform_id:
        return _IDENTITY_R, _ZERO_T
    value = model.entities.get(transform_id, '')
    if not value.startswith('ITEM_DEFINED_TRANSFORMATION('):
        return _IDENTITY_R, _ZERO_T
    args = parse_args_top(value)
    if len(args) < 4:
        return _IDENTITY_R, _ZERO_T
    frame_1 = parse_axis_placement_frame(model, args[2])
    frame_2 = parse_axis_placement_frame(model, args[3])
    if frame_1 is None:
        R1 = _IDENTITY_R
        o1: Vector3 = _ZERO_T
    else:
        o1, z1, x1 = frame_1
        R1 = build_frame_matrix(x1, z1)
    if frame_2 is None:
        R2 = _IDENTITY_R
        o2: Vector3 = _ZERO_T
    else:
        o2, z2, x2 = frame_2
        R2 = build_frame_matrix(x2, z2)
    R1_inv = mat_transpose(R1)
    R = mat_mul(R1_inv, R2)
    t = mat_apply(R1_inv, vector_sub(o2, o1))
    return R, t


def load_companion_centering_transform(
    step_path: Path,
) -> tuple[list[list[float]], Vector3] | None:
    """Load a ``*_transform.json`` sitting next to a processed STEP file.

    The centering pipeline records the rigid transform that would re-center
    the STEP content onto a selected anchor but does not necessarily bake it
    into the STEP geometry. Applying this transform on top of the raw STEP
    placements produces coordinates in the centered / expected world frame.
    Returns ``None`` when no companion file exists.
    """
    stem = step_path.stem
    if stem.endswith('_centered'):
        stem = stem[: -len('_centered')]
    candidate = step_path.with_name(f'{stem}_transform.json')
    if not candidate.exists():
        return None
    try:
        data = json.loads(candidate.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    t_section = data.get('transform') or {}
    R = t_section.get('rotation_matrix_3x3')
    t = t_section.get('translation')
    if not R or not t or len(R) != 3 or any(len(row) != 3 for row in R):
        return None
    R_out = [[float(v) for v in row] for row in R]
    t_out: Vector3 = (float(t[0]), float(t[1]), float(t[2]))
    return R_out, t_out


def compose_world_transforms(
    outer: tuple[list[list[float]], Vector3] | None,
    inner: tuple[list[list[float]], Vector3],
) -> tuple[list[list[float]], Vector3]:
    """Return ``outer ∘ inner`` so that applying ``inner`` then ``outer`` to a
    point gives the composed result: ``p' = R_out · (R_in p + t_in) + t_out``.
    If ``outer`` is ``None``, ``inner`` is returned unchanged.
    """
    if outer is None:
        return inner
    R_out, t_out = outer
    R_in, t_in = inner
    R = mat_mul(R_out, R_in)
    t = vector_add(mat_apply(R_out, t_in), t_out)
    return R, t


def pd_to_root_transform(
    model: StepModel, pd_id: str, preferred_nauo: str | None = None,
) -> tuple[list[list[float]], Vector3]:
    """Compose per-edge NAUO transforms walking from ``pd_id`` up to a root PD.

    Returns (R, t) such that ``p_root = R @ p_local + t``. When a PD has
    multiple parent occurrences, ``preferred_nauo`` picks the chain that
    matches that specific instance; otherwise the first parent is used.
    """
    R = _IDENTITY_R
    t = _ZERO_T
    current = pd_id
    visited: set[str] = set()
    first = True
    while current not in visited:
        visited.add(current)
        parents = model.parents_of.get(current, [])
        if not parents:
            break
        if first and preferred_nauo:
            occ = next(
                (p for p in parents if p.nauo_id == preferred_nauo), parents[0],
            )
        else:
            occ = parents[0]
        first = False
        edge_R, edge_t = _idt_local_to_parent(model, occ.transform_id)
        # p_parent = edge_R @ (R @ p_local + t) + edge_t
        R = mat_mul(edge_R, R)
        t = vector_add(mat_apply(edge_R, t), edge_t)
        current = occ.parent_pd
    return R, t


def _transform_feature(
    feature: dict, R: list[list[float]], t: Vector3,
) -> dict:
    return {
        **feature,
        'origin': vector_add(mat_apply(R, feature['origin']), t),
        'z_axis': vec_normalize(mat_apply(R, feature['z_axis'])),
        'x_axis': vec_normalize(mat_apply(R, feature['x_axis'])),
    }


def compute_anchor_frame_for_pd(
    model: StepModel, pd_id: str,
    world_transform: tuple[list[list[float]], Vector3] | None = None,
) -> dict | None:
    features, points = walk_pd_features_with_transforms(model, pd_id)
    centroid_pt = points_centroid(points)

    # Move features + centroid into the centered/world frame BEFORE picking the
    # anchor. The picker uses "lowest Z among near-max-radius circles" as its
    # bottom-of-cylinder heuristic, which is only valid once Z points the way
    # the centering pipeline intended. Doing this first guarantees the same
    # rim is selected across component and scene, even when the raw STEP files
    # disagree about which direction is "up".
    if world_transform is not None:
        R, t = world_transform
        features = [_transform_feature(f, R, t) for f in features]
        if centroid_pt is not None:
            centroid_pt = vector_add(mat_apply(R, centroid_pt), t)

    anchor = choose_primary_anchor(features)
    if not anchor:
        return None

    # STEP stores CIRCLE / CYLINDRICAL_SURFACE normals in arbitrary direction.
    # Force a consistent convention across files: Z axis points from the
    # anchor origin toward the geometry centroid. This cancels ±Z ambiguity
    # and gives the same frame for the component file and every instance.
    z_axis = anchor['z_axis']
    if centroid_pt is not None:
        d = vector_sub(centroid_pt, anchor['origin'])
        if vec_dot(d, z_axis) < 0:
            z_axis = (-z_axis[0], -z_axis[1], -z_axis[2])

    yaw_x = resolve_yaw_direction(anchor['origin'], z_axis, centroid_pt)
    x_axis = vec_orthogonalize(yaw_x if yaw_x is not None else anchor['x_axis'], z_axis)

    return {
        'origin': anchor['origin'],
        'z_axis': z_axis,
        'x_axis': x_axis,
        'radius': anchor['radius'],
        'anchor_entity_id': anchor['entity_id'],
        'anchor_kind': anchor['kind'],
        'yaw_resolved_by_centroid': yaw_x is not None,
    }


def compute_pose(comp_frame: dict, inst_frame: dict) -> dict:
    Rc = build_frame_matrix(comp_frame['x_axis'], comp_frame['z_axis'])
    Rs = build_frame_matrix(inst_frame['x_axis'], inst_frame['z_axis'])
    R_out = mat_mul(Rs, mat_transpose(Rc))
    origin_c_transformed = mat_apply(R_out, comp_frame['origin'])
    xyz = vector_sub(inst_frame['origin'], origin_c_transformed)
    rpy = rotation_to_rpy(R_out)
    return {'xyz': xyz, 'rpy_rad': rpy, 'rotation_matrix': R_out}


# ============================================================================
# Multi-feature registration pipeline
# ============================================================================
#
# Overview:
#   detect_geometric_features_for_pd  ->  list[GeometricFeature]
#   match_features_between            ->  list[FeatureMatch]
#   fuse_feature_references           ->  R, t, residuals
#   build_fused_frame                 ->  debug frame for visualization
#
# Each stage emits data intended for inspection; the UI reads the debug JSON.


FEATURE_DETECTION_DEFAULTS = {
    'include_circles':       True,
    'include_cylinders':     True,
    'include_planes':        True,
    'include_pca':           True,
    'plane_min_area_mm2':    500.0,
    'top_k_circles':         6,
    'top_k_cylinders':       4,
    'top_k_planes':          6,
    'min_match_similarity':  0.5,
    'direction_weight':      0.5,   # relative to point-pair weight in Kabsch H
    # Deduplication tolerances (features collapsed if all three match).
    'dedup_center_tol_mm':   0.5,
    'dedup_radius_tol_mm':   0.05,
    'dedup_normal_tol':      0.01,  # 1 - |cos(angle)|
    # Iterative outlier rejection after initial Kabsch fit. Disabled by
    # default: with seed-scoring + final re-pair under the winning R, the
    # winning rotation is already robust. Re-fitting R from inliers strips
    # disambiguating features (e.g., silica column side planes) and drifts
    # to symmetry-related basins.
    'refine_iterations':     0,
    'refine_pt_sigma':       3.0,   # drop pairs with pt residual > sigma * median
    'refine_dir_sigma':      3.0,
    'refine_min_matches':    3,
}


@dataclass
class GeometricFeature:
    feature_id: str
    source: str                     # 'step-circle' | 'step-cylinder' | 'step-plane' | 'mesh-pca'
    feature_type: str               # 'circle' | 'cylinder' | 'plane' | 'pca-axis'
    center: Vector3
    normal: Vector3
    ref_direction: Vector3 | None
    radius: float | None
    size: Vector3 | None
    confidence: float
    entity_id: str | None

    def to_json(self) -> dict:
        return {
            'feature_id':    self.feature_id,
            'source':        self.source,
            'feature_type':  self.feature_type,
            'center':        vector_round(self.center),
            'normal':        vector_round(self.normal),
            'ref_direction': vector_round(self.ref_direction) if self.ref_direction else None,
            'radius':        None if self.radius is None else round(self.radius, 6),
            'size':          vector_round(self.size) if self.size else None,
            'confidence':    round(self.confidence, 6),
            'entity_id':     self.entity_id,
        }


def _detect_circles_as_features(
    model: StepModel, pd_id: str, scope: set[str], prefix: str, top_k: int
) -> list[GeometricFeature]:
    raw: list[dict] = []
    # sorted() pins iteration order so tied-radius features aren't re-ordered
    # by hash randomization, which would flip greedy-similarity pairings.
    for eid in sorted(scope):
        value = model.entities.get(eid, '')
        if not value.startswith('CIRCLE('):
            continue
        args = parse_args_top(value)
        refs = extract_refs(value)
        if len(args) < 3 or not refs:
            continue
        try:
            radius = float(args[-1])
        except ValueError:
            continue
        frame = parse_axis_placement_frame(model, refs[0])
        if not frame:
            continue
        origin, z_axis, x_axis = frame
        raw.append({'eid': eid, 'origin': origin, 'z': z_axis, 'x': x_axis, 'r': radius})
    if not raw:
        return []
    r_max = max(r['r'] for r in raw) or 1e-9
    raw.sort(key=lambda d: -d['r'])
    raw = raw[:top_k]
    features = []
    for r in raw:
        conf = min(1.0, r['r'] / r_max)
        features.append(GeometricFeature(
            feature_id=f'{prefix}:circle:{r["eid"]}',
            source='step-circle',
            feature_type='circle',
            center=r['origin'],
            normal=r['z'],
            ref_direction=r['x'],
            radius=r['r'],
            size=None,
            confidence=conf,
            entity_id=r['eid'],
        ))
    return features


def _detect_cylinders_as_features(
    model: StepModel, pd_id: str, scope: set[str], prefix: str, top_k: int
) -> list[GeometricFeature]:
    raw: list[dict] = []
    for eid in sorted(scope):
        value = model.entities.get(eid, '')
        if not value.startswith('CYLINDRICAL_SURFACE('):
            continue
        args = parse_args_top(value)
        refs = extract_refs(value)
        if len(args) < 3 or not refs:
            continue
        try:
            radius = float(args[-1])
        except ValueError:
            continue
        frame = parse_axis_placement_frame(model, refs[0])
        if not frame:
            continue
        origin, z_axis, x_axis = frame
        raw.append({'eid': eid, 'origin': origin, 'z': z_axis, 'x': x_axis, 'r': radius})
    if not raw:
        return []
    r_max = max(r['r'] for r in raw) or 1e-9
    raw.sort(key=lambda d: -d['r'])
    raw = raw[:top_k]
    features = []
    for r in raw:
        conf = min(1.0, r['r'] / r_max)
        features.append(GeometricFeature(
            feature_id=f'{prefix}:cylinder:{r["eid"]}',
            source='step-cylinder',
            feature_type='cylinder',
            center=r['origin'],
            normal=r['z'],
            ref_direction=r['x'],
            radius=r['r'],
            size=None,
            confidence=conf,
            entity_id=r['eid'],
        ))
    return features


def _detect_planes_as_features(
    model: StepModel, pd_id: str, scope: set[str], prefix: str,
    min_area: float, top_k: int,
) -> list[GeometricFeature]:
    # Step 1: collect PLANE entities with frame.
    plane_frames: dict[str, tuple[Vector3, Vector3, Vector3]] = {}
    for eid in sorted(scope):
        value = model.entities.get(eid, '')
        if not value.startswith('PLANE('):
            continue
        refs = extract_refs(value)
        if not refs:
            continue
        frame = parse_axis_placement_frame(model, refs[0])
        if frame:
            plane_frames[eid] = frame
    if not plane_frames:
        return []
    # Step 2: walk ADVANCED_FACE entities, collect vertex points per plane.
    plane_points: dict[str, list[Vector3]] = defaultdict(list)
    for eid in sorted(scope):
        value = model.entities.get(eid, '')
        if not value.startswith('ADVANCED_FACE('):
            continue
        args = parse_args_top(value)
        if len(args) < 3 or not args[2].startswith('#'):
            continue
        plane_id = args[2]
        if plane_id not in plane_frames:
            continue
        # Local BFS through face refs, collecting points.
        seen: set[str] = set()
        q: deque[str] = deque([eid])
        while q:
            cur = q.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            v = model.entities.get(cur, '')
            if v.startswith('CARTESIAN_POINT('):
                p = parse_vector(v)
                if p is not None:
                    plane_points[plane_id].append(p)
            for r in extract_refs(v):
                if r in model.entities and r not in seen:
                    q.append(r)
    # Step 3: compute area per plane, filter.
    raw: list[dict] = []
    for plane_id, (origin, z_axis, x_axis) in plane_frames.items():
        pts = plane_points.get(plane_id, [])
        if not pts:
            continue
        y_axis = vec_cross(z_axis, x_axis)
        us = [vec_dot(vector_sub(p, origin), x_axis) for p in pts]
        vs = [vec_dot(vector_sub(p, origin), y_axis) for p in pts]
        extent_u = max(us) - min(us)
        extent_v = max(vs) - min(vs)
        area = extent_u * extent_v
        if area < min_area:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        cz = sum(p[2] for p in pts) / len(pts)
        raw.append({
            'eid': plane_id,
            'center': (cx, cy, cz),
            'origin': origin,
            'z': z_axis,
            'x': x_axis,
            'area': area,
            'size': (extent_u, extent_v, 0.0),
        })
    if not raw:
        return []
    area_max = max(r['area'] for r in raw) or 1e-9
    raw.sort(key=lambda d: -d['area'])
    raw = raw[:top_k]
    features = []
    for r in raw:
        conf = min(1.0, r['area'] / area_max)
        features.append(GeometricFeature(
            feature_id=f'{prefix}:plane:{r["eid"]}',
            source='step-plane',
            feature_type='plane',
            center=r['center'],
            normal=r['z'],
            ref_direction=r['x'],
            radius=None,
            size=r['size'],
            confidence=conf,
            entity_id=r['eid'],
        ))
    return features


def _detect_pca_features(
    model: StepModel, pd_id: str, prefix: str,
) -> list[GeometricFeature]:
    import numpy as np
    points = collect_points_in_pd(model, pd_id)
    if len(points) < 4:
        return []
    P = np.array(points, dtype=float)
    c = P.mean(axis=0)
    C = P - c
    cov = (C.T @ C) / max(1, len(C) - 1)
    eigvals, eigvecs = np.linalg.eigh(cov)
    order = np.argsort(-eigvals)
    eigvals = eigvals[order]
    eigvecs = eigvecs[:, order]
    max_e = float(eigvals[0]) or 1.0
    out = []
    for i in range(3):
        conf = float(eigvals[i] / max_e) * 0.3
        out.append(GeometricFeature(
            feature_id=f'{prefix}:pca:{i}',
            source='mesh-pca',
            feature_type='pca-axis',
            center=tuple(float(v) for v in c),  # type: ignore[arg-type]
            normal=tuple(float(v) for v in eigvecs[:, i]),  # type: ignore[arg-type]
            ref_direction=None,
            radius=None,
            size=tuple(float(v) for v in eigvals[:3]),  # type: ignore[arg-type]
            confidence=conf,
            entity_id=None,
        ))
    return out


def _dedupe_features(
    features: list[GeometricFeature],
    center_tol: float, radius_tol: float, normal_tol: float,
) -> list[GeometricFeature]:
    """Collapse near-duplicate features (same type + center + radius + normal axis).

    Same cylindrical rim frequently emits many CIRCLE/CYLINDRICAL_SURFACE entities
    from different face loops. Left unmerged, the matcher pairs them arbitrarily,
    which drives direction residuals up since the duplicates can be antiparallel.
    """
    kept: list[GeometricFeature] = []
    for f in features:
        if f.feature_type == 'pca-axis':
            kept.append(f)
            continue
        collapsed = False
        for k in kept:
            if k.feature_type != f.feature_type:
                continue
            dv = vector_sub(k.center, f.center)
            dc = (dv[0] ** 2 + dv[1] ** 2 + dv[2] ** 2) ** 0.5
            if dc > center_tol:
                continue
            if f.radius is not None and k.radius is not None:
                if abs(f.radius - k.radius) > radius_tol:
                    continue
            # Allow antiparallel normals (STEP faces can flip arbitrarily).
            cos_n = abs(vec_dot(k.normal, f.normal))
            if 1.0 - cos_n > normal_tol:
                continue
            # Same feature — keep the higher-confidence one.
            if f.confidence > k.confidence:
                idx = kept.index(k)
                kept[idx] = f
            collapsed = True
            break
        if not collapsed:
            kept.append(f)
    return kept


def detect_geometric_features_for_pd(
    model: StepModel, pd_id: str, prefix: str, cfg: dict | None = None,
) -> list[GeometricFeature]:
    cfg = {**FEATURE_DETECTION_DEFAULTS, **(cfg or {})}
    scope = collect_pd_scope_entities(model, pd_id)
    out: list[GeometricFeature] = []
    # Detect with a generous multiplier, then dedupe, then trim to top_k.
    mult = 4
    if cfg['include_circles']:
        out += _detect_circles_as_features(
            model, pd_id, scope, prefix, cfg['top_k_circles'] * mult,
        )
    if cfg['include_cylinders']:
        out += _detect_cylinders_as_features(
            model, pd_id, scope, prefix, cfg['top_k_cylinders'] * mult,
        )
    if cfg['include_planes']:
        out += _detect_planes_as_features(
            model, pd_id, scope, prefix,
            cfg['plane_min_area_mm2'], cfg['top_k_planes'] * mult,
        )
    if cfg['include_pca']:
        out += _detect_pca_features(model, pd_id, prefix)
    out = _dedupe_features(
        out,
        cfg['dedup_center_tol_mm'],
        cfg['dedup_radius_tol_mm'],
        cfg['dedup_normal_tol'],
    )
    # Trim per-type to final top_k, preserving detector priority.
    per_type_cap = {
        'circle':   cfg['top_k_circles'],
        'cylinder': cfg['top_k_cylinders'],
        'plane':    cfg['top_k_planes'],
    }
    trimmed: list[GeometricFeature] = []
    counts: dict[str, int] = defaultdict(int)
    for f in sorted(out, key=lambda g: -g.confidence):
        cap = per_type_cap.get(f.feature_type)
        if cap is not None and counts[f.feature_type] >= cap:
            continue
        counts[f.feature_type] += 1
        trimmed.append(f)
    return trimmed


def _transform_geom_feature(
    f: GeometricFeature, R: list[list[float]], t: Vector3,
) -> GeometricFeature:
    """Apply a rigid (R, t) to a feature's center/normal/ref_direction."""
    center = vector_add(mat_apply(R, f.center), t)
    normal = vec_normalize(mat_apply(R, f.normal))
    ref_direction = (
        vec_normalize(mat_apply(R, f.ref_direction))
        if f.ref_direction is not None else None
    )
    return GeometricFeature(
        feature_id=f.feature_id,
        source=f.source,
        feature_type=f.feature_type,
        center=center,
        normal=normal,
        ref_direction=ref_direction,
        radius=f.radius,
        size=f.size,
        confidence=f.confidence,
        entity_id=f.entity_id,
    )


def detect_geometric_features_for_pd_tx(
    model: StepModel, pd_id: str, prefix: str,
    cfg: dict | None = None,
    world_transform: tuple[list[list[float]], Vector3] | None = None,
) -> list[GeometricFeature]:
    """Transform-aware feature detection.

    Walks the NAUO tree under ``pd_id``, running circle/cylinder/plane
    detectors in each PD's own SR, then transforms every feature into a
    common frame. Without ``world_transform`` that frame is ``pd_id``'s
    local frame; with it, features land in whatever frame the transform
    targets (typically the assembly/scene root's centered world).

    Non-parallel features — handles, side planes, horizontal cylinders —
    provide the yaw anchor that rotationally-symmetric rims alone cannot.
    """
    cfg = {**FEATURE_DETECTION_DEFAULTS, **(cfg or {})}
    local_tx = collect_local_transforms(model, pd_id)
    mult = 4
    raw: list[GeometricFeature] = []
    for cur_pd, (R_loc, t_loc) in local_tx.items():
        if world_transform is not None:
            R_w, t_w = world_transform
            R = mat_mul(R_w, R_loc)
            t = vector_add(mat_apply(R_w, t_loc), t_w)
        else:
            R, t = R_loc, t_loc
        scope = _own_sr_entities(model, cur_pd)
        local: list[GeometricFeature] = []
        if cfg['include_circles']:
            local += _detect_circles_as_features(
                model, cur_pd, scope, prefix, cfg['top_k_circles'] * mult,
            )
        if cfg['include_cylinders']:
            local += _detect_cylinders_as_features(
                model, cur_pd, scope, prefix, cfg['top_k_cylinders'] * mult,
            )
        if cfg['include_planes']:
            local += _detect_planes_as_features(
                model, cur_pd, scope, prefix,
                cfg['plane_min_area_mm2'], cfg['top_k_planes'] * mult,
            )
        for f in local:
            raw.append(_transform_geom_feature(f, R, t))
    if cfg['include_pca']:
        raw += _detect_pca_features_tx(model, pd_id, prefix, world_transform)
    raw = _dedupe_features(
        raw,
        cfg['dedup_center_tol_mm'],
        cfg['dedup_radius_tol_mm'],
        cfg['dedup_normal_tol'],
    )
    per_type_cap = {
        'circle':   cfg['top_k_circles'],
        'cylinder': cfg['top_k_cylinders'],
        'plane':    cfg['top_k_planes'],
    }
    trimmed: list[GeometricFeature] = []
    counts: dict[str, int] = defaultdict(int)
    for f in sorted(raw, key=lambda g: -g.confidence):
        cap = per_type_cap.get(f.feature_type)
        if cap is not None and counts[f.feature_type] >= cap:
            continue
        counts[f.feature_type] += 1
        trimmed.append(f)
    return trimmed


def _detect_pca_features_tx(
    model: StepModel, pd_id: str, prefix: str,
    world_transform: tuple[list[list[float]], Vector3] | None,
) -> list[GeometricFeature]:
    """PCA on all points under ``pd_id``, each composed through the NAUO
    chain into a common frame so the principal axes reflect the scene
    placement rather than per-subpart-local clouds."""
    import numpy as np
    points = walk_pd_points_with_transforms(model, pd_id)
    if world_transform is not None:
        R_w, t_w = world_transform
        points = [vector_add(mat_apply(R_w, p), t_w) for p in points]
    if len(points) < 4:
        return []
    P = np.array(points, dtype=float)
    c = P.mean(axis=0)
    C = P - c
    cov = (C.T @ C) / max(1, len(C) - 1)
    eigvals, eigvecs = np.linalg.eigh(cov)
    order = np.argsort(-eigvals)
    eigvals = eigvals[order]
    eigvecs = eigvecs[:, order]
    max_e = float(eigvals[0]) or 1.0
    out = []
    for i in range(3):
        conf = float(eigvals[i] / max_e) * 0.3
        out.append(GeometricFeature(
            feature_id=f'{prefix}:pca:{i}',
            source='mesh-pca',
            feature_type='pca-axis',
            center=tuple(float(v) for v in c),  # type: ignore[arg-type]
            normal=tuple(float(v) for v in eigvecs[:, i]),  # type: ignore[arg-type]
            ref_direction=None,
            radius=None,
            size=tuple(float(v) for v in eigvals[:3]),  # type: ignore[arg-type]
            confidence=conf,
            entity_id=None,
        ))
    return out


def _filter_unique_asset_features(
    features: list[GeometricFeature],
    tol_rel: float = 0.05,
    min_kept: int = 3,
) -> list[GeometricFeature]:
    """Keep only asset features whose geometric size is unique within their type.

    A feature survives when no other same-type feature shares its radius
    (circles/cylinders) or face area (planes) within ``tol_rel``. Near-duplicate
    features are ambiguous anchors — SVD pairs them arbitrarily, which flips
    chirality on near-rotationally-symmetric assets (e.g., the silica-column
    bracket with four identical mounting holes). PCA axes and features with no
    size metadata are always kept. Falls back to the original list when fewer
    than ``min_kept`` survive, so a geometrically-plain asset still matches."""
    by_type: dict[str, list[GeometricFeature]] = defaultdict(list)
    for f in features:
        by_type[f.feature_type].append(f)

    def size_of(t: str, f: GeometricFeature) -> float | None:
        if t in ('circle', 'cylinder'):
            return f.radius
        if t == 'plane' and f.size:
            return f.size[0] * f.size[1]
        return None

    kept: list[GeometricFeature] = []
    for t, group in by_type.items():
        if t == 'pca-axis':
            kept.extend(group)
            continue
        for f in group:
            sf = size_of(t, f)
            if sf is None:
                kept.append(f)
                continue
            is_unique = True
            for g in group:
                if g is f:
                    continue
                sg = size_of(t, g)
                if sg is None:
                    continue
                ref = max(sf, sg, 1e-6)
                if abs(sf - sg) / ref < tol_rel:
                    is_unique = False
                    break
            if is_unique:
                kept.append(f)
    # PCA axes share the same center (the centroid), so they can't disambiguate
    # rotation alone — count only real geometric features toward the floor.
    real_kept = sum(1 for f in kept if f.feature_type != 'pca-axis')
    if real_kept < min_kept:
        return features
    return kept


def _feature_similarity(a: GeometricFeature, s: GeometricFeature) -> float:
    if a.feature_type != s.feature_type:
        return 0.0
    size_sim = 1.0
    if a.feature_type in ('circle', 'cylinder'):
        if a.radius is not None and s.radius is not None:
            ref = max(a.radius, s.radius, 1e-6)
            size_sim = math.exp(-abs(a.radius - s.radius) / (ref * 0.1))
        else:
            size_sim = 0.5
    elif a.feature_type == 'plane':
        if a.size and s.size:
            area_a = a.size[0] * a.size[1]
            area_s = s.size[0] * s.size[1]
            ref = max(area_a, area_s, 1.0)
            size_sim = math.exp(-abs(area_a - area_s) / (ref * 0.3))
    return size_sim * min(a.confidence, s.confidence)


def match_features_between_asset_and_scene(
    asset: list[GeometricFeature],
    scene: list[GeometricFeature],
    min_similarity: float = 0.25,
) -> list[dict]:
    asset = _filter_unique_asset_features(asset)
    by_type_a: dict[str, list[GeometricFeature]] = defaultdict(list)
    by_type_s: dict[str, list[GeometricFeature]] = defaultdict(list)
    for f in asset:
        by_type_a[f.feature_type].append(f)
    for f in scene:
        by_type_s[f.feature_type].append(f)
    matches: list[dict] = []
    for t in sorted(set(by_type_a) | set(by_type_s)):
        A = by_type_a.get(t, [])
        S = by_type_s.get(t, [])
        if not A or not S:
            continue
        pairs = []
        if t == 'pca-axis':
            # PCA axes pair by rank (they're already emitted in decreasing size).
            for i in range(min(len(A), len(S))):
                sim = min(A[i].confidence, S[i].confidence)
                if sim >= min_similarity:
                    pairs.append((sim, i, i))
        else:
            for i, a in enumerate(A):
                for j, s in enumerate(S):
                    sim = _feature_similarity(a, s)
                    if sim >= min_similarity:
                        pairs.append((sim, i, j))
        pairs.sort(key=lambda x: -x[0])
        used_a: set[int] = set()
        used_s: set[int] = set()
        for sim, i, j in pairs:
            if i in used_a or j in used_s:
                continue
            used_a.add(i)
            used_s.add(j)
            matches.append({
                'asset_feature': A[i],
                'scene_feature': S[j],
                'similarity': sim,
            })
    return matches


def _sign_align_direction(
    d: Vector3, feature_center: Vector3, reference_center: Vector3 | None
) -> Vector3:
    if reference_center is None:
        return d
    v = vector_sub(reference_center, feature_center)
    if vec_dot(v, d) < 0:
        return (-d[0], -d[1], -d[2])
    return d


def _kabsch_fit(
    PA, PS, WP, DA, DS, WD,
):
    """Weighted Kabsch with direction pairs. Returns (R, t, p_bar, q_bar) or None."""
    import numpy as np
    wp_sum = WP.sum()
    if wp_sum <= 0:
        return None
    w_norm = WP / wp_sum
    p_bar = (PA.T @ w_norm).reshape(3)
    q_bar = (PS.T @ w_norm).reshape(3)
    PC = PA - p_bar
    QC = PS - q_bar
    H = np.zeros((3, 3))
    for i in range(len(PA)):
        H += WP[i] * np.outer(PC[i], QC[i])
    for j in range(len(DA)):
        H += WD[j] * np.outer(DA[j], DS[j])
    U, _, Vt = np.linalg.svd(H)
    V = Vt.T
    d = np.sign(np.linalg.det(V @ U.T)) or 1.0
    R = V @ np.diag([1.0, 1.0, float(d)]) @ U.T
    t = q_bar - R @ p_bar
    return R, t, p_bar, q_bar


def _residuals_for_fit(R, t, PA, PS, DA, DS):
    """Per-match (point_residual_mm, direction_residual_rad)."""
    import numpy as np
    out = []
    for i in range(len(PA)):
        pred = R @ PA[i] + t
        res_pt = float(np.linalg.norm(PS[i] - pred))
        pred_d = R @ DA[i]
        cos_sim = float(np.clip(np.dot(pred_d, DS[i]), -1.0, 1.0))
        res_d = float(math.acos(cos_sim))
        out.append((res_pt, res_d))
    return out


_AXIS_ALIGNED_ROTATIONS_CACHE: list[list[list[float]]] | None = None


def _axis_aligned_rotations() -> list[list[list[float]]]:
    """The 24 proper rotation matrices of a cube — permutations of basis
    axes with signs such that det = +1. Used to seed ICP from each of the
    six "which asset axis maps to which world axis" hypotheses crossed
    with four in-plane twists."""
    global _AXIS_ALIGNED_ROTATIONS_CACHE
    if _AXIS_ALIGNED_ROTATIONS_CACHE is not None:
        return _AXIS_ALIGNED_ROTATIONS_CACHE
    import itertools
    mats: list[list[list[float]]] = []
    basis = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    for perm in itertools.permutations(range(3)):
        for sx in (1.0, -1.0):
            for sy in (1.0, -1.0):
                for sz in (1.0, -1.0):
                    cols = [
                        [basis[perm[0]][r] * sx for r in range(3)],
                        [basis[perm[1]][r] * sy for r in range(3)],
                        [basis[perm[2]][r] * sz for r in range(3)],
                    ]
                    R = [[cols[c][r] for c in range(3)] for r in range(3)]
                    # det = +1 only.
                    det = (
                        R[0][0] * (R[1][1] * R[2][2] - R[1][2] * R[2][1])
                        - R[0][1] * (R[1][0] * R[2][2] - R[1][2] * R[2][0])
                        + R[0][2] * (R[1][0] * R[2][1] - R[1][1] * R[2][0])
                    )
                    if abs(det - 1.0) < 1e-6:
                        mats.append(R)
    _AXIS_ALIGNED_ROTATIONS_CACHE = mats
    return mats


def _rematch_by_predicted_position(
    asset_features: list[GeometricFeature],
    scene_features: list[GeometricFeature],
    R, t,
    min_similarity: float,
) -> list[dict]:
    """ICP-style re-pairing: for each asset feature, predict its scene
    location as ``R @ center + t`` and greedily pair it with the nearest
    same-type unused scene feature. Resolves ties that greedy-on-similarity
    matching leaves ambiguous (e.g., two r=19.15 circles of a tilted
    cylinder's top and bottom rims pairing crossed).
    """
    import numpy as np
    by_type_a: dict[str, list[tuple[int, GeometricFeature]]] = defaultdict(list)
    by_type_s: dict[str, list[tuple[int, GeometricFeature]]] = defaultdict(list)
    for i, a in enumerate(asset_features):
        by_type_a[a.feature_type].append((i, a))
    for j, s in enumerate(scene_features):
        by_type_s[s.feature_type].append((j, s))
    out: list[dict] = []
    for tname in sorted(set(by_type_a) | set(by_type_s)):
        A = by_type_a.get(tname, [])
        S = by_type_s.get(tname, [])
        if not A or not S:
            continue
        candidates: list[tuple[float, int, int]] = []
        for ia, (_, a) in enumerate(A):
            pred = R @ np.array(a.center, dtype=float) + t
            for js, (_, s) in enumerate(S):
                d = float(np.linalg.norm(np.array(s.center, dtype=float) - pred))
                candidates.append((d, ia, js))
        candidates.sort(key=lambda x: x[0])
        used_a: set[int] = set()
        used_s: set[int] = set()
        for d, ia, js in candidates:
            if ia in used_a or js in used_s:
                continue
            used_a.add(ia); used_s.add(js)
            a = A[ia][1]; s = S[js][1]
            sim = _feature_similarity(a, s)
            if sim < min_similarity:
                continue
            out.append({
                'asset_feature': a,
                'scene_feature': s,
                'similarity': sim,
            })
    return out


def _build_fit_arrays(
    matches: list[dict],
    asset_pt_centroid: Vector3 | None,
    scene_pt_centroid: Vector3 | None,
    dir_factor: float,
):
    import numpy as np
    pt_a: list[Vector3] = []
    pt_s: list[Vector3] = []
    pt_w: list[float] = []
    dir_a: list[Vector3] = []
    dir_s: list[Vector3] = []
    dir_w: list[float] = []
    match_records: list[dict] = []
    for m in matches:
        a: GeometricFeature = m['asset_feature']
        s: GeometricFeature = m['scene_feature']
        w = float(a.confidence) * float(s.confidence) * float(m['similarity'])
        if w <= 0.0:
            continue
        d_a = _sign_align_direction(a.normal, a.center, asset_pt_centroid)
        d_s = _sign_align_direction(s.normal, s.center, scene_pt_centroid)
        pt_a.append(a.center); pt_s.append(s.center); pt_w.append(w)
        dir_a.append(d_a); dir_s.append(d_s); dir_w.append(w * dir_factor)
        match_records.append({
            'asset_feature_id': a.feature_id,
            'scene_feature_id': s.feature_id,
            'asset_feature_type': a.feature_type,
            'similarity': round(float(m['similarity']), 6),
            'weight': round(w, 6),
            'used_for_rotation': True,
            'used_for_translation': True,
        })
    PA = np.array(pt_a, dtype=float) if pt_a else np.zeros((0, 3))
    PS = np.array(pt_s, dtype=float) if pt_s else np.zeros((0, 3))
    WP = np.array(pt_w, dtype=float) if pt_w else np.zeros((0,))
    DA = np.array(dir_a, dtype=float) if dir_a else np.zeros((0, 3))
    DS = np.array(dir_s, dtype=float) if dir_s else np.zeros((0, 3))
    WD = np.array(dir_w, dtype=float) if dir_w else np.zeros((0,))
    return match_records, PA, PS, WP, DA, DS, WD


def fuse_feature_references(
    asset_features: list[GeometricFeature],
    scene_features: list[GeometricFeature],
    asset_pt_centroid: Vector3 | None,
    scene_pt_centroid: Vector3 | None,
    cfg: dict | None = None,
) -> dict | None:
    import numpy as np
    cfg = {**FEATURE_DETECTION_DEFAULTS, **(cfg or {})}
    dir_factor = cfg['direction_weight']
    min_sim = cfg['min_match_similarity']

    matches = match_features_between_asset_and_scene(
        asset_features, scene_features, min_sim,
    )
    if not matches:
        return None

    match_records, PA, PS, WP, DA, DS, WD = _build_fit_arrays(
        matches, asset_pt_centroid, scene_pt_centroid, dir_factor,
    )
    if not match_records:
        return None

    # Detect pairs of asset features that are interchangeable under similarity
    # (same type, same size-similarity to each scene candidate). When they
    # exist, greedy similarity matching picks one arbitrary assignment and a
    # symmetry-flipped R can then score lower than the correct R on that
    # specific pairing. We enumerate ambiguous swaps and add them as alternate
    # seed match lists, so a better pairing gets a chance to win.
    def _feat_key(f: GeometricFeature):
        if f.feature_type in ('circle', 'cylinder'):
            return (f.feature_type, round(f.radius or 0.0, 3))
        if f.feature_type == 'plane':
            sz = tuple(sorted(round(x, 2) for x in (f.size or (0.0, 0.0, 0.0))[:2]))
            return (f.feature_type, sz)
        return (f.feature_type,)

    ambiguous_groups_a: dict = defaultdict(list)
    for i, m_ in enumerate(matches):
        ambiguous_groups_a[_feat_key(m_['asset_feature'])].append(i)
    swap_alternates: list[list[dict]] = []
    ambiguous_indices: set[int] = set()
    for key, idxs in ambiguous_groups_a.items():
        if len(idxs) < 2:
            continue
        # Only swap when ALL tied asset features match tied scene features —
        # otherwise the similarity already disambiguated.
        scene_keys = {_feat_key(matches[i]['scene_feature']) for i in idxs}
        if len(scene_keys) != 1:
            continue
        ambiguous_indices.update(idxs)
        # Generate one swap per pair (enough to break 2-fold symmetries).
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                alt = list(matches)
                i, j = idxs[a], idxs[b]
                alt[i] = {**matches[i], 'scene_feature': matches[j]['scene_feature']}
                alt[j] = {**matches[j], 'scene_feature': matches[i]['scene_feature']}
                swap_alternates.append(alt)
    # Scoring uses only unambiguous pairs — pairs with a similarity-tied
    # alternate are compatible with either the correct R or a symmetric
    # flipped R, so they can't disambiguate. The remaining pairs (uniquely
    # identified by size) clearly favor the correct basin.
    score_mask = np.array(
        [i not in ambiguous_indices for i in range(len(match_records))],
        dtype=bool,
    )
    if not score_mask.any():
        # Fallback: no unambiguous pairs, use all (rare — only when every
        # feature has a similarity-tied counterpart).
        score_mask = np.ones(len(match_records), dtype=bool)

    def pair_key(ms):
        return tuple(sorted(
            (m['asset_feature'].feature_id, m['scene_feature'].feature_id)
            for m in ms
        ))

    def score_on_original(R_arr, t_arr):
        """Residual score against the original similarity-matched pairs."""
        res = _residuals_for_fit(R_arr, t_arr, PA, PS, DA, DS)
        denom_p = float(WP.sum()) or 1.0
        denom_d = float(WD.sum()) or 1.0
        sq_p = sum(WP[i] * res[i][0] ** 2 for i in range(len(res)))
        sq_d = sum(WD[i] * res[i][1] ** 2 for i in range(len(res)))
        return (sq_p / denom_p) + (sq_d / denom_d) * 10_000.0

    def build_candidate(R_arr, t_arr, current_matches):
        mr_, PA_, PS_, WP_, DA_, DS_, WD_ = _build_fit_arrays(
            current_matches, asset_pt_centroid, scene_pt_centroid, dir_factor,
        )
        if not mr_:
            return None
        p_bar_ = PA_.mean(axis=0); q_bar_ = PS_.mean(axis=0)
        return {
            'R': R_arr, 't': t_arr, 'p_bar': p_bar_, 'q_bar': q_bar_,
            'matches': current_matches, 'match_records': mr_,
            'PA': PA_, 'PS': PS_, 'WP': WP_,
            'DA': DA_, 'DS': DS_, 'WD': WD_,
            'score': score_on_original(R_arr, t_arr),
        }

    def run_icp(init_R, init_t, seed_matches):
        """Iterate ICP from a seed and return *both* the seed and the
        ICP-converged fit as candidates. Including the raw seed guards
        against ICP drifting off a good fit when the initial pairing was
        already correct: e.g., silica column side planes uniquely paired by
        area — re-pairing by distance crosses them under symmetric rims."""
        out = []
        c0 = build_candidate(init_R, init_t, seed_matches)
        if c0 is not None:
            out.append(c0)
        R = init_R; t = init_t
        current = seed_matches
        prev = pair_key(current)
        for _ in range(5):
            rem = _rematch_by_predicted_position(
                asset_features, scene_features, R, t, min_sim,
            )
            if not rem:
                break
            k = pair_key(rem)
            mr, pa, ps, wp, da, ds, wd = _build_fit_arrays(
                rem, asset_pt_centroid, scene_pt_centroid, dir_factor,
            )
            if not mr:
                break
            f = _kabsch_fit(pa, ps, wp, da, ds, wd)
            if f is None:
                break
            R, t, _, _ = f
            current = rem
            if k == prev:
                break
            prev = k
        mr_final, PA_f, PS_f, WP_f, DA_f, DS_f, WD_f = _build_fit_arrays(
            current, asset_pt_centroid, scene_pt_centroid, dir_factor,
        )
        if mr_final:
            f_final = _kabsch_fit(PA_f, PS_f, WP_f, DA_f, DS_f, WD_f)
            if f_final is not None:
                R_f, t_f, _, _ = f_final
                c1 = build_candidate(R_f, t_f, current)
                if c1 is not None:
                    out.append(c1)
        return out

    # Seed A: standard similarity-greedy Kabsch.
    fit_a = _kabsch_fit(PA, PS, WP, DA, DS, WD)
    # Seed B: direction-only Kabsch (zero position weights). Unique-normal
    # features (side planes) drive rotation without being fought by many
    # near-duplicate rim positions. Silica columns hit this: their r=19.15
    # top/bottom rim circles are easy to cross-pair, but the two tall side
    # planes have distinct ±Y normals that uniquely fix the rotation.
    WP0 = np.zeros_like(WP)
    fit_b = _kabsch_fit(PA, PS, WP0, DA, DS, WD) if len(DA) else None
    # Seed C: 24 axis-aligned rotation hypotheses as ICP seeds. Some scenes
    # place an asset in an orientation whose basin neither seed A nor seed
    # B lands in — sweeping axis-aligned rotations gives ICP a chance to
    # discover that basin. Also guards against sign-flip ambiguities in
    # PCA-based seeds. Translation is the centroid difference under each R.
    mean_a = PA.mean(axis=0) if len(PA) else np.zeros(3)
    mean_s = PS.mean(axis=0) if len(PS) else np.zeros(3)

    candidates = []
    for f in (fit_a, fit_b):
        if f is None:
            continue
        R0, t0, _, _ = f
        candidates.extend(run_icp(R0, t0, matches))
    for R_axis in _axis_aligned_rotations():
        R0 = np.array(R_axis, dtype=float)
        t0 = mean_s - R0 @ mean_a
        candidates.extend(run_icp(R0, t0, matches))
    # For each swap-alternate pairing, refit Kabsch from that alternate seed
    # list, then ICP-refine. Each alternate spawns two candidates (raw + ICP).
    for alt_matches in swap_alternates:
        _, pa_, ps_, wp_, da_, ds_, wd_ = _build_fit_arrays(
            alt_matches, asset_pt_centroid, scene_pt_centroid, dir_factor,
        )
        if len(pa_) == 0:
            continue
        fit_alt = _kabsch_fit(pa_, ps_, wp_, da_, ds_, wd_)
        if fit_alt is None:
            continue
        R_alt, t_alt, _, _ = fit_alt
        candidates.extend(run_icp(R_alt, t_alt, alt_matches))

    if not candidates:
        return None
    best = min(candidates, key=lambda c: c['score'])
    R = best['R']; t = best['t']
    p_bar = best['p_bar']; q_bar = best['q_bar']
    matches = best['matches']
    match_records = best['match_records']
    PA, PS, WP = best['PA'], best['PS'], best['WP']
    DA, DS, WD = best['DA'], best['DS'], best['WD']
    # Final re-pair under the winning R so the reported pair assignment
    # reflects the actual best correspondence (not an ambiguous
    # similarity-greedy pairing that may have crossed symmetric features).
    # Scoring already locked in the rotation — re-pair and refit *translation
    # only*, keeping R unchanged so symmetric cylindrical features can't drag
    # the rotation to a symmetry-related basin.
    final_rem = _rematch_by_predicted_position(
        asset_features, scene_features, R, t, min_sim,
    )
    if final_rem:
        mr_r, PA_r, PS_r, WP_r, DA_r, DS_r, WD_r = _build_fit_arrays(
            final_rem, asset_pt_centroid, scene_pt_centroid, dir_factor,
        )
        if mr_r:
            matches = final_rem
            match_records = mr_r
            PA, PS, WP = PA_r, PS_r, WP_r
            DA, DS, WD = DA_r, DS_r, WD_r
            wp_sum = float(WP.sum()) or 1.0
            p_bar = (WP[:, None] * PA).sum(axis=0) / wp_sum
            q_bar = (WP[:, None] * PS).sum(axis=0) / wp_sum
            t = q_bar - R @ p_bar

    # Iterative outlier rejection. We keep a parallel `active` mask instead of
    # mutating arrays, so the final match_records still describe every pair
    # (including rejected ones with used_for_rotation=False).
    active = [True] * len(match_records)
    iters = int(cfg.get('refine_iterations', 0))
    pt_sigma = float(cfg.get('refine_pt_sigma', 3.0))
    dir_sigma = float(cfg.get('refine_dir_sigma', 3.0))
    min_keep = int(cfg.get('refine_min_matches', 3))
    for _ in range(iters):
        res = _residuals_for_fit(R, t, PA, PS, DA, DS)
        act_idx = [i for i, a_ in enumerate(active) if a_]
        if len(act_idx) <= min_keep:
            break
        pt_vals = sorted(res[i][0] for i in act_idx)
        dir_vals = sorted(res[i][1] for i in act_idx)
        pt_med = pt_vals[len(pt_vals) // 2] or 0.0
        dir_med = dir_vals[len(dir_vals) // 2] or 0.0
        # Absolute floor prevents rejecting everything when median residuals
        # are already near zero (perfect fit).
        pt_thresh = max(pt_sigma * pt_med, 0.1)  # mm
        dir_thresh = max(dir_sigma * dir_med, math.radians(1.0))
        new_active = list(active)
        for i in act_idx:
            if res[i][0] > pt_thresh or res[i][1] > dir_thresh:
                new_active[i] = False
        kept = sum(1 for x in new_active if x)
        if kept < min_keep or kept == sum(1 for x in active if x):
            break
        active = new_active
        # Re-fit from the inliers only.
        idx = [i for i, a_ in enumerate(active) if a_]
        fit = _kabsch_fit(
            PA[idx], PS[idx], WP[idx], DA[idx], DS[idx], WD[idx],
        )
        if fit is None:
            break
        R, t, p_bar, q_bar = fit

    # Final residuals using the post-refit transform against every match,
    # but summary stats count only active inliers.
    final_res = _residuals_for_fit(R, t, PA, PS, DA, DS)
    sum_sq_pt = 0.0; max_pt = 0.0
    sum_sq_dir = 0.0; max_dir = 0.0
    n_active = 0
    for i, rec in enumerate(match_records):
        res_pt, res_d = final_res[i]
        rec['point_residual_mm'] = round(res_pt, 4)
        rec['direction_residual_rad'] = round(res_d, 5)
        rec['direction_residual_deg'] = round(math.degrees(res_d), 4)
        rec['used_for_rotation'] = bool(active[i])
        rec['used_for_translation'] = bool(active[i])
        if active[i]:
            n_active += 1
            sum_sq_pt += res_pt ** 2
            if res_pt > max_pt:
                max_pt = res_pt
            sum_sq_dir += res_d ** 2
            if res_d > max_dir:
                max_dir = res_d

    n_total = len(match_records)
    n_div = max(n_active, 1)
    residuals = {
        'point_rms_mm':          round((sum_sq_pt / n_div) ** 0.5, 4),
        'direction_rms_rad':     round((sum_sq_dir / n_div) ** 0.5, 5),
        'direction_rms_deg':     round(math.degrees((sum_sq_dir / n_div) ** 0.5), 4),
        'max_point_mm':          round(max_pt, 4),
        'max_direction_rad':     round(max_dir, 5),
        'max_direction_deg':     round(math.degrees(max_dir), 4),
        'n_used':                n_active,
        'n_total':               n_total,
        'n_rejected':            n_total - n_active,
    }

    return {
        'rotation_matrix':  [[round(float(v), 8) for v in row] for row in R.tolist()],
        'translation':      [round(float(v), 6) for v in t.tolist()],
        'matches':          match_records,
        'residuals':        residuals,
        'asset_centroid':   vector_round(tuple(float(v) for v in p_bar.tolist())),  # type: ignore[arg-type]
        'scene_centroid':   vector_round(tuple(float(v) for v in q_bar.tolist())),  # type: ignore[arg-type]
    }


def build_fused_debug_frame(
    features: list[GeometricFeature],
    point_centroid: Vector3 | None,
) -> dict | None:
    if not features:
        return None
    total_w = sum(f.confidence for f in features)
    if total_w <= 0:
        return None
    cx = sum(f.center[0] * f.confidence for f in features) / total_w
    cy = sum(f.center[1] * f.confidence for f in features) / total_w
    cz = sum(f.center[2] * f.confidence for f in features) / total_w
    origin = (cx, cy, cz)
    zx = zy = zz = 0.0
    for f in features:
        n = _sign_align_direction(f.normal, f.center, point_centroid)
        zx += n[0] * f.confidence
        zy += n[1] * f.confidence
        zz += n[2] * f.confidence
    z_axis = vec_normalize((zx, zy, zz))
    yaw_x = resolve_yaw_direction(origin, z_axis, point_centroid)
    x_axis = vec_orthogonalize(yaw_x if yaw_x is not None else (1.0, 0.0, 0.0), z_axis)
    y_axis = vec_cross(z_axis, x_axis)
    return {
        'origin': vector_round(origin),
        'x_axis': vector_round(x_axis),
        'y_axis': vector_round(y_axis),
        'z_axis': vector_round(z_axis),
    }


def position_record(model: StepModel, occ: Occurrence, root_center: Vector3 | None) -> dict:
    child_bbox = product_bbox(model, occ.child_pd)
    parent_bbox = product_bbox(model, occ.parent_pd)
    child_center = bbox_center(child_bbox)
    parent_center = bbox_center(parent_bbox)
    rel_parent = (
        vector_sub(child_center, parent_center)
        if child_center is not None and parent_center is not None
        else None
    )
    rel_root = (
        vector_sub(child_center, root_center)
        if child_center is not None and root_center is not None
        else None
    )

    bottom_center: Vector3 | None = None
    if child_bbox is not None:
        lo, hi = child_bbox
        bottom_center = ((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, lo[2])

    return {
        'nauo_id': occ.nauo_id,
        'product_definition_id': occ.child_pd,
        'product_name': model.pd_to_name.get(occ.child_pd, occ.child_pd),
        'occurrence_name': occ.occurrence_name,
        'parent_product_definition_id': occ.parent_pd,
        'parent_product_name': model.pd_to_name.get(occ.parent_pd, occ.parent_pd),
        'representation_relationship_id': occ.rr_id,
        'transform_id': occ.transform_id,
        'transform_position': vector_round(occ.transform_position),
        'geometry_center': vector_round(child_center),
        'geometry_bottom_center': vector_round(bottom_center),
        'geometry_size': vector_round(bbox_size(child_bbox)),
        'relative_to_parent_center': vector_round(rel_parent),
        'relative_to_root_center': vector_round(rel_root),
    }


def write_csv(path: Path, records: list[dict]) -> None:
    fields = [
        'nauo_id',
        'product_definition_id',
        'product_name',
        'occurrence_name',
        'parent_product_definition_id',
        'parent_product_name',
        'transform_id',
        'transform_x',
        'transform_y',
        'transform_z',
        'center_x',
        'center_y',
        'center_z',
        'relative_parent_x',
        'relative_parent_y',
        'relative_parent_z',
        'relative_root_x',
        'relative_root_y',
        'relative_root_z',
    ]
    with path.open('w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = {
                'nauo_id': record['nauo_id'],
                'product_definition_id': record['product_definition_id'],
                'product_name': record['product_name'],
                'occurrence_name': record['occurrence_name'],
                'parent_product_definition_id': record['parent_product_definition_id'],
                'parent_product_name': record['parent_product_name'],
                'transform_id': record['transform_id'],
            }
            for prefix, key in [
                ('transform', 'transform_position'),
                ('center', 'geometry_center'),
                ('relative_parent', 'relative_to_parent_center'),
                ('relative_root', 'relative_to_root_center'),
            ]:
                vec = record.get(key) or [None, None, None]
                row[f'{prefix}_x'] = vec[0]
                row[f'{prefix}_y'] = vec[1]
                row[f'{prefix}_z'] = vec[2]
            writer.writerow(row)


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def choose_ui_port(host: str, requested_port: int) -> int:
    if requested_port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])

    for port in range(requested_port, requested_port + 100):
        if port_is_free(host, port):
            return port
    raise RuntimeError(f'No free UI port found from {requested_port} to {requested_port + 99}')


def wait_for_ui(url: str, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + '/api/session', timeout=0.5) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.25)
    return False


def launch_ui(
    assembly_step: Path,
    matches_json: Path,
    host: str,
    requested_port: int,
    open_browser: bool,
    wait_seconds: float,
) -> tuple[str, subprocess.Popen]:
    port = choose_ui_port(host, requested_port)
    url = f'http://{host}:{port}'
    app_path = SCRIPT_DIR / 'app.py'
    cmd = [
        sys.executable,
        str(app_path),
        '--step',
        str(assembly_step.resolve()),
        '--matches',
        str(matches_json.resolve()),
        '--host',
        host,
        '--port',
        str(port),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    if not wait_for_ui(url, wait_seconds):
        if proc.poll() is not None:
            raise RuntimeError(f'UI server exited before startup on {url}')
        raise RuntimeError(f'UI server did not respond within {wait_seconds:g}s at {url}')

    if open_browser:
        webbrowser.open(url)
    return url, proc


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Find component instances in an assembly STEP file and record relative positions.',
    )
    parser.add_argument('component_step', type=Path, nargs='?', help='STEP file containing the component to find')
    parser.add_argument('assembly_step', type=Path, nargs='?', help='STEP file that may contain many component instances')
    parser.add_argument('--name', help='Component product name to match inside the component STEP')
    parser.add_argument('--output', '-o', type=Path, default=Path('output/component_matches.json'))
    parser.add_argument('--csv', type=Path, help='Optional CSV output path')
    parser.add_argument(
        '--target',
        choices=['root', 'children', 'leaves', 'all'],
        default='root',
        help=(
            'Which product definitions from the component STEP to match when --name is omitted '
            '(default: root, with single-child root promotion enabled)'
        ),
    )
    parser.add_argument(
        '--no-promote-single-child-root',
        action='store_true',
        help='Keep the literal root target even when root contains only one child',
    )
    parser.add_argument(
        '--geometry-precision',
        type=float,
        default=0.01,
        help='Rounding precision for bounding-box geometry matching, in STEP units (default: 0.01)',
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Print parser and matcher diagnostics to stderr',
    )
    parser.add_argument(
        '--debug-limit',
        type=int,
        default=20,
        help='Maximum number of debug rows to print per section (default: 20)',
    )
    parser.add_argument(
        '--launch-ui',
        action='store_true',
        help='After writing matches, start the web viewer with the assembly and highlighted matches',
    )
    parser.add_argument(
        '--ui-host',
        default='127.0.0.1',
        help='Host for --launch-ui (default: 127.0.0.1)',
    )
    parser.add_argument(
        '--ui-port',
        type=int,
        default=8017,
        help='Starting port for --launch-ui; next free port is used if occupied, 0 chooses any free port',
    )
    parser.add_argument(
        '--ui-wait-seconds',
        type=float,
        default=20.0,
        help='Seconds to wait for launched UI server to respond (default: 20)',
    )
    parser.add_argument(
        '--no-browser',
        action='store_true',
        help='With --launch-ui, start the server but do not open a browser',
    )
    parser.add_argument(
        '--ui-only',
        nargs=2,
        metavar=('ASSEMBLY_STEP', 'MATCH_JSON'),
        type=Path,
        help='Open an existing match JSON in the web viewer without recomputing matches',
    )
    args = parser.parse_args()

    if args.geometry_precision <= 0:
        parser.error('--geometry-precision must be greater than zero')
    if args.debug_limit < 1:
        parser.error('--debug-limit must be at least 1')
    if args.ui_port < 0 or args.ui_port > 65535:
        parser.error('--ui-port must be between 0 and 65535')
    if args.ui_wait_seconds <= 0:
        parser.error('--ui-wait-seconds must be greater than zero')

    if args.ui_only:
        assembly_step, matches_json = args.ui_only
        try:
            url, _proc = launch_ui(
                assembly_step,
                matches_json,
                args.ui_host,
                args.ui_port,
                open_browser=not args.no_browser,
                wait_seconds=args.ui_wait_seconds,
            )
            print(f"Launched UI: {url}")
            if args.no_browser:
                print("Open that URL in your browser to inspect highlighted matches.")
            return 0
        except Exception as exc:
            print(f"UI launch failed: {exc}", file=sys.stderr)
            return 1

    if args.component_step is None or args.assembly_step is None:
        parser.error('component_step and assembly_step are required unless --ui-only is used')

    component = load_step_model(args.component_step)
    assembly = load_step_model(args.assembly_step)
    component_centering = load_companion_centering_transform(args.component_step)
    assembly_centering = load_companion_centering_transform(args.assembly_step)
    if args.debug:
        if component_centering:
            debug_print(args.debug, f'[debug] applying companion centering for component: {component_centering}')
        if assembly_centering:
            debug_print(args.debug, f'[debug] applying companion centering for assembly:  {assembly_centering}')
    if args.debug:
        debug_model_summary('component STEP', component, args.debug_limit)
        debug_model_summary('assembly STEP', assembly, args.debug_limit)

    promote_single_child_root = not args.no_promote_single_child_root
    component_targets = choose_component_targets(
        component,
        args.name,
        args.target,
        promote_single_child_root,
    )
    if args.debug and args.target == 'root' and promote_single_child_root:
        literal_roots = component.root_product_defs()
        promoted_roots = promoted_single_child_roots(component, literal_roots)
        for literal, promoted in zip(literal_roots, promoted_roots):
            if literal != promoted:
                debug_print(
                    args.debug,
                    (
                        f"[debug] promoted single-child root {literal} "
                        f"{component.pd_to_name.get(literal, literal)!r} -> {promoted} "
                        f"{component.pd_to_name.get(promoted, promoted)!r}"
                    ),
                )
    debug_print(args.debug, '\n[debug] selected component targets')
    for pd in component_targets[:args.debug_limit]:
        debug_print(
            args.debug,
            f'  {pd}: {component.pd_to_name.get(pd, pd)!r}, bbox={format_bbox(product_bbox(component, pd))}',
        )
    if len(component_targets) > args.debug_limit:
        debug_print(args.debug, f'  ... {len(component_targets) - args.debug_limit} more targets')

    matched_pds, reasons, name_matches = find_matching_product_defs(
        component,
        assembly,
        component_targets,
        args.geometry_precision,
        debug=args.debug,
        debug_limit=args.debug_limit,
    )

    root_boxes = [product_bbox(assembly, root) for root in assembly.root_product_defs()]
    root_center = bbox_center(bbox_union(root_boxes))
    records = [
        position_record(assembly, occ, root_center)
        for occ in assembly.occurrences
        if occ.child_pd in matched_pds
    ]
    records.sort(key=lambda r: (r['product_name'], r['nauo_id']))

    # Feature-based pose: match circle/cylinder/plane features between the
    # canonical asset and each scene instance, fuse into a rigid R|t.
    # Rotation comes from the fused fit; translation from the fit's t,
    # which aligns asset-centroid landmarks with their scene counterparts.
    # Falls back to identity + AABB centre when no matchable features exist.
    identity_R = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    feature_cfg = dict(FEATURE_DETECTION_DEFAULTS)
    asset_features: list[GeometricFeature] = []
    asset_centroid: Vector3 | None = None
    if component_targets:
        asset_pd = component_targets[0]
        asset_features = detect_geometric_features_for_pd_tx(
            component, asset_pd, 'asset', feature_cfg,
            world_transform=None,
        )
        asset_points = walk_pd_points_with_transforms(component, asset_pd)
        if asset_points:
            n = float(len(asset_points))
            asset_centroid = (
                sum(p[0] for p in asset_points) / n,
                sum(p[1] for p in asset_points) / n,
                sum(p[2] for p in asset_points) / n,
            )

    nauo_to_occ = {occ.nauo_id: occ for occ in assembly.occurrences}
    for record in records:
        record['rotation_matrix'] = identity_R
        record['pose_xyz'] = record['geometry_center']
        record['pose_rpy_rad'] = [0.0, 0.0, 0.0]
        record['pose_rpy_deg'] = [0.0, 0.0, 0.0]
        record['fusion_residuals'] = None
        record['matches_feature_pairs'] = []

        if not asset_features:
            continue
        occ = nauo_to_occ.get(record['nauo_id'])
        if occ is None:
            continue
        R_inst, t_inst = pd_to_root_transform(
            assembly, occ.child_pd, preferred_nauo=occ.nauo_id,
        )
        scene_prefix = f'scene:{occ.nauo_id}'
        scene_features = detect_geometric_features_for_pd_tx(
            assembly, occ.child_pd, scene_prefix, feature_cfg,
            world_transform=(R_inst, t_inst),
        )
        scene_points = walk_pd_points_with_transforms(assembly, occ.child_pd)
        scene_points_world = [
            vector_add(mat_apply(R_inst, p), t_inst) for p in scene_points
        ]
        scene_centroid: Vector3 | None = None
        if scene_points_world:
            n = float(len(scene_points_world))
            scene_centroid = (
                sum(p[0] for p in scene_points_world) / n,
                sum(p[1] for p in scene_points_world) / n,
                sum(p[2] for p in scene_points_world) / n,
            )
        fit = fuse_feature_references(
            asset_features, scene_features, asset_centroid, scene_centroid,
            feature_cfg,
        )
        if fit is None:
            continue
        R_fit = fit['rotation_matrix']
        t_fit = fit['translation']
        record['rotation_matrix'] = R_fit
        record['pose_xyz'] = [round(float(v), 6) for v in t_fit]
        rpy = rotation_to_rpy(R_fit)
        record['pose_rpy_rad'] = [round(float(v), 6) for v in rpy]
        record['pose_rpy_deg'] = [round(math.degrees(float(v)), 4) for v in rpy]
        record['fusion_residuals'] = fit['residuals']
        record['matches_feature_pairs'] = fit['matches']
        record['fusion_asset_centroid'] = fit['asset_centroid']
        record['fusion_scene_centroid'] = fit['scene_centroid']
    if args.debug:
        for record in records[:args.debug_limit]:
            debug_print(
                args.debug,
                f"  {record['nauo_id']}: xyz={record['pose_xyz']}",
            )
    if args.debug:
        debug_print(args.debug, '\n[debug] match result')
        debug_print(args.debug, f'  methods: {reasons}')
        debug_print(args.debug, f'  matched product definitions: {len(matched_pds)}')
        debug_print(args.debug, f'  matched instances: {len(records)}')
        for record in records[:args.debug_limit]:
            debug_print(
                args.debug,
                (
                    f"  {record['nauo_id']}: {record['product_name']!r}, "
                    f"center={record['geometry_center']}, "
                    f"rel_parent={record['relative_to_parent_center']}, "
                    f"transform={record['transform_position']}"
                ),
            )
        if len(records) > args.debug_limit:
            debug_print(args.debug, f'  ... {len(records) - args.debug_limit} more matched instances')

    result = {
        'component_step': str(args.component_step),
        'assembly_step': str(args.assembly_step),
        'match_methods': reasons,
        'component_targets': [
            {
                'product_definition_id': pd,
                'product_name': component.pd_to_name.get(pd, pd),
                'geometry_size': vector_round(bbox_size(product_bbox(component, pd))),
            }
            for pd in component_targets
        ],
        'name_matches': name_matches,
        'matched_product_definition_count': len(matched_pds),
        'matched_instance_count': len(records),
        'matches': records,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        write_csv(args.csv, records)

    print(f"Matched {len(records)} instance(s) across {len(matched_pds)} product definition(s).")
    print(f"Wrote JSON: {args.output}")
    if args.csv:
        print(f"Wrote CSV: {args.csv}")
    if args.launch_ui:
        try:
            url, _proc = launch_ui(
                args.assembly_step,
                args.output,
                args.ui_host,
                args.ui_port,
                open_browser=not args.no_browser,
                wait_seconds=args.ui_wait_seconds,
            )
            print(f"Launched UI: {url}")
            if args.no_browser:
                print("Open that URL in your browser to inspect highlighted matches.")
        except Exception as exc:
            print(f"UI launch failed: {exc}", file=sys.stderr)
            return 1
    if not records:
        print("No matches found. Try --name with the component product name or relax --geometry-precision.", file=sys.stderr)
    return 0 if records else 1


if __name__ == '__main__':
    raise SystemExit(main())
