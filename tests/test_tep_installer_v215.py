#!/usr/bin/env python3
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "bin" / "hb-tep-onboard").read_text(encoding="utf-8")
WALLET = (ROOT / "bin" / "hb-node-wallet-bootstrap").read_text(encoding="utf-8")
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
SERVICE = (ROOT / "systemd" / "hashburst-tep.service").read_text(encoding="utf-8")
RUNTIME = (ROOT / "tep" / "hb_tep_runtime.py").read_text(encoding="utf-8")


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

    def test_blockchain_wallet_and_signing_identity_bootstrap(self):
        self.assertIn('WALLET_BOOTSTRAP="$SCRIPT_DIR/hb-node-wallet-bootstrap"', SCRIPT)
        self.assertIn('bash "$WALLET_BOOTSTRAP"', SCRIPT)
        self.assertIn('REWARD_ADDRESS', WALLET)
        self.assertIn('NODE_KEYSTORE', WALLET)
        self.assertIn('NODE_KEYSTORE_PASSWORD_FILE', WALLET)
        self.assertIn('/var/lib/hashburst/wallet', WALLET)
        self.assertIn('/etc/hashburst/wallet.pass', WALLET)
        self.assertIn('wallet new --dir', WALLET)
        self.assertIn('multiple reward keystores exist', WALLET)
        self.assertIn('Reward/signing wallet preserved', WALLET)

    def test_blockchain_nodes_auto_join_canonical_p2p_network(self):
        self.assertIn('BLOCKCHAIN_BOOTSTRAP="/ip4/${RENDEZVOUS_IP}/tcp/30307/p2p/${RENDEZVOUS_PEER_ID}"', SCRIPT)
        self.assertIn('set_env "$NODE_ENV" BOOTSTRAP_PEERS "$BLOCKCHAIN_BOOTSTRAP" if-empty', SCRIPT)
        self.assertIn('if [ "$NODE_NAME" != "$RENDEZVOUS_NODE_ID" ]; then', SCRIPT)
        self.assertIn("NODE_REGISTRATION can propagate", SCRIPT)

    def test_blockchain_p2p_firewall_opens_before_node_start(self):
        firewall = "ufw allow 30307/tcp comment 'HashBurst blockchain P2P'"
        start = 'systemctl enable --now hashburst-node.service'
        self.assertIn(firewall, SCRIPT)
        self.assertLess(SCRIPT.index(firewall), SCRIPT.index(start))
        self.assertIn("ufw allow 47777/udp comment 'HashBurst TEP'", SCRIPT)

    def test_nat_bootstrap_contains_only_public_rendezvous_identity(self):
        self.assertIn("64.31.4.9", SCRIPT)
        self.assertIn("12D3KooWCiH3B8E84UNsop5epp7vNXfC6oSg2iyB4wjyCm6a84ow", SCRIPT)
        self.assertIn("50506353bd0ac23aec8502e3d5ed6c018975a7c5ea6e22dc363df321d6ca8960", SCRIPT)
        self.assertIn("HB_TEP_TRUSTED_RENDEZVOUS", SCRIPT)
        self.assertIn("HB_TEP_RENDEZVOUS_PEERS", SCRIPT)
        self.assertNotIn("swarm.key", SCRIPT)
        self.assertNotIn("replication_token", SCRIPT)

    def test_canonical_rendezvous_enables_only_local_self_failover(self):
        self.assertIn('if [ "$NODE_NAME" = "$RENDEZVOUS_NODE_ID" ]; then', SCRIPT)
        self.assertIn('HB_TEP_RELAY_ENABLED "1" replace', SCRIPT)
        self.assertIn('HB_TEP_RENDEZVOUS_PEERS "$PEER_ID" replace', SCRIPT)
        self.assertIn('rendezvous_peer_id != self.peer_id', RUNTIME)
        self.assertIn('env.service != "storage.summary"', RUNTIME)

    def test_ordinary_nodes_force_relay_off_and_canonical_rendezvous(self):
        self.assertGreaterEqual(SCRIPT.count('HB_TEP_RELAY_ENABLED "0" replace'), 2)
        self.assertGreaterEqual(SCRIPT.count('HB_TEP_TRUSTED_RENDEZVOUS "$RENDEZVOUS_PEER_ID" replace'), 2)
        self.assertGreaterEqual(SCRIPT.count('HB_TEP_RENDEZVOUS_PEERS "$RENDEZVOUS_PEER_ID" replace'), 2)
        self.assertNotIn('HB_TEP_RELAY_ENABLED "0" if-empty', SCRIPT)

    def test_stable_tep_node_id_replacement_is_refused(self):
        self.assertIn('EXISTING_NODE_ID="$(get_env "$TEP_ENV" HB_TEP_NODE_ID)"', SCRIPT)
        self.assertIn('refusing to replace existing HB_TEP_NODE_ID', SCRIPT)
        self.assertIn('set_env "$TEP_ENV" HB_TEP_NODE_ID "$NODE_NAME" replace', SCRIPT)

    def test_tep_service_remains_local_for_status_ipc_and_hardened(self):
        self.assertIn("ExecStart=/usr/bin/python3 -m tep.hb_tep_runtime", SERVICE)
        self.assertIn("NoNewPrivileges=true", SERVICE)
        self.assertIn("ProtectSystem=strict", SERVICE)
        self.assertIn("ProtectHome=true", SERVICE)
        self.assertIn("RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", SERVICE)

    def test_installer_wires_onboarding_for_release(self):
        self.assertIn('need_file "$SCRIPT_DIR/bin/hb-tep-onboard"', INSTALL)
        self.assertIn('need_file "$SCRIPT_DIR/tep/hb_tep_runtime.py"', INSTALL)
        self.assertIn('bash "$SCRIPT_DIR/bin/hb-tep-onboard" "$NODE_NAME" "$ROLE" "$STORAGE_ROLE"', INSTALL)
        self.assertIn('VERSION="2.1.5"', INSTALL)

    def test_reinstall_never_silently_replaces_swarm_key(self):
        self.assertIn('cmp -s /tmp/swarm.key /etc/hashburst/swarm.key', INSTALL)
        self.assertIn('refusing federation key replacement', INSTALL)
        self.assertIn('[ -f /tmp/swarm.key ] && [ ! -f /etc/hashburst/swarm.key ]', INSTALL)
        self.assertNotIn('if [ -f /tmp/swarm.key ]; then install -m 0600 /tmp/swarm.key /etc/hashburst/swarm.key; fi', INSTALL)


if __name__ == "__main__":
    unittest.main()
