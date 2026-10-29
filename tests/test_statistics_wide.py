import json,sqlite3,tempfile,time,unittest
from pathlib import Path
from contextlib import contextmanager,closing
from unittest.mock import patch,MagicMock
import ai_costs
import service_reports as reports
from service_reports_center import center_report
import sync_data

class Store:
 def __init__(self,path):self.path=path
 @contextmanager
 def connection(self):
  db=sqlite3.connect(self.path);db.row_factory=sqlite3.Row
  try:
   with db:yield db
  finally:db.close()

class StatisticsTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.store=Store(Path(self.temp.name)/'authority.sqlite');self.now=int(time.time());self.i=0
  with self.store.connection() as db:
   db.executescript('''CREATE TABLE managers(account_id INTEGER,subject TEXT,company_id TEXT);
   CREATE TABLE manager_directory(account_id INTEGER,full_name TEXT,department TEXT,company_name TEXT);
   CREATE TABLE events(account_id INTEGER,event_id TEXT,digest TEXT,payload TEXT,received_at INTEGER,company_id TEXT,subject TEXT);''')
   db.executemany('INSERT INTO managers VALUES(?,?,?)',[(11,'manager_001:11','dvk'),(22,'manager_002:22','pravo'),(33,'manager_003:33','dvk')])
   db.executemany('INSERT INTO manager_directory VALUES(?,?,?,?)',[(11,'Иван','Финанс','ДВК'),(22,'Пётр','Право','ДВК'),(33,'Мария','Финанс','ДВК')])
  for account,company,manager in [(11,'dvk','manager_001'),(22,'pravo','manager_002')]:
   self.event(account,company,manager,'clients',dict(id=1,telegram_id=account*100,product='БГ',manager_id=manager,full_name='Клиент',username='client'))
   self.event(account,company,manager,'messages',dict(id=1,client_id=1,telegram_message_id=1,status='sent',sent_at=self.now,text='Первое сообщение'))
 def event(self,account,company,manager,table,row):
  self.i+=1
  payload=dict(installation='installation-'+str(account),data=dict(table=table,row=row))
  with self.store.connection() as db:db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',(account,str(self.i),'digest',json.dumps(payload),self.now,company,manager+':'+str(account)))
 def report(self,**filters):return center_report(self.store,99,99,start=self.now-100,end=self.now+100,**filters)
 def test_all_company_and_organization_scopes(self):
  self.assertEqual(self.report()['metrics']['sent_messages'],2)
  report=self.report(organization='Финанс')
  self.assertEqual(report['metrics']['sent_messages'],1)
  self.assertEqual(report['total_managers'],2)
  self.assertEqual(report['missing_accounts'],[33])
  self.assertEqual(self.report(company_id='pravo')['managers'][0]['member']['account_id'],22)
 def test_account_scope_and_admin_authority(self):
  self.assertEqual(len(self.report(account_id=11)['managers']),1)
  with self.assertRaises(PermissionError):center_report(self.store,11,99,start=self.now-100,end=self.now+100)
  with self.assertRaises(PermissionError):self.report(account_id=22,organization='Финанс')
 def test_replayed_updates_are_not_duplicate_messages(self):
  self.event(11,'dvk','manager_001','messages',dict(id=1,client_id=1,telegram_message_id=1,status='sent',sent_at=self.now,text='Первое сообщение'))
  self.assertEqual(self.report(account_id=11)['metrics']['sent_messages'],1)
 def test_missing_statistics_is_explicit_not_full_coverage(self):
  r=self.report();self.assertEqual(r['reporting_managers'],0);self.assertFalse(r['complete'])
 def test_new_model_cost_is_priced_and_unknown_model_is_unknown(self):
  row=dict(model='gpt-5-mini',input_tokens=1000000,output_tokens=1000000,cached_tokens=0,usage_json='{"tool_calls":[]}')
  self.assertEqual(ai_costs.estimate(row)['usd_min'],2.25)
  row['model']='not-priced-model';self.assertIsNone(ai_costs.estimate(row)['usd_min'])
 def test_actual_tool_calls_and_estimates_are_separate(self):
  row=dict(model='gpt-5-mini',input_tokens=1,output_tokens=1,cached_tokens=0,usage_json=json.dumps(dict(tools=['web_search'],tool_calls=['web_search_call'])))
  cost=ai_costs.estimate(row);self.assertEqual(cost['tool_usd_known'],.01);self.assertFalse(cost['billing_complete'])
 def test_reporting_uses_existing_sync_only_after_explicit_enable(self):
  with closing(sqlite3.connect(':memory:')) as db:
   db.row_factory=sqlite3.Row
   db.execute('CREATE TABLE local_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)')
   for table in sync_data.REPORT_TABLES:db.execute('CREATE TABLE '+table+'(id INTEGER)')
   sync_data.initialize(db)
   db.execute('INSERT INTO report_usage VALUES(1)')
   self.assertEqual(db.execute('SELECT count(*) FROM sync_outbox').fetchone()[0],0)
   sync_data.enable_report_export(db);n=db.execute('SELECT count(*) FROM sync_outbox').fetchone()[0]
   self.assertEqual(n,1)
   sync_data.enable_report_export(db);self.assertEqual(db.execute('SELECT count(*) FROM sync_outbox').fetchone()[0],n)
   db.execute('INSERT INTO report_usage VALUES(2)');self.assertEqual(db.execute('SELECT count(*) FROM sync_outbox').fetchone()[0],2)
 def test_provider_costs_requires_separate_admin_key(self):
  with patch.dict('os.environ',{},clear=True),patch('urllib.request.urlopen') as call:
   self.assertFalse(ai_costs.provider_costs(self.now-10,self.now)['available'])
  call.assert_not_called()
 def test_both_roles_have_statistics_button(self):
  import bot_menu
  self.assertIn('📈 Статистика',bot_menu.SENDER);self.assertIn('📈 Статистика',bot_menu.ADMIN)

if __name__=='__main__':unittest.main()
