import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
os.environ.setdefault('TELEGRAM_API_ID','12345')
import ai,arbitration_api as api


class LegalFixTests(unittest.TestCase):
    def test_names_exclude_profile_decoration_and_business_text(self):
        for source,wanted in [('Сергей | ДВК Право','Сергей'),('🌴 Павел Петров','Павел'),
                              ('Александра • закупки','Александра'),('ДВК Право Сергей','Сергей')]:
            self.assertEqual(ai.profile_first_name(source),wanted)
        with self.assertRaises(ValueError):ai.profile_first_name('ДВК | Право')

    def test_ip_name_prefix_does_not_discard_confirmed_inn(self):
        search=[dict(Inn='123456789012',Name='ИП Семёнов Иван Иванович')]
        detail=[dict(Name='Семенов Иван Иванович')]
        self.assertIs(api._find_party_by_identity(search,detail,'123456789012'),detail[0])
        self.assertIsNone(api._find_party_by_identity(search,[dict(Name='Иванов Иван Иванович')],'123456789012'))
        self.assertIsNone(api._find_party_by_identity(search,[dict(Name='Семенов Иван Иванович',Inn='999999999999')],'123456789012'))

    def test_ip_inn_available_only_in_details_is_checked(self):
        search=dict(Success=1,Cases=[dict(CaseId='test-id',CaseNumber='А00-1/2026',
                    Respondents=[dict(Name='ИП Тестовый Иван Иванович')])])
        details=dict(Success=1,Cases=[dict(CaseId='test-id',CaseNumber='А00-1/2026',
                     Respondents=[dict(Name='Тестовый Иван Иванович',Inn='123456789012')])])
        with patch('config.PARSER_API_KEY','fixture'),patch.object(api,'_api_get',side_effect=[search,details]):
            result=api.find_latest_defendant_case('123456789012')
        self.assertEqual(result['status'],'CONFIRMED')
        self.assertEqual(result['case']['defendant_inn'],'123456789012')
        details['Cases'][0]['Respondents'][0].pop('Inn')
        with patch('config.PARSER_API_KEY','fixture'),patch.object(api,'_api_get',side_effect=[search,details]):
            self.assertEqual(api.find_latest_defendant_case('123456789012')['status'],'NOT_FOUND')

    def test_legal_prompt_uses_actual_sender_not_fixed_sergey(self):
        root=Path(ai.__file__).resolve().parent
        catalog=ai.get_catalog()
        response=Mock();response.responses.create.return_value=SimpleNamespace(
            output_text='ГОТОВОЕ СООБЩЕНИЕ\nМеня зовут Павел, ООО ДВК Право.\nКОНЕЦ СООБЩЕНИЯ')
        with patch('ai.get_catalog',return_value=catalog),patch('ai.OpenAI',return_value=response),\
             patch('config.OPENAI_API_KEY','fixture'),patch('arbitration_api.find_latest_defendant_case',return_value={
                 'status':'CONFIRMED','case':{'case_number':'А00-1/2026'}}):
            for product,inn in [('Право_ИП','123456789012'),('Право_ООО','1234567890')]:
                result=ai.generate_company_analysis(inn,'Тестовый Клиент','test',product,sender_name='Павел | ДВК')
                self.assertIn('ПРОВЕРЕННОЕ ИМЯ ОТПРАВИТЕЛЯ: Павел',result['prompt_text'])
                self.assertNotIn('Сергей',result['prompt_text'])
                self.assertNotIn('[ИМЯ ОТПРАВИТЕЛЯ]',result['prompt_text'])
        text=(root/'prompts/pravo_ip.md').read_text('utf-8')
        self.assertNotIn('действующего директора',text)
        self.assertIn('12-значным ИНН',text)
