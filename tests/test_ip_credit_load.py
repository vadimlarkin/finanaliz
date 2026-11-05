import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'skills/borrower-financial-analysis/scripts'))
import ip_investor_report
import render_ip_dashboard


class CreditLoad(unittest.TestCase):
    def case(self):
        data = json.loads((ROOT/'examples/ip-investor-v3.json').read_text())
        data['schema_version'] = 'ip-investor-3.1'
        data['loan']['term_months']['value'] = 3
        data['credit_load'] = {
            'start_month': '2026-12', 'start_basis': 'user_scenario', 'start_sources': ['USER'],
            'contracts': [
                {'id':'monthly', 'monthly_payment':{'value':1000,'status':'confirmed','sources':['B']},
                 'payments':[{'id':'replacement','date':'2026-12-15','treatment':'replace',
                              'amount':{'value':1200,'status':'confirmed','sources':['B']}}]},
                {'id':'bullet', 'monthly_payment':None,
                 'payments':[{'id':'bullet-1','date':'2027-01-20','treatment':'additional',
                              'amount':{'value':3500,'status':'confirmed','sources':['B']}}]}
            ]}
        # Source IDs are the fixture's existing public evidence references.
        ids = {s['id'] for s in data['sources']}
        if 'USER' not in ids:
            data['sources'].append({'id':'USER','file':'synthetic-loan.md','locator':'условия сценария'})
        return data

    def test_term_sets_months_and_exact_payment_replaces_same_month_baseline(self):
        data = self.case(); original = copy.deepcopy(data)
        audit = ip_investor_report.prepare(data)
        load = audit['credit_load']
        self.assertEqual(load['period'], {'start_month':'2026-12','end_month':'2027-02'})
        self.assertEqual([row['month'] for row in load['months']], ['2026-12','2027-01','2027-02'])
        self.assertEqual([row['included_rub'] for row in load['months']], ['1200.00','4500.00','1000.00'])
        self.assertEqual(load['total_rub'], '6700.00')
        self.assertEqual(load['regular_monthly_rub'], '1000.00')
        self.assertEqual(load['regular_contracts'], 1)
        self.assertEqual(data, original)

    def test_pdf_uses_the_specific_loan_period_and_groups_long_calendar_without_losing_total(self):
        data = self.case()
        data['loan']['term_months']['value'] = 18
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'report.pdf'
            audit = render_ip_dashboard.render(data, output)
            pdf = PdfReader(output)
            text = pdf.pages[0].extract_text()
            self.assertEqual(len(pdf.pages), 1)
            self.assertIn('Анализ финансового состояния', text)
            self.assertIn('Расчёт кредитной нагрузки до мая 2028 года', text)
            self.assertIn('18 месяцев с декабря 2026 по май 2028.', text)
            self.assertIn('Всего за 18 месяцев', text)
            self.assertIn('0,022 млн ₽', text)
            self.assertNotIn('наш возврат', text.lower())
            self.assertEqual(audit['credit_load']['total_rub'], '21700.00')
            self.assertEqual(len(audit['credit_load']['months']), 18)
            self.assertEqual(len(audit['credit_load']['calendar']), 6)

    def test_contract_end_without_any_payment_evidence_cannot_create_zero_future_payments(self):
        data = self.case()
        data['credit_load']['contracts'] = [{'id':'unknown','monthly_payment':None,'monthly_until':'2026-11','payments':[]}]
        load = ip_investor_report.prepare(data)['credit_load']
        self.assertIsNone(load['total_rub'])
        self.assertTrue(all(row['included_rub'] is None for row in load['months']))

    def test_unknown_and_explicit_zero_remain_distinct_and_term_can_be_unknown(self):
        data = self.case()
        data['credit_load']['contracts'] = [{'id':'unknown','monthly_payment':None}]
        missing = ip_investor_report.prepare(data)['credit_load']
        self.assertEqual(missing['status'], 'missing')
        self.assertIsNone(missing['total_rub'])
        data['credit_load']['contracts'][0]['monthly_payment'] = {'value':0,'status':'confirmed','sources':['B']}
        zero = ip_investor_report.prepare(data)['credit_load']
        self.assertEqual(zero['total_rub'], '0.00')
        data['loan']['term_months'] = None
        no_term = ip_investor_report.prepare(data)['credit_load']
        self.assertIsNone(no_term['period']['end_month'])
        self.assertIsNone(no_term['total_rub'])

    def test_duplicate_contracts_payments_or_unknown_sources_preserve_previous_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'report.pdf'
            render_ip_dashboard.render(self.case(), path)
            old = path.read_bytes()
            for defect in ('contract','payment','source','period'):
                with self.subTest(defect=defect):
                    data = self.case()
                    if defect=='contract':data['credit_load']['contracts'].append(copy.deepcopy(data['credit_load']['contracts'][0]))
                    elif defect=='payment':data['credit_load']['contracts'][0]['payments'] *= 2
                    elif defect=='source':data['credit_load']['contracts'][0]['monthly_payment']['sources']=['MISSING']
                    else:data['credit_load']['start_month']='2026-13'
                    with self.assertRaises(render_ip_dashboard.InputError):
                        render_ip_dashboard.render(data,path)
                    self.assertEqual(path.read_bytes(),old)

    def test_payments_outside_window_are_not_charged_and_additional_keeps_the_monthly_base(self):
        data = self.case()
        contract = data['credit_load']['contracts'][0]
        data['credit_load']['contracts'] = [contract]
        contract['payments'][0]['treatment']='additional'
        contract['payments'].append({'id':'outside','date':'2027-03-01','treatment':'additional',
                                    'amount':{'value':9000,'status':'confirmed','sources':['B']}})
        load = ip_investor_report.prepare(data)['credit_load']
        self.assertEqual([r['included_rub'] for r in load['months']],['2200.00','1000.00','1000.00'])
        self.assertEqual(load['total_rub'],'4200.00')
        self.assertEqual(len(load['contracts'][0]['payments']),2)

    def test_assessment_month_fallback_and_confirmed_final_regular_month_are_applied(self):
        data = self.case()
        data['credit_load'].pop('start_month');data['credit_load'].pop('start_basis');data['credit_load'].pop('start_sources')
        contract = data['credit_load']['contracts'][0]
        data['credit_load']['contracts']=[contract]
        contract['payments']=[];contract['monthly_until']='2026-11'
        load = ip_investor_report.prepare(data)['credit_load']
        self.assertEqual(load['period'],{'start_month':'2026-10','end_month':'2026-12'})
        self.assertEqual([r['included_rub'] for r in load['months']],['1000.00','1000.00','0.00'])
        self.assertEqual(load['total_rub'],'2000.00')

    def test_calendar_grouping_preserves_every_month_for_long_terms(self):
        for term,columns in [(1,1),(12,12),(13,5),(37,4),(145,7)]:
            with self.subTest(term=term):
                data=self.case();data['loan']['term_months']['value']=term
                contract=data['credit_load']['contracts'][0]
                contract['payments']=[];data['credit_load']['contracts']=[contract]
                load=ip_investor_report.prepare(data)['credit_load']
                self.assertEqual(len(load['months']),term)
                self.assertEqual(len(load['calendar']),columns)
                self.assertEqual(load['calendar'][-1]['end_month'],load['period']['end_month'])
                self.assertEqual(load['total_rub'],f'{term*1000}.00')

    def test_pdf_loan_and_scenario_month_words_match_short_terms(self):
        for term,words in [(1,'1 месяц'),(3,'3 месяца')]:
            data=self.case();data['loan']['term_months']['value']=term
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'report.pdf';render_ip_dashboard.render(data,path)
                text=PdfReader(path).pages[0].extract_text()
                self.assertIn(' /  '+words+'\n',text)

    def test_planned_or_user_start_requires_an_explicit_month(self):
        for basis in ('planned_disbursement','user_scenario'):
            data=self.case();data['credit_load']['start_basis']=basis
            del data['credit_load']['start_month']
            with self.assertRaises(render_ip_dashboard.InputError):
                ip_investor_report.prepare(data)


if __name__ == '__main__':
    unittest.main()
