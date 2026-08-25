#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

if len(sys.argv) != 3:
    raise SystemExit(f"usage: {sys.argv[0]} MASTER_CONFIG_JSON OUTPUT_JSON")

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
with src.open() as f:
    cfg = json.load(f)
if not isinstance(cfg, dict):
    raise SystemExit("master config must be a JSON object")

required = {"network", "tep_host", "tep_port", "api_port", "coins", "segmentation"}
missing = sorted(required - set(cfg))
if missing:
    raise SystemExit("missing required config keys: " + ",".join(missing))

# DR1 must coexist with neurallity-seg-worker on 10.0.0.2:9000.
# Binding the HashBurst status API specifically to loopback avoids that collision.
cfg["api_host"] = "127.0.0.1"

forbidden = ("85.233.199.35", "77.90.188.153")
def walk(value, path="$"):
    if isinstance(value, dict):
        for k, v in value.items():
            walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            walk(v, f"{path}[{i}]")
    elif isinstance(value, str):
        for token in forbidden:
            if token in value:
                raise SystemExit(f"candidate physical address found at {path}; inspect config before deployment")
walk(cfg)

payload = json.dumps(cfg, indent=2, sort_keys=True) + "\n"
tmp = dst.with_suffix(dst.suffix + ".tmp")
tmp.write_text(payload)
os.chmod(tmp, 0o600)
os.replace(tmp, dst)
print(dst)
