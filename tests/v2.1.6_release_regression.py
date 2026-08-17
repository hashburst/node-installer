#!/usr/bin/env python3
"""Release-preparation contract checks for HashBurst Node Installer v2.1.6."""
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
v215 = (ROOT / 'tests/v2.1.5_release_regression.py').read_text(encoding='utf-8')
ci = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
release = (ROOT / 'docs/RELEASE-v2.1.6.md').read_text(encoding='utf-8')

ok('prep_keeps_installer_at_2_1_5', 'VERSION="2.1.5"' in install)
ok('v216_unreleased_changelog', '## 2.1.6 - Unreleased' in changelog)
ok('v216_release_notes_present', 'Status: release preparation' in release)
ok('node7_field_gate_documented', 'node-7' in release and 'Release is blocked' in release)
ok('version_promotion_is_gated', 'Only after the final `node-7` field gate passes' in release)

ok('running_node_state_detected', 'systemctl is-active --quiet hashburst-node.service' in onboard)
ok('running_node_restart_path', 'systemctl restart hashburst-node.service' in onboard)
ok('fresh_node_enable_path', 'systemctl enable --now hashburst-node.service' in onboard)
ok('tep_pubkey_written_before_restart', onboard.index('set_env "$NODE_ENV" TEP_PUBKEY "$TEP_PUBKEY" replace') < onboard.index('systemctl restart hashburst-node.service'))
ok('bootstrap_written_before_restart', onboard.index('set_env "$NODE_ENV" BOOTSTRAP_PEERS "$BLOCKCHAIN_BOOTSTRAP" if-empty') < onboard.index('systemctl restart hashburst-node.service'))
ok('wallet_bootstrap_before_restart', onboard.index('bash "$WALLET_BOOTSTRAP"') < onboard.index('systemctl restart hashburst-node.service'))

ok('sandbox_tracks_node_state', 'NODE_STATE="/tmp/hb-v215-node-active"' in sandbox)
ok('sandbox_fresh_enable_assertion', "grep -q '^enable --now hashburst-node.service$' \"$SYS_LOG\"" in sandbox)
ok('sandbox_fresh_no_restart_assertion', "! grep -q '^restart hashburst-node.service$' \"$SYS_LOG\"" in sandbox)
ok('sandbox_reinstall_restart_assertion', "grep -q '^restart hashburst-node.service$' \"$SYS_LOG\"" in sandbox)
ok('sandbox_identity_preservation', all(x in sandbox for x in ['TEP_NODE_ID1', 'TEP_PEER_ID1', 'TEP_PUBKEY1', 'WALLET_SHA1', 'PASS_SHA1', 'SWARM_SHA']))

ok('v216_enriches_identity_from_api_nodes', '/api/nodes' in runtime and 'tep_pubkey' in runtime and '_ensure_peer_identity' in runtime)
ok('v216_preserves_nat_coordinates_on_enrichment', 'never overwrite NAT coordinates' in runtime)
ok('v216_heartbeat_fail_closed', 'registered peer identity is incomplete' in runtime and 'return self.crypto.hmac_key' not in runtime)
ok('v216_distinguishes_missing_peer_key', 'Heartbeat auth missing peer key' in runtime)
ok('v216_distinguishes_key_derivation_failure', 'Heartbeat auth key derivation failed' in runtime)
ok('v216_distinguishes_gcm_failure', 'Heartbeat AES-GCM auth failed' in runtime)

ok('v215_contract_retained', 'installer_version_2_1_5' in v215)
ok('ci_retains_v215_release_contract', 'v2.1.5 release contract' in ci)
ok('ci_adds_v216_identity_gate', 'v2.1.6 TEP identity enrichment' in ci)
ok('ci_adds_v216_release_prep_contract', 'v2.1.6 release preparation contract' in ci)

ok('replication_still_not_auto_enabled', 'enable --now hashburst-replication-controller' not in install and 'enable --now hashburst-replica-agent' not in install)
ok('release_notes_keep_wire_format_unchanged', 'heartbeat wire format' in release)

print(json.dumps({'ok': True, 'checks': len(checks), 'version': '2.1.6-prep'}, indent=2))
