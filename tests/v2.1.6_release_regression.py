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
sandbox = (ROOT / 'tests/v215_installer_sandbox.sh').read_text(encoding='utf-8')
v215 = (ROOT / 'tests/v2.1.5_release_regression.py').read_text(encoding='utf-8')
ci = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
release = (ROOT / 'docs/RELEASE-v2.1.6.md').read_text(encoding='utf-8')

# Preparation intentionally keeps the canonical installer at 2.1.5 until the
# node-7 field gate passes. This prevents an unvalidated release declaration.
ok('prep_keeps_installer_at_2_1_5', 'VERSION="2.1.5"' in install)
ok('v216_unreleased_changelog', '## 2.1.6 - Unreleased' in changelog)
ok('v216_release_notes_present', 'Status: release preparation' in release)
ok('node7_field_gate_documented', 'node-7' in release and 'Release is blocked' in release)
ok('version_promotion_is_gated', 'Only after the `node-7` field gate passes' in release)

# Runtime change inherited from PR #9.
ok('running_node_state_detected', 'systemctl is-active --quiet hashburst-node.service' in onboard)
ok('running_node_restart_path', 'systemctl restart hashburst-node.service' in onboard)
ok('fresh_node_enable_path', 'systemctl enable --now hashburst-node.service' in onboard)
ok('tep_pubkey_written_before_restart', onboard.index('set_env "$NODE_ENV" TEP_PUBKEY "$TEP_PUBKEY" replace') < onboard.index('systemctl restart hashburst-node.service'))
ok('bootstrap_written_before_restart', onboard.index('set_env "$NODE_ENV" BOOTSTRAP_PEERS "$BLOCKCHAIN_BOOTSTRAP" if-empty') < onboard.index('systemctl restart hashburst-node.service'))
ok('wallet_bootstrap_before_restart', onboard.index('bash "$WALLET_BOOTSTRAP"') < onboard.index('systemctl restart hashburst-node.service'))

# Functional sandbox coverage added on PR #9.
ok('sandbox_tracks_node_state', 'NODE_STATE="/tmp/hb-v215-node-active"' in sandbox)
ok('sandbox_fresh_enable_assertion', "grep -q '^enable --now hashburst-node.service$' \"$SYS_LOG\"" in sandbox)
ok('sandbox_fresh_no_restart_assertion', "! grep -q '^restart hashburst-node.service$' \"$SYS_LOG\"" in sandbox)
ok('sandbox_reinstall_restart_assertion', "grep -q '^restart hashburst-node.service$' \"$SYS_LOG\"" in sandbox)
ok('sandbox_identity_preservation', all(x in sandbox for x in ['TEP_NODE_ID1', 'TEP_PEER_ID1', 'TEP_PUBKEY1', 'WALLET_SHA1', 'PASS_SHA1', 'SWARM_SHA']))

# v2.1.5 contract must remain present and CI must execute both contracts.
ok('v215_contract_retained', 'installer_version_2_1_5' in v215)
ok('ci_retains_v215_release_contract', 'v2.1.5 release contract' in ci)
ok('ci_adds_v216_release_prep_contract', 'v2.1.6 release preparation contract' in ci)

# Scope remains narrow; no replication services are auto-enabled.
ok('replication_still_not_auto_enabled', 'enable --now hashburst-replication-controller' not in install and 'enable --now hashburst-replica-agent' not in install)
ok('release_notes_state_crypto_unchanged', 'AES-256-GCM/X25519 cryptography' in release and 'heartbeat wire format' in release)

print(json.dumps({'ok': True, 'checks': len(checks), 'version': '2.1.6-prep'}, indent=2))
