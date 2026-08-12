from __future__ import annotations
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / 'tep' / 'hb_tep.py'
SERVICE = ROOT / 'systemd' / 'hashburst-tep.service'
ENV_EXAMPLE = ROOT / 'config' / 'hashburst-tep.env.example'
INSTALLER = ROOT / 'install.sh'

class Step7CPackagingTests(unittest.TestCase):
    def test_canonical_daemon_exists_and_compiles(self):
        self.assertTrue(CANON.is_file()); ast.parse(CANON.read_text())

    def test_canonical_packet_types_are_frozen(self):
        tree = ast.parse(CANON.read_text()); found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.startswith('PKT_'):
                        found[target.id] = node.value.value
        self.assertEqual(found['PKT_HEARTBEAT'], 0x01)
        self.assertEqual(found['PKT_APP_REQUEST'], 0x20)
        self.assertEqual(found['PKT_APP_RESPONSE'], 0x21)
        self.assertEqual(found['PKT_APP_ERROR'], 0x22)
        self.assertEqual(found['PKT_RELAY_REQUEST'], 0x23)
        self.assertEqual(found['PKT_RELAY_RESPONSE'], 0x24)

    def test_systemd_service_is_hardened_and_uses_module_layout(self):
        s = SERVICE.read_text()
        for token in ['WorkingDirectory=/opt/hashburst-tep', 'ExecStart=/usr/bin/python3 -m tep.hb_tep', 'EnvironmentFile=-/etc/hashburst/hashburst-tep.env', 'NoNewPrivileges=true', 'ProtectSystem=strict', 'ReadWritePaths=/var/lib/hashburst /var/log/hashburst']:
            self.assertIn(token, s)
        for token in ['8093', '8094', '8095', '5011']:
            self.assertNotIn(token, s)

    def test_installer_packages_but_never_enables_tep(self):
        s = INSTALLER.read_text(); self.assertIn('/opt/hashburst-tep/tep', s); self.assertIn('config/hashburst-tep.env.example', s)
        for token in ['systemctl enable --now hashburst-tep', 'systemctl start hashburst-tep', 'systemctl restart hashburst-tep']:
            self.assertNotIn(token, s)

    def test_env_defaults_are_fail_closed(self):
        s = ENV_EXAMPLE.read_text(); self.assertIn('HB_TEP_RELAY_ENABLED=0', s); self.assertIn('HB_TEP_PEER_ID=', s); self.assertIn('HB_TEP_RENDEZVOUS_PEERS=', s)

    def test_canonical_supports_systemd_environment(self):
        s = CANON.read_text()
        for token in ['HB_TEP_NODE_ID','HB_TEP_PEER_ID','HB_TEP_LISTEN','HB_TEP_RPC_PORT','HB_TEP_LOG_LEVEL','HB_TEP_RELAY_CLIENTS','HB_TEP_TRUSTED_RENDEZVOUS','HB_TEP_RENDEZVOUS_PEERS']:
            self.assertIn(token, s)

    def test_app_fails_closed_without_crypto_identity(self):
        s = CANON.read_text(); self.assertIn('if not self.app_ready', s); self.assertIn('HB-TEP-APP/1 is not ready', s)

if __name__ == '__main__': unittest.main()
