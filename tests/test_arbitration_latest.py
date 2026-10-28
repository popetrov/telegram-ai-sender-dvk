import os
import unittest
from unittest.mock import patch
os.environ.setdefault('TELEGRAM_API_ID','12345')
import arbitration_api as api
from context_adapters import append_arbitration_context

INN='1234567890'

def case(id,date,finished=False):
    return dict(CaseId=id,StartDate=date,CaseNumber='А00-'+id+'/2026',Finished=finished,
                Respondents=[dict(Inn=INN,Name='ООО Тест')])

def page(cases,pages=1):return dict(Success=1,PagesCount=pages,Cases=cases)


class LatestTests(unittest.TestCase):
    def search(self,responses):
        with patch('config.PARSER_API_KEY','fixture'),patch.object(api,'_api_get',side_effect=responses) as call:
            result=api.find_latest_defendant_case(INN)
        return result,call

    def test_newest_can_be_on_last_page_and_finished(self):
        new=case('new','2026-10-27',True)
        result,call=self.search([page([case('old','2020-01-01')],2),page([new],2),dict(Success=1,Cases=[new])])
        self.assertEqual(result['status'],'CONFIRMED')
        self.assertEqual(result['case']['case_id'],'new')
        self.assertTrue(result['case']['finished'])
        self.assertEqual([c.args[1].get('page') for c in call.call_args_list],[1,2,None])

    def test_newest_detail_failure_never_falls_back(self):
        result,call=self.search([page([case('old','2020-01-01'),case('new','2026-10-27')]),dict(Success=0)])
        self.assertEqual(result['status'],'ERROR');self.assertIsNone(result['case'])
        self.assertEqual(call.call_count,2)

    def test_incomplete_or_changing_pages_and_unknown_dates_stop(self):
        for replies in ([page([case('old','2020-01-01')],2),dict(Success=0)],
                        [dict(Success=1,Cases=[case('old','2020-01-01')])],
                        [page([case('old','2020-01-01')],2),page([],2)],
                        [page([case('old','2020-01-01')],2),page([case('new','2026-10-27')],3)],
                        [page([case('old','2020-01-01'),case('unknown',None)])]):
            with self.subTest(replies=replies):
                result,_=self.search(replies);self.assertEqual(result['status'],'ERROR')

    def test_newest_identity_or_date_conflict_stops(self):
        for change in (dict(Respondents=[dict(Name='Другая компания')]),dict(StartDate='2019-01-01')):
            new=case('new','2026-10-27');detail={**new,**change}
            result,call=self.search([page([case('old','2020-01-01'),new]),dict(Success=1,Cases=[detail])])
            self.assertEqual(result['status'],'ERROR');self.assertEqual(call.call_count,2)

    def test_error_blocks_generation_instead_of_web_fallback(self):
        with patch.object(api,'find_latest_defendant_case',return_value=dict(status='ERROR',case=None,reason='Неполная выдача')):
            with self.assertRaisesRegex(RuntimeError,'РУЧНАЯ ПРОВЕРКА'):
                append_arbitration_context('prompt',INN)

    def test_empty_search_is_not_an_old_case(self):
        result,_=self.search([page([],0)])
        self.assertEqual(result['status'],'NOT_FOUND')
