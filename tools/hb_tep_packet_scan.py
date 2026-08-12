#!/usr/bin/env python3
"""Scan a real hb_tep.py for integer PKT_* constants without executing it."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

from tep.hb_tep_wire import allocate_packet_types


def scan_packet_constants(path: str | Path) -> dict[str, int]:
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    found: dict[str, int] = {}
    for node in tree.body:
        names: list[str] = []
        value_node = None
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            value_node = node.value
        if value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except Exception:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        for name in names:
            if name.startswith("PKT_"):
                found[name] = value
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("hb_tep_py")
    args = parser.parse_args()
    found = scan_packet_constants(args.hb_tep_py)
    proposal = allocate_packet_types(found.values()).as_dict()
    print(json.dumps({"occupied": found, "proposed": proposal}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
