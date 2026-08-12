#!/usr/bin/env python3
from __future__ import annotations
import ast, hashlib, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = '1a1e0554c001020c80261b638e57bf5fe072da574967264dd2e0dbef91b61e24'
EXPECTED_TYPES = {
    'PKT_HEARTBEAT': 0x01,
    'PKT_APP_REQUEST': 0x20,
    'PKT_APP_RESPONSE': 0x21,
    'PKT_APP_ERROR': 0x22,
    'PKT_RELAY_REQUEST': 0x23,
    'PKT_RELAY_RESPONSE': 0x24,
}
REQUIRED = [
    'tep/hb_tep_app.py', 'tep/hb_tep_client.py', 'tep/hb_tep_services.py',
    'tep/hb_tep_relay.py', 'tep/hb_tep_wire.py', 'patched/hb_tep.py',
    'tools/hb_tep_packet_scan.py', 'tools/tep_staging_network_test.py',
    'docs/TEP_APP_PROTOCOL.md', 'docs/TEP_NAT_DESIGN.md',
    'docs/TEP_SECURITY_MODEL.md', 'docs/TEP_ROLLBACK.md',
    '.github/workflows/ci.yml', 'baseline/hb_tep-production-current.py',
    'tests/test_tep_ipc_step7a.py', 'docs/TEP_LOCAL_IPC.md',
    'tests/test_tep_aggregator_ipc_step7b.py', 'docs/TEP_AGGREGATOR_WIRING.md',
]

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def packet_types(path: Path) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, int):
                for t in targets:
                    if isinstance(t, ast.Name) and t.id.startswith('PKT_'):
                        out[t.id] = value.value
    return out

def must(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)

for rel in REQUIRED:
    must((ROOT / rel).is_file(), f'missing release file: {rel}')

must(sha256(ROOT/'baseline/hb_tep-production-current.py') == BASELINE_SHA,
     'production baseline source SHA256 mismatch')

pt = packet_types(ROOT/'patched/hb_tep.py')
for name, value in EXPECTED_TYPES.items():
    must(pt.get(name) == value, f'{name} must be 0x{value:02x}')
must(len(set(EXPECTED_TYPES.values())) == len(EXPECTED_TYPES), 'packet type collision')

patched = (ROOT/'patched/hb_tep.py').read_text(encoding='utf-8')
for forbidden in ['subprocess', 'os.system', 'shell=True', '127.0.0.1:5011']:
    must(forbidden not in patched, f'forbidden construct in patched daemon: {forbidden}')

for required_text in [
    "PKT_APP_REQUEST     = 0x20", "PKT_RELAY_RESPONSE  = 0x24",
    "'app_ready': self.app_ready", "'relay': bool(self.app_ready and self._relay_enabled)",
    "ThreadingHTTPServer(('127.0.0.1', self.status_port)",
    "self.path != '/app/storage-summary'",
    "IPC_MAX_REQUEST_BYTES = 8 * 1024", "IPC_MAX_RESPONSE_BYTES = 32 * 1024",
    "IPC_TIMEOUT_SEC = 3.0", "IPC_DIRECT_TIMEOUT_SEC = 1.2",
]:
    must(required_text in patched, f'missing daemon contract: {required_text}')

services = (ROOT/'tep/hb_tep_services.py').read_text(encoding='utf-8')
must('storage.summary' in services, 'storage.summary missing')
for forbidden in ['http.proxy', 'files.delete', 'shell=True', 'subprocess']:
    must(forbidden not in services, f'forbidden service capability: {forbidden}')

agg = (ROOT/'aggregator/hb_aggregator.py').read_text(encoding='utf-8')
must('transport' in agg and 'tep_peer_id' in agg, 'aggregator is not transport-aware')
must("role in {'primary', 'secondary'}" in agg or 'role in {"primary", "secondary"}' in agg,
     'committable role invariant missing')

adapter = (ROOT/'aggregator/hb_tep_adapter.py').read_text(encoding='utf-8')
for required_text in ['IPC_HOST = "127.0.0.1"', 'IPC_DEFAULT_PORT = 47778',
                      'IPC_PATH = "/app/storage-summary"', 'method="POST"']:
    must(required_text in adapter, f'missing aggregator IPC contract: {required_text}')
for forbidden in ['http://0.0.0.0', 'https://', 'subprocess', 'shell=True']:
    must(forbidden not in adapter, f'forbidden aggregator IPC capability: {forbidden}')

ci = (ROOT/'.github/workflows/ci.yml').read_text(encoding='utf-8')
for token in ['v2.1.4 lifecycle safety', 'v2.1.4 release contract',
              'HB-TEP-APP unit and daemon integration', 'HB-TEP-APP release contract',
              'tests.test_tep_ipc_step7a', 'tests.test_tep_aggregator_ipc_step7b']:
    must(token in ci, f'CI gate missing: {token}')

print(json.dumps({
    'ok': True,
    'baseline_sha256': BASELINE_SHA,
    'patched_sha256': sha256(ROOT/'patched/hb_tep.py'),
    'packet_types': {k: pt[k] for k in EXPECTED_TYPES},
    'required_files': len(REQUIRED),
}, indent=2, sort_keys=True))
