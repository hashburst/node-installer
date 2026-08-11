#!/usr/bin/env python3
"""Release contract checks for HashBurst Node Installer v2.1.4."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
checks = []

def ok(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)
    print("PASS", name)

install = (ROOT / "install.sh").read_text()
controller = (ROOT / "replication/hb_replication_controller_v214.py").read_text()
db = (ROOT / "replication/hb_replication_v214_db.py").read_text()
agent = (ROOT / "replication/hb_replica_agent_v214.py").read_text()
ctl_env = (ROOT / "config/replication-controller.env.example").read_text()
agent_env = (ROOT / "config/replica-agent.env.example").read_text()
ctl_unit = (ROOT / "systemd/hashburst-replication-controller.service").read_text()
agent_unit = (ROOT / "systemd/hashburst-replica-agent.service").read_text()
ci = (ROOT / ".github/workflows/ci.yml").read_text()

ok("installer_version_2_1_4", 'VERSION="2.1.4"' in install)
ok("controller_v214_packaged", (ROOT / "replication/hb_replication_controller_v214.py").is_file())
ok("db_v214_packaged", (ROOT / "replication/hb_replication_v214_db.py").is_file())
ok("agent_v214_packaged", (ROOT / "replication/hb_replica_agent_v214.py").is_file())
ok("hb_ipfs_adjacent_install", '/opt/hashburst/replication/hb_ipfs.py' in install)
ok("controller_unit_v214", 'hb_replication_controller_v214.py' in ctl_unit)
ok("agent_unit_v214", 'hb_replica_agent_v214.py' in agent_unit)
ok("controller_default_observe", 'HB_REPL_MODE=observe' in ctl_env)
ok("controller_local_bind_default", 'HB_REPL_BIND=127.0.0.1' in ctl_env)
ok("controller_port_8095", 'HB_REPL_PORT=8095' in ctl_env)
ok("unpin_controller_default_off", 'HB_REPL_UNPIN_ENABLED=0' in ctl_env)
ok("unpin_agent_default_off", 'HB_REPL_ALLOW_UNPIN=0' in agent_env)
ok("delete_grace_900", 'HB_REPL_DELETE_GRACE_SEC=900' in ctl_env)
ok("reconcile_interval_900", 'HB_REPL_RECONCILE_INTERVAL_SEC=900' in ctl_env)
ok("generation_fencing", 'generation=generation+1' in db and 'stale-or-referenced' in db)
ok("jit_unpin_authorization", 'authorize_unpin' in controller and 'authorize-unpin' in agent)
ok("authorized_unpin_blocks_reregister", 'authorized UNPIN in progress' in db)
ok("installer_does_not_enable_controller", 'enable --now hashburst-replication-controller' not in install)
ok("installer_does_not_enable_agent", 'enable --now hashburst-replica-agent' not in install)
ok("ci_runs_v214_lifecycle", 'tests.test_replication_v214' in ci)
ok("mining_port_contract_preserved", '8093' not in controller)
ok("storage_aggregator_port_contract_preserved", '8094' not in controller)
print(f"PASS v2.1.4 release regression ({len(checks)} checks)")
