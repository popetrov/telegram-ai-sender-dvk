import asyncio
import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace as Object
from unittest.mock import AsyncMock,Mock,patch
os.environ.setdefault('TELEGRAM_API_ID','12345')
import arbitration_api as api
import legal_research as legal
import config


def confirmed(inn='123456789012'):
    event=dict(id='e',date='2026-10-26',type='Заявление',content='Исковое заявление',claim_sum='105065.02',document_url=None)
    return dict(status='CONFIRMED',case=dict(case_id='id',case_number='А40-123/2026',start_date='2026-10-26',
        defendant_inn=inn,defendant_name='ИП Тестовый Иван Иванович',plaintiffs=['ООО Истец'],respondents=['ИП Тестовый Иван Иванович'],
        court='АС города Москвы',claim_sum='105065.02',latest_event=event,events=[event],finished=True,state='Завершено',card_url='https://kad.arbitr.ru/Card/id'))


class ParserRecoveryTests(unittest.TestCase):
    def setUp(self):
        api._blocked_keys.clear()
        api._cooldowns.clear()
        self.sleep=patch.object(api.time,'sleep');self.sleep.start();self.addCleanup(self.sleep.stop)
    def response(self,data):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps(data).encode();return response
    def test_temporary_http_then_success_keeps_page_and_observes_both(self):
        failed=urllib.error.HTTPError('https://provider/?key=SECRET',503,'Unavailable',{},None)
        events=[]
        with patch.object(api.urllib.request,'urlopen',side_effect=[failed,self.response(dict(Success=1,Cases=[],PagesCount=0))]) as request,api.observe_requests(events.append):
            result=api._api_get(api.API_URL,{'page':2},'SECRET')
        self.assertEqual(result['Success'],1);self.assertEqual(request.call_count,2)
        self.assertEqual([e['params']['page'] for e in events],[2,2])
        self.assertNotIn('SECRET',json.dumps(events))
    def test_success_zero_is_retried_but_exhaustion_is_bounded(self):
        with patch.object(api.urllib.request,'urlopen',return_value=self.response(dict(Success=0))) as request:
            with self.assertRaises(api.ParserFailure) as error:api._api_get(api.API_URL,{},'key')
        self.assertTrue(error.exception.retryable);self.assertEqual(request.call_count,3)
    def test_quota_stops_other_rows_until_explicit_retry(self):
        error=urllib.error.HTTPError('https://provider/?key=SECRET',403,'Forbidden',{},io.BytesIO(b'{"error_code":40305}'))
        with patch.object(api.urllib.request,'urlopen',side_effect=error) as request:
            for _ in range(2):
                with self.assertRaises(api.ParserFailure) as raised:api._api_get(api.API_URL,{},'SECRET')
                self.assertEqual(raised.exception.code,'quota');self.assertNotIn('SECRET',str(raised.exception))
            self.assertEqual(request.call_count,1)
        api.reset_service_block('SECRET')
        with patch.object(api.urllib.request,'urlopen',return_value=self.response(dict(Success=1))):
            self.assertEqual(api._api_get(api.API_URL,{},'SECRET')['Success'],1)
    def test_empty_newest_card_retries_without_using_an_old_case(self):
        with patch.object(api,'_api_get',return_value=dict(Success=1,Cases=[])) as get:
            with self.assertRaises(api.ParserFailure):api._get_case_details('newest','key')
        self.assertEqual(get.call_count,3)
    def test_rate_limit_respects_pause_and_is_not_reset_by_retry_button(self):
        error=urllib.error.HTTPError('https://provider',429,'Rate limited',{'Retry-After':'120'},io.BytesIO(b'{}'))
        with patch.object(api.urllib.request,'urlopen',side_effect=error) as request:
            with self.assertRaises(api.ParserFailure):api._api_get(api.API_URL,{},'rate-key')
            api.reset_service_block('rate-key')
            with self.assertRaises(api.ParserFailure) as limited:api._api_get(api.API_URL,{},'rate-key')
        self.assertEqual(limited.exception.code,'rate_limit');self.assertEqual(request.call_count,1)


