#!/usr/bin/env python3
"""Upgrade an existing HashBurst explorer storage panel to schema v2.1.2.

The patch is intentionally narrow and idempotent. It does not introduce inline
style attributes or JavaScript style-property assignments, preserving strict
nonce-based Content-Security-Policy deployments.
"""
from __future__ import annotations
import argparse
import re
from pathlib import Path


def _card_bounds(text: str, value_id: str) -> tuple[int, int] | None:
    marker = f'id="{value_id}"'
    pos = text.find(marker)
    if pos < 0:
        return None
    start = text.rfind('<div class="stat-card">', 0, pos)
    if start < 0:
        raise ValueError(f"stat-card start not found for: {value_id}")
    tag_re = re.compile(r'<div\b[^>]*>|</div>')
    depth = 0
    for m in tag_re.finditer(text, start):
        if m.group(0).startswith('</div'):
            depth -= 1
            if depth == 0:
                return start, m.end()
        else:
            depth += 1
    raise ValueError(f"stat-card end not found for: {value_id}")


def _replace_card(text: str, value_id: str, new_id: str, new_label: str, new_subtitle: str | None = None) -> str:
    bounds = _card_bounds(text, value_id)
    if bounds is None:
        if f'id="{new_id}"' in text:
            return text
        raise ValueError(f"storage card not found: {value_id}")
    start, end = bounds
    card = text[start:end]
    card = re.sub(r'(<div class="stat-label">).*?(</div>)', rf'\1{new_label}\2', card, count=1, flags=re.S)
    card = card.replace(f'id="{value_id}"', f'id="{new_id}"', 1)
    if new_subtitle is not None and re.search(r'<div class="stat-sub">.*?</div>', card, flags=re.S):
        card = re.sub(r'(<div class="stat-sub">).*?(</div>)', rf'\1{new_subtitle}\2', card, count=1, flags=re.S)
    return text[:start] + card + text[end:]


def patch_text(text: str) -> str:
    out = text
    out = _replace_card(out, 'st-ipfs', 'st-best-effort', 'Best-effort Capacity', 'Best-effort replica capacity')
    out = _replace_card(out, 'st-total', 'st-committable', 'Committable Capacity', 'Sovereign storage pool')

    replacements = (
        ("$('st-ipfs').textContent = s.ipfs_private_peers ?? '—';",
         "$('st-best-effort').textContent = fmtGB(s.capacity_best_effort_gb);"),
        ("$('st-total').textContent = fmtGB(s.capacity_total_gb);",
         "$('st-committable').textContent = fmtGB(s.capacity_committable_gb);"),
    )
    for old, new in replacements:
        if new in out:
            continue
        if old not in out:
            raise ValueError(f"expected explorer JavaScript marker not found: {old}")
        out = out.replace(old, new, 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('input', type=Path)
    ap.add_argument('-o', '--output', type=Path)
    args = ap.parse_args()
    out = patch_text(args.input.read_text())
    dest = args.output or args.input
    dest.write_text(out)
    print(dest)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
