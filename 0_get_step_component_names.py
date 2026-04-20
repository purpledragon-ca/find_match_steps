"""
Extract unique component names from a STEP file.

Usage:
    python get_step_component_names.py <path_to_step_file> [options]

Options:
    --output FILE   Save names to FILE (default: component_names.txt)
    --filter TEXT   Filter component names by substring
    --sort          Sort names alphabetically (default: on)

Example:
    python get_step_component_names.py ../isaacsim_models/left_shelf.step
    python get_step_component_names.py ../isaacsim_models/left_shelf.step --output names.txt
    python get_step_component_names.py ../isaacsim_models/left_shelf.step --filter 过滤柱
"""

import re
import sys
import argparse
from collections import deque
from pathlib import Path


def decode_step_string(s):
    """Decode STEP \\X2\\...\\X0\\ Unicode escape sequences."""
    def replace_unicode(m):
        hex_str = m.group(1)
        try:
            chars = [chr(int(hex_str[i:i+4], 16)) for i in range(0, len(hex_str), 4)]
            return ''.join(chars)
        except Exception:
            return m.group(0)
    return re.sub(r'\\X2\\([0-9A-Fa-f]+)\\X0\\', replace_unicode, s)


def parse_records(content):
    """Parse all STEP entity records into a dict: entity_id -> body string."""
    records = {}
    for match in re.finditer(r'#(\d+)\s*=\s*(.+?);', content, re.DOTALL):
        num = int(match.group(1))
        body = match.group(2).replace('\n', ' ').strip()
        records[num] = body
    return records


def extract_component_names(step_file_path):
    """
    Parse a STEP file and return (order, counts, levels) where:
      - order is a list of unique component names in first-seen order
      - counts is a dict of name -> occurrence count
      - levels is a dict of name -> minimum hierarchy depth (1 = root)
    """
    path = Path(step_file_path)
    if not path.exists():
        raise FileNotFoundError(f"STEP file not found: {step_file_path}")

    print(f"Reading: {path.name}", file=sys.stderr)
    content = path.read_text(encoding='utf-8', errors='replace')

    print("Parsing records...", file=sys.stderr)
    records = parse_records(content)
    print(f"  Total records: {len(records)}", file=sys.stderr)

    ref_pattern = re.compile(r'#(\d+)')

    # PRODUCT name chain: PRODUCT_DEFINITION -> PRODUCT_DEFINITION_FORMATION -> PRODUCT
    products = {}
    for num, body in records.items():
        if body.startswith('PRODUCT('):
            m = re.match(r"PRODUCT\s*\(\s*'([^']*)'\s*,\s*'([^']*)'", body)
            if m:
                products[num] = decode_step_string(m.group(1) or m.group(2))

    prod_form_to_prod = {}
    for num, body in records.items():
        if body.startswith('PRODUCT_DEFINITION_FORMATION'):
            m = re.match(
                r"PRODUCT_DEFINITION_FORMATION[A-Z_]*\s*\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*(#\d+)",
                body
            )
            if m:
                prod_form_to_prod[num] = int(m.group(1)[1:])

    prod_def_to_name = {}
    prod_defs = {n for n, b in records.items() if b.startswith('PRODUCT_DEFINITION(')}
    for num, body in records.items():
        if body.startswith('PRODUCT_DEFINITION('):
            for ref in [int(r) for r in ref_pattern.findall(body)]:
                if ref in prod_form_to_prod:
                    prod_id = prod_form_to_prod[ref]
                    prod_def_to_name[num] = products.get(prod_id, f'unknown_{prod_id}')
                    break

    # Build assembly hierarchy from NEXT_ASSEMBLY_USAGE_OCCURENCE records
    # Format: NEXT_ASSEMBLY_USAGE_OCCURENCE('id','name','desc',#parent,#child,...)
    children_of = {}   # parent prod_def id -> set of child prod_def ids
    parents_of = {}    # child prod_def id -> set of parent prod_def ids
    for num, body in records.items():
        if 'NEXT_ASSEMBLY_USAGE_OCCUR' in body[:35]:
            refs = [int(r) for r in ref_pattern.findall(body)]
            if len(refs) >= 2:
                parent_id, child_id = refs[0], refs[1]
                children_of.setdefault(parent_id, set()).add(child_id)
                parents_of.setdefault(child_id, set()).add(parent_id)

    # BFS from roots (prod_defs with no parent) to compute hierarchy level
    roots = prod_defs - set(parents_of.keys())
    prod_def_level = {}
    queue = deque()
    for root in roots:
        prod_def_level[root] = 0
        queue.append(root)
    while queue:
        pid = queue.popleft()
        for child in children_of.get(pid, []):
            if child not in prod_def_level:
                prod_def_level[child] = prod_def_level[pid] + 1
                queue.append(child)

    # For each name, take the minimum (shallowest) level across all its prod_defs
    name_level = {}
    for prod_def_id, name in prod_def_to_name.items():
        level = prod_def_level.get(prod_def_id, 0)
        if name not in name_level or level < name_level[name]:
            name_level[name] = level

    # Collect names with counts
    counts = {}
    order = []
    for name in prod_def_to_name.values():
        if name not in counts:
            order.append(name)
        counts[name] = counts.get(name, 0) + 1

    return order, counts, name_level


def main():
    parser = argparse.ArgumentParser(
        description='Extract unique component names from a STEP file.'
    )
    parser.add_argument('step_file', help='Path to the .step / .STEP file')
    parser.add_argument('--output', metavar='FILE', default='component_names.txt',
                        help='Save names to FILE (default: component_names.txt)')
    parser.add_argument('--filter', metavar='TEXT',
                        help='Only show components whose name contains TEXT')
    parser.add_argument('--no-sort', action='store_true',
                        help='Preserve original order instead of sorting alphabetically')
    args = parser.parse_args()

    names, counts, levels = extract_component_names(args.step_file)

    if args.filter:
        names = [n for n in names if args.filter in n]

    if not args.no_sort:
        # Primary: count descending; secondary: level ascending (shallow first)
        names.sort(key=lambda n: (-counts[n], levels.get(n, 0)))

    total_instances = sum(counts[n] for n in names)
    print(f"\nFound {len(names)} unique component names ({total_instances} total instances)\n", file=sys.stderr)

    out_path = Path(args.output)
    with out_path.open('w', encoding='utf-8') as f:
        f.write("name\tcount\tlevel\n")
        for name in names:
            f.write(f"{name}\t{counts[name]}\t{levels.get(name, 0)}\n")
    print(f"Saved to {out_path}", file=sys.stderr)

    # Print header
    print(f"{'#':<4} {'count':<8} {'level':<7} name")
    print("-" * 60)
    for i, name in enumerate(names, 1):
        print(f"{i:<4} {counts[name]:<8} {levels.get(name, 0):<7} {name}")


if __name__ == '__main__':
    main()
