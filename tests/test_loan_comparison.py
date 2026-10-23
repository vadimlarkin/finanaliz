import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'skills/borrower-financial-analysis/scripts'))
from analyze import analyze, InputError
from render_dashboard import render
from pypdf import PdfReader


class LoanComparison(unittest.TestCase):
    def case(self):
        return json.loads((ROOT/'examples/investor.json').read_text())

    def test_same_baseline_earnings_and_equity_are_used_before_and_after_full_drawdown(self):
        data=self.case()
        data['report'].pop('current_debt',None)
        original=copy.deepcopy(data)
        audit=analyze(data)
        rows={r['id']:r for r in audit['loan_comparison']['rows']}
        # LTM: 18 / 7.5; 18 / 8.7. Legacy debt fallback: 27 / 25; 32 / 25.
        self.assertEqual(rows['interest_coverage']['before']['value'],2.4)
        self.assertAlmostEqual(rows['interest_coverage']['after']['value'],2.0689655172413794)
        self.assertEqual(rows['leverage']['before']['value'],1.08)
        self.assertEqual(rows['leverage']['after']['value'],1.28)
        self.assertEqual(rows['interest_coverage']['after']['display'],'2,07')
        self.assertEqual(rows['leverage']['after']['display'],'1,28')
        self.assertEqual(len(audit['metrics']),5)
        self.assertEqual(next(m['value'] for m in audit['metrics'] if m['id']=='cash_coverage'),1.14)
        self.assertEqual(data,original)

    def test_current_principal_excludes_new_loan_and_related_is_not_counted_twice(self):
        data=self.case()
        data['report']['current_debt']={'as_of':'2026-10-22','facts':{
            k:{'value':v,'status':'confirmed','sources':['S1'],'as_of':'2026-10-22'}
            for k,v in [('bank_principal',15000000),('nonbank_principal',10000000),
                        ('related_party_principal',4000000),('interest_bearing_debt',25000000),('equity',30000000)]}}
        for new_amount in [5000000,9000000]:
            data['facts']['new_loan_amount']['value']=new_amount
            result=analyze(data)
            debt=result['report']['current_debt']
            self.assertEqual(debt['facts']['bank_principal']['value'],15000000)
            self.assertEqual(debt['facts']['unrelated_nonbank_principal']['value'],6000000)
            self.assertEqual(debt['facts']['related_party_principal']['value'],4000000)
            ratio=result['loan_comparison']['rows'][1]
            self.assertEqual(ratio['as_of'],'2026-10-22')
            self.assertAlmostEqual(ratio['before']['value'],25/30)
            self.assertAlmostEqual(ratio['after']['value'],(25000000+new_amount)/30000000)

    def test_missing_related_principal_is_not_treated_as_zero(self):
        data=self.case()
        data['report']['current_debt']={'as_of':'2026-10-22','facts':{
            'nonbank_principal':{'value':10000000,'status':'confirmed','sources':['S1'],'as_of':'2026-10-22'}}}
        result=analyze(data)
        self.assertIsNone(result['report']['current_debt']['facts']['unrelated_nonbank_principal']['value'])
        self.assertIsNone(result['loan_comparison']['rows'][1]['before']['value'])

    def test_current_debt_requires_same_date_and_related_cannot_exceed_gross_nonbank(self):
        for wrong in ['date','excess','source','future']:
            data=self.case()
            data['report']['current_debt']={'as_of':'2026-10-22','facts':{
                k:{'value':v,'status':'confirmed','sources':['S1'],'as_of':'2026-10-22'}
                for k,v in [('nonbank_principal',100),('related_party_principal',40)]}}
            current=data['report']['current_debt']
            if wrong=='date':current['facts']['nonbank_principal']['as_of']='2026-09-30'
            if wrong=='excess':current['facts']['related_party_principal']['value']=101
            if wrong=='source':current['facts']['nonbank_principal']['sources']=['UNKNOWN']
            if wrong=='future':current['as_of']='2026-10-23'
            with self.subTest(wrong=wrong),self.assertRaises(InputError):analyze(data)

    def test_commissions_stay_internal_and_do_not_enter_the_interest_only_row(self):
        data=self.case()
        data['facts']['new_loan_fees_12m']['value']=1000000
        result=analyze(data)
        self.assertAlmostEqual(result['loan_comparison']['rows'][0]['after']['value'],2.0689655172413794)
        self.assertAlmostEqual(next(m['value'] for m in result['metrics'] if m['id']=='new_loan_coverage'),16/10.2)

    def test_interest_free_new_loan_does_not_increase_interest_bearing_debt(self):
        data=self.case()
        data['facts']['new_loan_rate_pct']['value']=0
        ratio=analyze(data)['loan_comparison']['rows'][1]
        self.assertEqual(ratio['before']['value'],ratio['after']['value'])

    def test_zero_rate_and_positive_scheduled_interest_cannot_describe_the_same_loan(self):
        data=self.case()
        data['loan_repayment']='schedule'
        data['loan_interest_basis']='schedule'
        data['facts']['new_loan_rate_pct']['value']=0
        data['facts']['new_loan_interest_12m']={'value':1200000,'status':'confirmed','sources':['S1']}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.pdf'
            path.write_bytes(b'previous report')
            with self.assertRaises(InputError):render(data,path)
            self.assertEqual(path.read_bytes(),b'previous report')

    def test_conflicting_comparison_facts_keep_the_conflict_in_pdf_and_audit(self):
        for group,key in [('interest_ltm','interest_expense'),('current_debt','interest_bearing_debt'),('current_debt','equity')]:
            data=self.case()
            data['report'][group]['facts'][key]['status']='conflicting'
            with self.subTest(group=group,key=key),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'report.pdf'
                result=render(data,path)
                row=result['loan_comparison']['rows'][0 if group=='interest_ltm' else 1]
                for cell in (row['before'],row['after']):
                    self.assertIsNone(cell['value'])
                    self.assertEqual(cell['status'],'conflicting')
                    self.assertEqual(cell['display'],'Противоречие')
                    self.assertIn(key,cell['conflicting'])
                self.assertIn('Противоречие',PdfReader(path).pages[0].extract_text())

    def test_same_date_equity_cannot_disagree_with_financial_history(self):
        data=self.case()
        current=data['report']['current_debt']
        current['as_of']='2026-09-30'
        for fact in current['facts'].values():fact['as_of']='2026-09-30'
        current['facts']['equity']['value']=31000000
        with self.assertRaises(InputError):analyze(data)

    def test_unconfirmed_current_debt_is_not_substituted_with_old_confirmed_debt(self):
        data=self.case()
        data['report']['current_debt']['facts']['interest_bearing_debt']['status']='unverified'
        result=analyze(data)
        ratio=result['loan_comparison']['rows'][1]
        self.assertEqual(ratio['before']['display'],'Нет данных')
        self.assertEqual(ratio['after']['display'],'Нет данных')
        self.assertEqual(next(m['value'] for m in result['metrics'] if m['id']=='leverage'),1.08)

    def test_pdf_uses_formula_rows_and_two_numeric_columns_without_internal_cash_metric(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.pdf'
            audit=render(self.case(),path)
            pdf=PdfReader(path)
            self.assertEqual(len(pdf.pages),1)
            text=pdf.pages[0].extract_text()
            for label in ['Прибыль от продаж / проценты','Процентный долг / капитал','До займа','После займа','2,40','2,07',
                          'Кредитная нагрузка**','Банковские','Небанковские, без связанных','Связанные',
                          'без учёта рассматриваемого нового займа']:
                self.assertIn(label,text)
            for label in ['Денежное покрытие','Покрытие процентов','Долговая нагрузка','После нового займа','×']:
                self.assertNotIn(label,text)
            self.assertEqual(next(m['value'] for m in audit['metrics'] if m['id']=='cash_coverage'),1.14)
