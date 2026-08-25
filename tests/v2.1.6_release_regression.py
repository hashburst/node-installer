#!/usr/bin/env python3
"""Final release contract checks for HashBurst Node Installer v2.1.6."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
checks = []


def ok(name, cond):
    if not cond:
        raise AssertionError(name)
    checks.append(name)
    print('PASS', name)


install = (ROOT / 'install.sh').read_text(encoding='utf-8')
onboard = (ROOT / 'bin/hb-tep-onboard').read_text(encoding='utf-8')
runtime = (ROOT / 'tep/hb_tep_runtime.py').read_text(encoding='utf-8')
sandbox = (ROOT / 'tests/v215_installer_sandbox.sh').read_text(encoding='utf-8')
v216_sandbox = (ROOT / 'tests/v216_installer_sandbox.sh').read_text(encoding='utf-8')
v215 = (ROOT / 'tests/v2.1.5_release_regression.py').read_text(encoding='utf-8')
ci = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
release = (ROOT / 'docs/RELEASE-v2.1.6.md').read_text(encoding='utf-8')
checklist = (ROOT / 'docs/V2.1.6-RELEASE-CHECKLIST.md').read_text(encoding='utf-8')

ok('installer_version_2_1_6', 'VERSION="2.1.6"' in install)
ok('v216_changelog_present', '## 2.1.6' in changelog)
ok('v216_release_notes_present', '# HashBurst Node Installer v2.1.6' in release)
ok('node7_field_gate_passed', 'Final field validation: PASS' in release)
ok('network_rollout_passed', 'Network rollout validation: PASS' in release)
ok('checklist_ready_for_merge', 'Status: READY FOR MERGE' in checklist)

ok('running_node_state_detected', 'systemctl is-active --quiet hashburst-node.service' in onboard)
ok('running_node_restart_path', 'systemctl restart hashburst-node.service' in onboard)
ok('fresh_node_enable_path', 'systemctl enable --now hashburst-node.service' in onboard)
ok('tep_pubkey_written_before_restart', onboard.index('set_env "$NODE_ENV" TEP_PUBKEY "$TEP_PUBKEY" replace') < onboard.index('systemctl restart hashburst-node.service'))
ok('bootstrap_written_before_restart', onboard.index('set_env "$NODE_ENV" BOOTSTRAP_PEERS "$BLOCKCHAIN_BOOTSTRAP" if-empty') < onboard.index('systemctl restart hashburst-node.service'))
ok('wallet_bootstrap_before_restart', onboard.index('bash "$WALLET_BOOTSTRAP"') < onboard.index('systemctl restart hashburst-node.service'))

ok('v215_sandbox_identity_preservation_retained', all(x in sandbox for x in ['TEP_NODE_ID1', 'TEP_PEER_ID1', 'TEP_PUBKEY1', 'WALLET_SHA1', 'PASS_SHA1', 'SWARM_SHA']))
ok('v216_upgrade_idempotency_sandbox_present', 'V216_INSTALLER_SANDBOX_PASS' in v216_sandbox and '2.1.6' in v216_sandbox)
ok('v216_upgrade_idempotency_reuses_proven_sandbox', 'v215_installer_sandbox.sh' in v216_sandbox)

ok('v216_enriches_identity_from_api_nodes', '/api/nodes' in runtime and 'tep_pubkey' in runtime and '_ensure_peer_identity' in runtime)
legacy_superset = (
    'restored registered peer' in runtime
    and '_install_registry_reconciliation' in runtime
    and '_merge_registered_nodes' in runtime
)
atomic_superset = all(x in runtime for x in [
    '_install_registry_reconciliation',
    '_tep_peer_snapshot',
    '_authoritative_nodes',
    '_peer_from_registry_records',
    'set(tep_by_id) | set(nodes_by_id)',
    'self.peers._peers = fresh',
])
ok('v216_restores_registered_peer_superset', legacy_superset or atomic_superset)
ok('v216_atomic_reconciliation_preserves_superset', legacy_superset or atomic_superset)
ok('v216_preserves_nat_coordinates', 'never overwrite NAT coordinates' in runtime or 'observed NAT endpoint' in runtime or 'authenticated observed NAT endpoints' in runtime)
ok('v216_heartbeat_fail_closed', 'registered peer identity is incomplete' in runtime and 'return self.crypto.hmac_key' not in runtime)
ok('v216_distinguishes_missing_peer_key', 'Heartbeat auth missing peer key' in runtime)
ok('v216_distinguishes_key_derivation_failure', 'Heartbeat auth key derivation failed' in runtime)
ok('v216_distinguishes_gcm_failure', 'Heartbeat AES-GCM auth failed' in runtime)

ok('v215_contract_retained', 'installer_version_not_older_than_2_1_5' in v215)
ok('ci_retains_v215_release_contract', 'v2.1.5 release contract' in ci)
ok('ci_adds_v216_identity_gate', 'v2.1.6 TEP identity enrichment' in ci)
ok('ci_adds_v216_installer_sandbox', 'v2.1.6 installer upgrade/idempotency sandbox' in ci)
ok('ci_adds_v216_release_contract', 'v2.1.6 release contract' in ci)

ok('replication_still_not_auto_enabled', 'enable --now hashburst-replication-controller' not in install and 'enable --now hashburst-replica-agent' not in install)
ok('release_notes_keep_wire_format_unchanged', 'heartbeat wire format' in release)

print(json.dumps({'ok': True, 'checks': len(checks), 'version': '2.1.6'}, indent=2))
