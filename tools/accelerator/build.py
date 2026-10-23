#!/usr/bin/env python3
"""Build a source-only, reproducible cross-platform accelerator package."""
import hashlib
from pathlib import Path
import sys
import zipfile
root=Path(__file__).resolve().parent
out=Path(sys.argv[1]).resolve()
files=['accelerator.py','policy.example.json','README.md','tests/test_accelerator.py']
if out.exists():raise SystemExit('output exists')
checks=''.join(hashlib.sha256((root/n).read_bytes()).hexdigest()+'  '+n+'\n' for n in files)
with zipfile.ZipFile(out,'x',compression=zipfile.ZIP_DEFLATED) as z:
    for name in files+['SHA256SUMS']:
        info=zipfile.ZipInfo(name,date_time=(2026,10,23,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
        info.external_attr=0o100644<<16
        z.writestr(info,checks.encode() if name=='SHA256SUMS' else (root/name).read_bytes())
print(hashlib.sha256(out.read_bytes()).hexdigest()+'  '+out.name)
