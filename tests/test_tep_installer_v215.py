#!/usr/bin/env python3
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "bin" / "hb-tep-onboard").read_text(encoding="utf-8")
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
SERVICE = (ROOT / "systemd" / "hashburst-tep.service").read_text(encoding="utf-8")


class TepInstallerV215Tests(unittest.TestCase):
    def test_onboarding_is_fail_closed_and_aes_required(self):
        self.assertIn("AES-256-GCM", SCRIPT)
        self.assertIn("cryptography", SCRIPT)
        self.assertIn("app_ready", SCRIPT)
        self.assertIn('HB_TEP_RELAY_ENABLED "0"', SCRIPT)
        self.assertIn("refusing to replace existing HB_TEP_PEER_ID", SCRIPT)

    def test_onboarding_preserves_keys_and_uses_stable_blockchain_peer_id(self):
        self.assertIn("/var/lib/hashburst/tep", SCRIPT)
        self.assertNotIn("node_x25519.key", SCRIPT)
        self.assertIn("/api/health", SCRIPT)
        self.assertIn("HB_TEP_PEER_ID", SCRIPT)
        self.assertIn("TEP_PUBKEY", SCRIPT)
        self.assertIn("systemctl enable --now hashburst-node.service", SCRIPT)

    def test_nat_bootstrap_contains_only_public_rendezvous_identity(self):
        self.assertIn("64.31.4.9", SCRIPT)
        self.assertIn("12D3KooWCiH3B8E84UNsop5epp7vNXfC6oSg2iyB4wjyCm6a84ow", SCRIPT)
        self.assertIn("50506353bd0ac23aec8502e3d5ed6c018975a7c5ea6e22dc363df321d6ca8960", SCRIPT)
        self.assertIn("HB_TEP_TRUSTED_RENDEZVOUS", SCRIPT)
        self.assertIn("HB_TEP_RENDEZVOUS_PEERS", SCRIPT)
        self.assertNotIn("swarm.key", SCRIPT)
        self.assertNotIn("replication_token", SCRIPT)

    def test_tep_service_remains_local_for_status_ipc_and_hardened(self):
        self.assertIn("ExecStart=/usr/bin/python3 -m tep.hb_tep", SERVICE)
        self.assertIn("NoNewPrivileges=true", SERVICE)
        self.assertIn("ProtectSystem=strict", SERVICE)
        self.assertIn("ProtectHome=true", SERVICE)
        self.assertIn("RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", SERVICE)

    def test_installer_wires_onboarding_for_release(self):
        self.assertIn('need_file "$SCRIPT_DIR/bin/hb-tep-onboard"', INSTALL)
        self.assertIn('bash "$SCRIPT_DIR/bin/hb-tep-onboard" "$NODE_NAME" "$ROLE" "$STORAGE_ROLE"', INSTALL)
        self.assertIn('VERSION="2.1.5"', INSTALL)


if __name__ == "__main__":
    unittest.main()
