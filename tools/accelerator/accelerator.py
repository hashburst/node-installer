#!/usr/bin/env python3
"""Read-only telemetry collection and empirical mining policy evaluation."""
import argparse
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import time
import urllib.request
import urllib.parse

VERSION = '0.1.1'
MAX_BYTES = 16 * 1024 * 1024

def canonical(v):
    return json.dumps(v, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(v):
    return hashlib.sha256(canonical(v).encode()).hexdigest()

def number(v, minimum=0):
    if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < minimum:
        raise ValueError('invalid finite numeric telemetry')
    return v

def fetch_json(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or parts.username or parts.password:
        raise ValueError('HTTPS endpoint required without embedded credentials')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError('API redirects refused')
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'HashBurst-Accelerator/'+VERSION})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
        data = response.read(MAX_BYTES+1)
    if len(data) > MAX_BYTES:
        raise ValueError('API response exceeds limit')
    return json.loads(data)

def read_json(path):
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)

def write_new(path, value):
    with open(path, 'x', encoding='utf-8') as f:
        f.write(json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+'\n')

def database(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS samples (worker TEXT, sampled INTEGER, profile TEXT, payload TEXT, PRIMARY KEY(worker,sampled))')
    db.execute('CREATE TABLE IF NOT EXISTS collections (collected INTEGER PRIMARY KEY, payload TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS worker_health (worker TEXT PRIMARY KEY, collected INTEGER, healthy INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS local_identity (id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB)')
    db.execute('INSERT OR IGNORE INTO local_identity VALUES (1, ?)', (os.urandom(32),))
    db.commit()
    return db

def strings(value):
    if not isinstance(value, list) or not value or any(not isinstance(x,str) or not x or len(x)>128 for x in value):
        raise ValueError('expected nonempty string list')
    return value

def normalize(worker, salt, now, max_age):
    identity = [worker.get('user'), worker.get('worker'), worker.get('machinename')]
    if any(not isinstance(x,str) or not x for x in identity):
        raise ValueError('worker identity incomplete')
    key = hmac.new(salt, canonical(identity).encode(), hashlib.sha256).hexdigest()
    seen = int(number(worker['lastseen']))
    if seen > now+30 or now-seen > max_age or worker.get('online') is not True or worker.get('status') != 'Running':
        raise ValueError('worker offline, paused or stale')
    if any(worker.get(k) not in (None,0,False,'') for k in ('tempalert','crashalert','uncleanalert','diskmingbalert')):
        raise ValueError('worker health alert')
    watts = number(worker['powerdraw'])
    if watts <= 0:
        raise ValueError('missing measured power')
    temp = 0  # maxtemp is an alert threshold, not a sensor reading.
    workload=[]; revenue=0; accepted=0; rejected=0
    data=worker.get('data')
    if not isinstance(data,list) or not data:
        raise ValueError('missing workload')
    for miner in data:
        if miner.get('Benchmarking') is not False or miner.get('Donator') is not False:
            raise ValueError('benchmark or donation workload')
        slots=[]
        for device in miner.get('Devices',[]):
            slots.append({'id':str(device['Id']), 'name':str(device['Name'])})
            try:
                measured = number(device.get('Temp'))
            except ValueError:
                continue
            if measured > 0:
                temp=max(temp,measured)
        if not slots:
            raise ValueError('missing device inventory')
        if temp <= 0:
            raise ValueError('missing measured device temperature')
        workload.append({'miner':str(miner['Name']), 'version':str(miner['Version']),
                         'algorithms':strings(miner['Algorithm']), 'pools':strings(miner['Pool']),
                         'devices':sorted(slots,key=canonical)})
        revenue += number(miner['BTC/day'])
        for field in ('Accepted','Rejected','Stale'):
            values=miner.get(field)
            if not isinstance(values,list) or not values:
                raise ValueError('missing share counters')
            total=sum(number(x) for x in values)
            if field=='Accepted':accepted+=total
            else:rejected+=total
    workload.sort(key=canonical)
    hardware=digest({'cpu':worker.get('cpu'), 'slots':[x['devices'] for x in workload]})
    profile=digest(workload)
    return {'worker':key,'sampled':seen,'profile':profile,'hardware':hardware,'workload':workload,
            'estimated_btc_day':revenue,'watts':watts,'temperature_c':temp,
            'temperature_source':'device_temp_v1',
            'accepted':accepted,'rejected_or_stale':rejected}

def collect(db, workers, pools, now, max_age=300):
    if not isinstance(workers,list):
        raise ValueError('RainbowMiner response must be a worker list')
    if not isinstance(pools,dict) or not isinstance(pools.get('pools'),list):
        raise ValueError('pool summary schema not recognized')
    timestamp=int(number(pools['generated_at']))
    if not -30 <= now-timestamp <= max_age:
        raise ValueError('pool summary stale')
    salt=db.execute('SELECT salt FROM local_identity WHERE id=1').fetchone()[0]
    counts={'received':len(workers),'inserted':0,'duplicates':0,'excluded':0}
    reasons={}
    with db:
        for worker in workers:
            if not isinstance(worker, dict):
                counts['excluded'] += 1
                reasons['incomplete telemetry'] = reasons.get('incomplete telemetry', 0) + 1
                continue
            identity=[worker.get('user'),worker.get('worker'),worker.get('machinename')]
            key=hmac.new(salt,canonical(identity).encode(),hashlib.sha256).hexdigest()
            db.execute('INSERT OR REPLACE INTO worker_health VALUES (?,?,0)',(key,now))
            try:
                row=normalize(worker,salt,now,max_age)
                cur=db.execute('INSERT OR IGNORE INTO samples VALUES (?,?,?,?)',
                               (row['worker'],row['sampled'],row['profile'],canonical(row)))
                counts['inserted' if cur.rowcount else 'duplicates']+=1
                db.execute('UPDATE worker_health SET healthy=1 WHERE worker=?',(key,))
            except (ValueError,KeyError,TypeError) as exc:
                counts['excluded']+=1
                reason=str(exc) if isinstance(exc,ValueError) else 'incomplete telemetry'
                reasons[reason]=reasons.get(reason,0)+1
        # Pool totals are not allocated to individual workers or treated as payouts.
        summary={'collected_at':now,'workers':counts,'excluded_reasons':reasons,
                 'pool_generated_at':timestamp,'pool_count':len(pools['pools']),
                 'pool_summary_sha256':digest(pools),'worker_response_sha256':digest(workers)}
        db.execute('INSERT OR REPLACE INTO collections VALUES (?,?)',(now,canonical(summary)))
    return summary

def validate_policy(p):
    if p.get('mode')!='shadow':
        raise ValueError('only shadow mode is implemented')
    for key in ('max_age_seconds','history_seconds','min_samples','min_span_seconds','min_sample_spacing_seconds','max_temperature_c','min_improvement_btc_day','min_improvement_ratio','confidence_multiplier'):
        number(p[key])
    if p['min_samples']<3 or p['min_span_seconds']<60 or p['min_sample_spacing_seconds']<1:
        raise ValueError('insufficient statistical observation policy')
    for key in ('allowed_pools','allowed_algorithms'):
        strings(p[key])
    for key in ('electricity_fiat_kwh','btc_fiat'):
        if p.get(key) is not None:number(p[key])
    if p.get('btc_fiat') == 0:raise ValueError('BTC exchange rate must be positive')
    if p.get('btc_fiat') is not None:number(p['price_observed_at'])
    return p

def recommend(db, p, now):
    validate_policy(p)
    out={'schema':'hashburst-accelerator-shadow-v1','created_at':now,'expires_at':now+p['max_age_seconds'],
         'policy_sha256':digest(p),'mode':'shadow','apply_supported':False,
         'objective':'estimated BTC/day minus electricity; not settled pool payouts',
         'decisions':[]}
    economics=p.get('electricity_fiat_kwh') is not None and p.get('btc_fiat') is not None
    economics=economics and -30<=now-p['price_observed_at']<=p['max_age_seconds']
    rows=[json.loads(x[0]) for x in db.execute('SELECT payload FROM samples WHERE sampled>=? AND sampled<=? ORDER BY worker,sampled',(now-p['history_seconds'],now+30))]
    workers={}
    for row in rows:workers.setdefault(row['worker'],[]).append(row)
    for worker,history in workers.items():
        latest=history[-1]
        decision={'worker':worker,'action':'hold','current_profile':latest['profile'],'sampled':latest['sampled']}
        out['decisions'].append(decision)
        if latest.get('temperature_source') != 'device_temp_v1':
            decision['reason']='new temperature telemetry required after collector upgrade';continue
        health=db.execute('SELECT collected,healthy FROM worker_health WHERE worker=?',(worker,)).fetchone()
        if not health or not health[1] or now-health[0]>p['max_age_seconds']:
            decision['reason']='current worker health not confirmed';continue
        if now-latest['sampled']>p['max_age_seconds']:
            decision['reason']='stale telemetry';continue
        if latest['temperature_c'] > p['max_temperature_c']:
            decision['reason']='current temperature exceeds policy';continue
        if not economics:
            decision['reason']='electricity cost or fresh BTC price missing';continue
        grouped={}
        for row in history:
            if row.get('temperature_source') != 'device_temp_v1':continue
            if row['hardware']!=latest['hardware'] or row['temperature_c']>p['max_temperature_c']:continue
            if any(pool not in p['allowed_pools'] for m in row['workload'] for pool in m['pools']):continue
            if any(alg not in p['allowed_algorithms'] for m in row['workload'] for alg in m['algorithms']):continue
            group=grouped.setdefault(row['profile'],[])
            if not group or row['sampled']-group[-1]['sampled']>=p['min_sample_spacing_seconds']:group.append(row)
        models={}
        for profile,samples in grouped.items():
            if len(samples)<p['min_samples'] or samples[-1]['sampled']-samples[0]['sampled']<p['min_span_seconds']:continue
            if now-samples[-1]['sampled']>p['max_age_seconds']:continue
            values=[s['estimated_btc_day']-s['watts']*24/1000*p['electricity_fiat_kwh']/p['btc_fiat'] for s in samples]
            mean=statistics.mean(values)
            margin=p['confidence_multiplier']*statistics.stdev(values)/math.sqrt(len(values))
            models[profile]={'mean':mean,'lower':mean-margin,'upper':mean+margin,'samples':len(values),
                             'workload':samples[-1]['workload']}
        if latest['profile'] not in models or len(models)<2:
            decision['reason']='insufficient recent comparable workload history';continue
        current=models[latest['profile']]
        best=max(models,key=lambda x:models[x]['lower'])
        delta=models[best]['lower']-current['upper']
        threshold=max(p['min_improvement_btc_day'],abs(current['mean'])*p['min_improvement_ratio'])
        if best!=latest['profile'] and delta>threshold:
            decision.update(action='recommend_profile',candidate_profile=best,workload=models[best]['workload'],
                            conservative_gain_btc_day=delta,reason='empirical estimate exceeds switching threshold')
        else:decision['reason']='no sufficiently supported improvement'
        decision['model']=models
    return out

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    c=sub.add_parser('collect');c.add_argument('--db',required=True);c.add_argument('--workers-url-env',default='HB_RBMINER_URL');c.add_argument('--pool-url',default='https://blockchainapi.one/api/hashburst/mining/summary')
    r=sub.add_parser('recommend');r.add_argument('--db',required=True);r.add_argument('--policy',required=True);r.add_argument('--out',required=True)
    sub.add_parser('doctor')
    args=parser.parse_args()
    if args.command=='doctor':
        print(json.dumps({'platform':platform.system(),'architecture':platform.machine(),'python':platform.python_version(),
                          'cpu_logical':os.cpu_count(),'accelerator_mode':'shadow','full_node_installed':False,
                          'gpu_fpga_asic_capability':'requires driver and benchmark discovery'},indent=2));return
    now=int(time.time())
    with database(args.db) as db:
        if args.command=='collect':
            url=os.environ.get(args.workers_url_env)
            if not url:raise ValueError('worker URL environment variable missing')
            print(json.dumps(collect(db,fetch_json(url),fetch_json(args.pool_url),now),indent=2))
        else:
            result=recommend(db,read_json(args.policy),now);write_new(args.out,result)
            print('SHADOW_PLAN_WRITTEN no_worker_configuration_applied')

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,sqlite3.Error):
        raise SystemExit('STOP: invalid, unavailable or incomplete input; no worker configuration applied')
