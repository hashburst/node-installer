#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "bin" / "hb-tep-onboard").read_text(encoding="utf-8")
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
SERVICE = (ROOT / "systemd" / "hashburst-tep.service").read_text(encoding="utf-8")


def test_onboarding_is_fail_closed_and_aes_required():
    assert "AES-256-GCM" in SCRIPT
    assert "cryptography" in SCRIPT
    assert "app_ready" in SCRIPT
    assert "HB_TEP_RELAY_ENABLED \"0\"" in SCRIPT
    assert "refusing to replace existing HB_TEP_PEER_ID" in SCRIPT


def test_onboarding_preserves_keys_and_uses_stable_blockchain_peer_id():
    assert "/var/lib/hashburst/tep" in SCRIPT
    assert "node_x25519.key" not in SCRIPT  # helper never deletes/replaces key material
    assert "/api/health" in SCRIPT
    assert "HB_TEP_PEER_ID" in SCRIPT
    assert "TEP_PUBKEY" in SCRIPT
    assert "systemctl enable --now hashburst-node.service" in SCRIPT


def test_nat_bootstrap_contains_only_public_rendezvous_identity():
    assert "64.31.4.9" in SCRIPT
    assert "12D3KooWCiH3B8E84UNsop5epp7vNXfC6oSg2iyB4wjyCm6a84ow" in SCRIPT
    assert "50506353bd0ac23aec8502e3d5ed6c018975a7c5ea6e22dc363df321d6ca8960" in SCRIPT
    assert "HB_TEP_TRUSTED_RENDEZVOUS" in SCRIPT
    assert "HB_TEP_RENDEZVOUS_PEERS" in SCRIPT
    assert "swarm.key" not in SCRIPT
    assert "replication_token" not in SCRIPT


def test_tep_service_remains_local_for_status_ipc_and_hardened():
    assert "ExecStart=/usr/bin/python3 -m tep.hb_tep" in SERVICE
    assert "NoNewPrivileges=true" in SERVICE
    assert "ProtectSystem=strict" in SERVICE
    assert "ProtectHome=true" in SERVICE
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" in SERVICE


def test_installer_wiring_pending_until_release_cut():
    # This deliberately fails once the helper has not yet been wired into install.sh.
    # The v2.1.5 release cut must remove the old package-only behavior and invoke
    # the helper after systemd daemon-reload, before normal full-node health gates.
    assert 'need_file "$SCRIPT_DIR/bin/hb-tep-onboard"' in INSTALL
    assert 'bash "$SCRIPT_DIR/bin/hb-tep-onboard" "$NODE_NAME" "$ROLE" "$STORAGE_ROLE"' in INSTALL
    assert 'VERSION="2.1.5"' in INSTALL
