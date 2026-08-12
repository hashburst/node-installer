#!/usr/bin/env python3
"""Release contract checks for HashBurst Node Installer v2.1.5."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
checks=[]

def ok(name, cond):
    if not cond:
        raise AssertionError(name)
    checks.append(name)
    print('PASS', name)

install=(ROOT/'install.sh').read_text(encoding='utf-8')
onboard=(ROOT/'bin/hb-tep-onboard').read_text(encoding='utf-8')
tep=(ROOT/'tep/hb_tep.py').read_text(encoding='utf-8')
runtime=(ROOT/'tep/hb_tep_runtime.py').read_text(encoding='utf-8')
service=(ROOT/'systemd/hashburst-tep.service').read_text(encoding='utf-8')
agg=(ROOT/'aggregator/hb_aggregator.py').read_text(encoding='utf-8')
adapter=(ROOT/'aggregator/hb_tep_adapter.py').read_text(encoding='utf-8')
ci=(ROOT/'.github/workflows/ci.yml').read_text(encoding='utf-8')

ok('installer_version_2_1_5', 'VERSION="2.1.5"' in install)
ok('onboard_helper_required', 'need_file "$SCRIPT_DIR/bin/hb-tep-onboard"' in install)
ok('onboard_helper_invoked', 'bash "$SCRIPT_DIR/bin/hb-tep-onboard" "$NODE_NAME" "$ROLE" "$STORAGE_ROLE"' in install)
ok('tep_packaged', '/opt/hashburst-tep/tep' in install)
ok('tep_runtime_packaged_by_directory_copy', (ROOT/'tep/hb_tep_runtime.py').is_file())
ok('tep_existing_env_preserved', 'if [ ! -f /etc/hashburst/hashburst-tep.env ]' in install)
ok('tep_aes_gcm_required', 'AES-256-GCM' in onboard and 'cryptography' in onboard)
ok('tep_key_material_not_deleted', 'rm -f /var/lib/hashburst/tep' not in onboard and 'node_x25519.key' not in onboard)
ok('stable_peer_id_from_local_blockchain', '/api/health' in onboard and 'HB_TEP_PEER_ID' in onboard)
ok('tep_pubkey_before_node_registration', 'set_env "$NODE_ENV" TEP_PUBKEY' in onboard and onboard.index('set_env "$NODE_ENV" TEP_PUBKEY') < onboard.index('systemctl enable --now hashburst-node.service'))
ok('peer_id_replacement_refused', 'refusing to replace existing HB_TEP_PEER_ID' in onboard)
ok('non_rendezvous_relay_default_off', 'HB_TEP_RELAY_ENABLED "0"' in onboard)
ok('canonical_rendezvous_self_relay', 'HB_TEP_RELAY_ENABLED "1" replace' in onboard and 'HB_TEP_RENDEZVOUS_PEERS "$PEER_ID" replace' in onboard)
ok('trusted_rendezvous_configured', 'HB_TEP_TRUSTED_RENDEZVOUS' in onboard and 'HB_TEP_RENDEZVOUS_PEERS' in onboard)
ok('bootstrap_has_no_secret_material', 'swarm.key' not in onboard and 'replication_token' not in onboard and 'HB_ADMIN_SECRET' not in onboard)
ok('bootstrap_public_rendezvous_identity', '64.31.4.9' in onboard and '50506353bd0ac23aec8502e3d5ed6c018975a7c5ea6e22dc363df321d6ca8960' in onboard)
ok('edge_8091_not_opened', 'if [ "$STORAGE_ROLE" = edge ]; then' in install and 'Edge storage: :8091 remains unexposed' in install)
ok('tep_udp_firewall_only', "ufw allow 47777/udp" in onboard)
ok('status_ipc_localhost', "ThreadingHTTPServer(('127.0.0.1', self.status_port)" in tep)
ok('tep_service_hardened', all(x in service for x in ['NoNewPrivileges=true','ProtectSystem=strict','ProtectHome=true','RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX']))
ok('tep_service_uses_v215_runtime', 'ExecStart=/usr/bin/python3 -m tep.hb_tep_runtime' in service)
ok('dynamic_nat_heartbeat_refresh', all(x in runtime for x in ['find_by_wire_node_id','_record_authenticated_heartbeat','update_authenticated_endpoint','_relay_table.observe']))
ok('dynamic_nat_key_pin', 'heartbeat TEP public key mismatch' in runtime)
ok('local_rendezvous_storage_summary_only', 'env.service != "storage.summary"' in runtime and 'local relay permits storage.summary only' in runtime)
ok('aggregator_tep_transport', 'transport' in agg and 'tep_peer_id' in agg and 'summary_node_id' in agg)
ok('aggregator_flat_layout_supported', 'import hb_tep_adapter' in agg)
ok('aggregator_ipc_fixed_local', 'IPC_HOST = "127.0.0.1"' in adapter and 'IPC_PATH = "/app/storage-summary"' in adapter)
ok('ci_v215_gate', 'v2.1.5 installer and onboarding' in ci and 'v2.1.5 NAT runtime' in ci and 'v2.1.5 release contract' in ci)
ok('replication_still_not_auto_enabled', 'enable --now hashburst-replication-controller' not in install and 'enable --now hashburst-replica-agent' not in install)

print(json.dumps({'ok':True,'checks':len(checks),'version':'2.1.5'}, indent=2))
