#!/usr/bin/env python3
"""Standalone release contract check for HashBurst Node Installer v2.1.2."""
from pathlib import Path
import importlib.util, json, sys

root=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path.cwd()
checks=[]
def ok(name, cond):
    if not cond: raise AssertionError(name)
    checks.append(name); print('PASS', name)

unit=(root/'aggregator/hashburst-aggregator.service').read_text()
server=(root/'aggregator/hb_aggregator_server.py').read_text()
ok('systemd_storage_port_8094', 'Environment=HB_AGGREGATOR_PORT=8094' in unit)
ok('systemd_timeout_3', 'Environment=HB_AGGREGATOR_TIMEOUT=3' in unit)
ok('systemd_not_storage_8093', 'Environment=HB_AGGREGATOR_PORT=8093' not in unit)
ok('server_default_8094', 'os.environ.get("HB_AGGREGATOR_PORT", "8094")' in server)

cfg=json.loads((root/'aggregator/storage-nodes.example.json').read_text())
edge=next(n for n in cfg['nodes'] if n['role']=='edge')
ok('edge_example_explicit_best_effort', edge['capacity_class']=='best-effort')

p=root/'aggregator/hb_aggregator.py'
spec=importlib.util.spec_from_file_location('hb_agg_v212',p)
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ok('offline_unknown_fail_safe', m._capacity_class({'online':False})=='unknown')
ok('offline_edge_role_best_effort', m._capacity_class({'online':False,'configured_role':'edge'})=='best-effort')
ok('online_edge_role_best_effort', m._capacity_class({'online':True,'role':'edge','configured_class':'committable'})=='best-effort')
ok('online_primary_role_committable', m._capacity_class({'online':True,'role':'primary','configured_class':'best-effort'})=='committable')

patcher=(root/'integrations/explorer/patch_hashburst_explorer.py').read_text()
ok('explorer_new_committable_field', 'capacity_committable_gb' in patcher)
ok('explorer_new_best_effort_field', 'capacity_best_effort_gb' in patcher)
ok('explorer_no_element_style_assignment', '.style.' not in patcher)
print(f'PASS v2.1.2 release regression ({len(checks)} checks)')
