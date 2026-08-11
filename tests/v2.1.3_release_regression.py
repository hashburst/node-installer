#!/usr/bin/env python3
"""Release contract checks for HashBurst Node Installer v2.1.3 candidate."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
checks = []

def ok(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)
    print("PASS", name)

install = (ROOT / "install.sh").read_text()
controller = (ROOT / "replication/hb_replication_controller.py").read_text()
agent = (ROOT / "replication/hb_replica_agent.py").read_text()
ipfs = (ROOT / "hbfiles/hb_ipfs.py").read_text()
ctl_unit = (ROOT / "systemd/hashburst-replication-controller.service").read_text()
agent_unit = (ROOT / "systemd/hashburst-replica-agent.service").read_text()
files_unit = (ROOT / "systemd/hashburst-files.service").read_text()
hb_files = (ROOT / "hbfiles/hb_files.py").read_text()
ci = (ROOT / ".github/workflows/ci.yml").read_text()

ok("installer_version_2_1_3", 'VERSION="2.1.3"' in install)
ok("replication_controller_packaged", (ROOT / "replication/hb_replication_controller.py").is_file())
ok("replica_agent_packaged", (ROOT / "replication/hb_replica_agent.py").is_file())
ok("default_n_3", 'HB_REPL_N", "3"' in controller)
ok("default_m_2", 'HB_REPL_M", "2"' in controller)
ok("edge_grace_6h", '6 * 3600' in controller)
ok("controller_default_observe", 'HB_REPL_MODE", "observe"' in controller)
ok("controller_local_bind_default", 'HB_REPL_BIND", "127.0.0.1"' in controller)
ok("controller_port_8095", 'HB_REPL_PORT", "8095"' in controller)
ok("agent_kubo_localhost", 'http://127.0.0.1:5011' in agent)
ok("unpin_default_disabled", 'HB_REPL_ALLOW_UNPIN", "0"' in agent)
ok("ipfs_pin_primitive", 'def pin(self, cid: str)' in ipfs)
ok("ipfs_pin_verification", 'def is_pinned(self, cid: str)' in ipfs)
ok("installer_does_not_enable_controller", 'enable --now hashburst-replication-controller' not in install)
ok("installer_does_not_enable_agent", 'enable --now hashburst-replica-agent' not in install)
ok("controller_unit_no_missing_hashburst_user", 'User=hashburst' not in ctl_unit and 'Group=hashburst' not in ctl_unit)
ok("agent_unit_no_missing_hashburst_user", 'User=hashburst' not in agent_unit and 'Group=hashburst' not in agent_unit)
ok("ci_runs_replication_tests", 'tests.test_replication_repair' in ci and 'tests.test_replica_agent' in ci and 'tests.test_replication_hook' in ci)
ok("hbfiles_replication_hook_default_off", "HB_REPL_HOOK_ENABLED=0" in install and "REPL_HOOK_ENABLED" in hb_files)
ok("hbfiles_has_replication_client_path", "PYTHONPATH=/opt/hashburst/replication" in files_unit)
ok("mining_port_contract_preserved", '8093' not in controller)
ok("storage_aggregator_port_contract_preserved", '8094' not in controller)
print(f"PASS v2.1.3 release regression ({len(checks)} checks)")
