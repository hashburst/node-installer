import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('accelerator', ROOT/'accelerator.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

def worker(t, pool='Zpool', revenue=0.0001):
    return {'user':'test','worker':'worker','machinename':'host','cpu':'cpu','lastseen':t,
            'online':True,'status':'Running','powerdraw':100,'maxtemp':50,
            'data':[{'Name':'miner','Version':'1','Algorithm':['RandomX'],'Pool':[pool],
                     'Devices':[{'Id':0,'Name':'CPU','Temp':50}], 'BTC/day':revenue,
                     'Benchmarking':False,'Donator':False,'Accepted':[100], 'Rejected':[0],'Stale':[0]}]}

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=a.database(str(Path(self.tmp.name)/'history.sqlite'))
        self.policy=json.loads((ROOT/'policy.example.json').read_text())
        self.policy.update(electricity_fiat_kwh=0.2,btc_fiat=100000,price_observed_at=2000,max_age_seconds=1000)
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def ingest(self,w,t):return a.collect(self.db,[w],{'generated_at':t,'pools':[]},t)
    def test_duplicates_are_not_training_evidence(self):
        self.ingest(worker(1000),1000);r=self.ingest(worker(1000),1001)
        self.assertEqual(r['workers']['duplicates'],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM samples').fetchone()[0],1)
    def test_unsafe_and_missing_power_excluded(self):
        for update in [{'online':False},{'tempalert':1},{'powerdraw':0},{'lastseen':1}]:
            w=worker(1000);w.update(update);self.assertEqual(self.ingest(w,1000)['workers']['excluded'],1)
    def test_secrets_and_network_addresses_not_stored(self):
        w=worker(1000);w.update(machineip='PRIVATE',console='PRIVATE',response='PRIVATE')
        self.ingest(w,1000)
        stored=self.db.execute('SELECT payload FROM samples').fetchone()[0]
        for s in ['PRIVATE','machinename','"user"','"host"']:self.assertNotIn(s,stored)
    def test_unknown_economics_holds(self):
        self.ingest(worker(2000),2000);self.policy['btc_fiat']=None
        self.assertEqual(a.recommend(self.db,self.policy,2000)['decisions'][0]['action'],'hold')
    def test_observed_better_profile_recommended(self):
        for i in range(6):self.ingest(worker(1000+i*60,'Nanopool',0.0002),1000+i*60)
        for i in range(6):self.ingest(worker(1400+i*60,'Zpool',0.0001),1400+i*60)
        result=a.recommend(self.db,self.policy,2000)
        self.assertFalse(result['apply_supported'])
        self.assertEqual(result['decisions'][0]['action'],'recommend_profile')
    def test_new_health_alert_overrides_old_valid_data(self):
        for i in range(6):self.ingest(worker(1000+i*60,'Nanopool',0.0002),1000+i*60)
        for i in range(6):self.ingest(worker(1400+i*60),1400+i*60)
        w=worker(1999);w['crashalert']=1;self.ingest(w,1999)
        self.assertEqual(a.recommend(self.db,self.policy,2000)['decisions'][0]['reason'],'current worker health not confirmed')
    def test_current_overtemperature_blocks_older_models(self):
        for i in range(6):self.ingest(worker(1000+i*60,'Nanopool',0.0002),1000+i*60)
        for i in range(6):self.ingest(worker(1400+i*60),1400+i*60)
        w=worker(1999);w['data'][0]['Devices'][0]['Temp']=99;self.ingest(w,1999)
        self.assertEqual(a.recommend(self.db,self.policy,2000)['decisions'][0]['reason'],'current temperature exceeds policy')
    def test_alert_threshold_is_not_measured_temperature(self):
        w=worker(2000);w['maxtemp']=90;self.ingest(w,2000)
        row=json.loads(self.db.execute('SELECT payload FROM samples').fetchone()[0])
        self.assertEqual(row['temperature_c'],50)
    def test_missing_sensor_temperature_excluded(self):
        w=worker(2000);w['data'][0]['Devices'][0]['Temp']=0
        self.assertEqual(self.ingest(w,2000)['workers']['excluded'],1)
    def test_partial_missing_sensor_temperature_uses_valid_reading(self):
        w=worker(2000)
        w['data'][0]['Devices'].append({'Id':1,'Name':'GPU','Temp':0})
        self.assertEqual(self.ingest(w,2000)['workers']['inserted'],1)
        row=json.loads(self.db.execute('SELECT payload FROM samples').fetchone()[0])
        self.assertEqual(row['temperature_c'],50)
    def test_unavailable_sensor_temperature_uses_valid_reading(self):
        w=worker(2000)
        w['data'][0]['Devices'].append({'Id':1,'Name':'GPU','Temp':None})
        self.assertEqual(self.ingest(w,2000)['workers']['inserted'],1)
        row=json.loads(self.db.execute('SELECT payload FROM samples').fetchone()[0])
        self.assertEqual(row['temperature_c'],50)
    def test_old_normalization_cannot_be_recommended(self):
        self.ingest(worker(2000),2000)
        row=json.loads(self.db.execute('SELECT payload FROM samples').fetchone()[0]);del row['temperature_source']
        self.db.execute('UPDATE samples SET payload=?',(json.dumps(row),));self.db.commit()
        result=a.recommend(self.db,self.policy,2000)['decisions'][0]
        self.assertEqual(result['action'],'hold')
        self.assertIn('upgrade',result['reason'])
    def test_malformed_worker_excluded(self):
        r=a.collect(self.db,[None,worker(2000)],{'generated_at':2000,'pools':[]},2000)
        self.assertEqual(r['workers']['excluded'],1)
        self.assertEqual(r['workers']['inserted'],1)
    def test_stale_price_holds(self):
        self.ingest(worker(2000),2000);self.policy['price_observed_at']=1
        self.assertEqual(a.recommend(self.db,self.policy,2000)['decisions'][0]['action'],'hold')
    def test_future_and_nonfinite_rejected(self):
        w=worker(5000);self.assertEqual(self.ingest(w,1000)['workers']['excluded'],1)
        w=worker(1000);w['powerdraw']=float('nan')
        with self.assertRaises(ValueError):self.ingest(w,1000)
    def test_unknown_pool_not_recommended(self):
        for i in range(6):self.ingest(worker(1000+i*60,'Unknown',0.005),1000+i*60)
        for i in range(6):self.ingest(worker(1400+i*60),1400+i*60)
        self.assertEqual(a.recommend(self.db,self.policy,2000)['decisions'][0]['action'],'hold')
    def test_non_shadow_policy_refused(self):
        self.policy['mode']='apply'
        with self.assertRaises(ValueError):a.recommend(self.db,self.policy,2000)
    def test_pool_timestamp_required(self):
        with self.assertRaises(ValueError):a.collect(self.db,[],{'generated_at':1,'pools':[]},2000)
    def test_output_never_overwrites(self):
        p=Path(self.tmp.name)/'plan.json';a.write_new(p,{})
        with self.assertRaises(FileExistsError):a.write_new(p,{})

if __name__=='__main__':unittest.main()