class LegalFactsTests(unittest.TestCase):
    def test_complete_finished_case_can_render_without_new_web_research(self):
        facts=legal.verified_facts(confirmed(),'123456789012')
        text=legal.fallback_message(facts,'Пётр')
        self.assertFalse(legal.validate_message(text,facts,'Пётр'))
        self.assertIn('Исковое заявление',text);self.assertNotIn('принят',text)
    def test_missing_or_conflicting_fields_are_real_manual_review(self):
        for patch_value in ({'claim_sum':None,'events':[]},{'latest_event':None},{'plaintiffs':[]},
                            {'defendant_inn':'999999999999'},{'court':None},{'respondents':['Первый','Второй']}):
            data=confirmed();data['case'].update(patch_value)
            with self.subTest(patch_value=patch_value),self.assertRaises(legal.ResearchProblem):legal.verified_facts(data,'123456789012')
    def test_fee_is_not_used_as_claim_amount(self):
        data=confirmed();data['case']['events'][0]['content']='О взыскании судебных расходов'
        with self.assertRaises(legal.ResearchProblem):legal.verified_facts(data,'123456789012')
    def test_changed_number_party_amount_date_and_false_acceptance_are_rejected(self):
        facts=legal.verified_facts(confirmed(),'123456789012');text=legal.fallback_message(facts,'Пётр')
        for bad in (text.replace('123/2026','999/2026'),text.replace('ООО Истец','ООО Другой'),
                    text.replace('105 065,02','1 000,00'),text.replace('26.10.2026','25.10.2026'),text+' Иск принят к производству.'):
            with self.subTest(text=bad):self.assertTrue(legal.validate_message(bad,facts,'Пётр'))
    def test_events_pass_from_details_with_semantics_and_link(self):
        raw=dict(CaseInstances=[dict(InstanceEvents=[dict(Id='old',Date='2026-01-01',EventTypeName='Определение'),
            dict(Id='new',Date='2026-10-26',EventContentTypeName='Исковое заявление',ClaimSum=10,File='https://kad.arbitr.ru/PdfDocument/file')])])
        events=api.case_events(raw);self.assertEqual(events[0]['id'],'new');self.assertEqual(events[0]['claim_sum'],10)
        self.assertEqual(events[0]['content'],'Исковое заявление')
    def test_explicit_different_ooo_inn_outweighs_same_name(self):
        result=api._find_party_by_identity([dict(Name='ООО Тест',Inn='1234567890')],
            [dict(Name='ООО Тест',Inn='9999999999')],'1234567890')
        self.assertIsNone(result)


class LegalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        legal._writer_cooldowns.clear()
        self.path=Path(self.temp.name)/'local.sqlite'
        for name,value in [('DATABASE_PATH',str(self.path)),('MANAGER_ID','manager_019'),('OPENAI_API_KEY','FAKE'),('PARSER_API_KEY','SECRET')]:
            patcher=patch.object(config,name,value);patcher.start();self.addCleanup(patcher.stop)
        self.row=dict(inn='123456789012',username='fixture',full_name='Иван',product='Право_ИП',fields={})
        self.settings=dict(model='gpt-5-mini',prompt='pravo_ip.md',version='1')
    def run_case(self,outputs,result=None):
        client=Mock();client.responses.create.side_effect=[Object(output_text=s) if isinstance(s,str) else s for s in outputs]
        factory=Mock(return_value=client)
        with patch.object(api,'find_latest_defendant_case',return_value=result or confirmed()):
            answer=legal.run(self.row,'Пётр',self.settings,'Методика',factory)
        return answer,client,factory
    def test_model_manual_reason_is_preserved_and_good_facts_use_safe_template(self):
        raw='ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА\nНе могу открыть сайт КАД.'
        answer,client,_=self.run_case([raw,raw])
        self.assertTrue(answer['legal_fallback']);self.assertIn('105 065,02',answer['final_message'])
        with legal.Journal(config.MANAGER_ID).db() as db:
            row=db.execute('SELECT model_json FROM legal_research_attempts').fetchone()
        self.assertIn('Не могу открыть сайт КАД',row[0]);self.assertEqual(client.responses.create.call_count,2)
    def test_invalid_writer_is_repaired_once_and_cannot_change_judicial_facts(self):
        text=legal.fallback_message(legal.verified_facts(confirmed(),self.row['inn']),'Пётр')
        answer,client,_=self.run_case(['ГОТОВОЕ СООБЩЕНИЕ\nНеверный номер А40-999/2026\nКОНЕЦ СООБЩЕНИЯ',json.dumps(dict(greeting='Здравствуйте!',offer_index=0))])
        self.assertFalse(answer['legal_fallback']);self.assertEqual(answer['final_message'],text)
        self.assertEqual(client.responses.create.call_args.kwargs['tools'],[])
    def test_missing_facts_never_call_ai_and_remain_retryable_by_explicit_action(self):
        data=confirmed();data['case']['latest_event']=None
        with patch.object(api,'find_latest_defendant_case',return_value=data):
            factory=Mock()
            with self.assertRaises(legal.ResearchProblem):legal.run(self.row,'Пётр',self.settings,'Методика',factory)
        factory.assert_not_called();self.assertEqual(len(legal.Journal(config.MANAGER_ID).retry_rows()),1)
    def test_generated_result_reuses_ai_only_after_fresh_same_case_check(self):
        text=legal.fallback_message(legal.verified_facts(confirmed(),self.row['inn']),'Пётр')
        self.run_case([json.dumps(dict(greeting='Здравствуйте!',offer_index=0))])
        with patch.object(api,'find_latest_defendant_case',return_value=confirmed()) as search:
            factory=Mock();answer=legal.run(self.row,'Пётр',self.settings,'Методика',factory)
        search.assert_called_once();factory.assert_not_called();self.assertEqual(answer['final_message'],text)
    def test_new_case_never_reuses_previous_generated_text(self):
        self.run_case(['ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА','ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА'])
        data=confirmed();data['case']['case_number']='А40-999/2026'
        answer,client,_=self.run_case(['bad','bad'],data)
        self.assertIn('999/2026',answer['final_message']);self.assertEqual(client.responses.create.call_count,2)
    def test_quota_creates_durable_deduplicated_owner_notice_without_client_details(self):
        for _ in range(2):
            with self.assertRaises(legal.ResearchProblem):self.run_case([],dict(status='ERROR',error_code='quota',reason='Лимит сервиса',retryable=False))
        with legal.Journal(config.MANAGER_ID).db() as db:
            rows=db.execute('SELECT code FROM legal_service_notices').fetchall()
        self.assertEqual([r[0] for r in rows],['quota'])
    def test_ready_and_uncertain_delivery_are_never_researched_again(self):
        self.run_case(['bad','bad'])
        with legal.Journal(config.MANAGER_ID).db() as db:
            db.execute('CREATE TABLE prepared_messages(id,manager_id,inn,username,product,status)')
            db.execute('INSERT INTO prepared_messages VALUES(1,?,?,?,?,?)',(config.MANAGER_ID,self.row['inn'],'fixture','Право_ИП','uncertain'))
        self.assertEqual(legal.Journal(config.MANAGER_ID).retry_rows(),[])
        with legal.Journal(config.MANAGER_ID).db() as db:
            self.assertEqual(db.execute('SELECT status FROM prepared_messages').fetchone()[0],'uncertain')
    def test_unavailable_writer_uses_safe_facts_template_after_one_call(self):
        answer,client,_=self.run_case([TimeoutError('private request URL')])
        self.assertTrue(answer['legal_fallback']);self.assertEqual(client.responses.create.call_count,1)
        with legal.Journal(config.MANAGER_ID).db() as db:
            self.assertNotIn('private request URL',db.execute('SELECT model_json FROM legal_research_attempts').fetchone()[0])
    def test_quota_batch_uses_one_network_request_and_preserves_all_failed_rows(self):
        api.reset_service_block('SECRET')
        error=urllib.error.HTTPError('https://provider/?key=SECRET',403,'Forbidden',{},io.BytesIO(b'{"error_code":40305}'))
        factory=Mock()
        with patch.object(api.urllib.request,'urlopen',side_effect=error) as network:
            for index in range(3):
                row={**self.row,'username':'client'+str(index)}
                with self.assertRaises(legal.ResearchProblem) as paused:legal.run(row,'Пётр',self.settings,'Методика',factory)
                self.assertEqual(paused.exception.status,'research_paused')
        self.assertEqual(network.call_count,1);factory.assert_not_called()
        self.assertEqual(len(legal.Journal(config.MANAGER_ID).retry_rows()),3)
        api.reset_service_block('SECRET')
    def test_writer_outage_does_not_delay_other_rows_again(self):
        self.run_case([TimeoutError('private')])
        self.row={**self.row,'username':'second'}
        answer,client,factory=self.run_case([])
        factory.assert_not_called();self.assertTrue(answer['legal_fallback'])
