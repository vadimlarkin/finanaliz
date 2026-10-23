import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'skills/borrower-financial-analysis/scripts'))
from analyze import analyze, InputError
from render_dashboard import render
from pypdf import PdfReader


class TrailingInterest(unittest.TestCase):
    def case(self):
        data = json.loads((ROOT/'examples/investor.json').read_text())
        period = {'start':'2025-10-01', 'end':'2026-09-30'}
        data['report']['interest_ltm'] = {'period':period, 'facts':{
            key:{'value':value, 'status':'calculated', 'sources':['S1'],
                 'period_start':period['start'], 'period_end':period['end'],
                 'derivation':calculation}
            for key,value,calculation in [
                ('operating_profit',18000000,'16 000 000 - 10 000 000 + 12 000 000'),
                ('interest_expense',7500000,'8 000 000 - 4 500 000 + 4 000 000'),
            ]}}
        return data

    def test_pdf_compares_latest_twelve_months_without_reusing_the_old_annual_profit(self):
        data = self.case()
        original = copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.pdf'
            result = render(data, path)
            row = result['loan_comparison']['rows'][0]
            self.assertEqual(row['period'],{'start':'2025-10-01','end':'2026-09-30'})
            self.assertEqual(row['before']['value'],2.4)
            self.assertAlmostEqual(row['after']['value'],2.0689655172413794)
            text = PdfReader(path).pages[0].extract_text()
            self.assertIn('Последние 12 мес.', text)
            self.assertIn('01.10.2025', text)
            self.assertIn('30.09.2026', text)
            self.assertIn('2,40', text)
            self.assertIn('2,07', text)
            self.assertEqual(next(m['value'] for m in result['metrics'] if m['id']=='interest_coverage'),2)
        self.assertEqual(data, original)

    def test_missing_ltm_does_not_fall_back_to_last_year_or_annualize_nine_months(self):
        for missing in ['block','profit','interest','unverified']:
            data=self.case()
            if missing=='block':data['report'].pop('interest_ltm')
            elif missing=='unverified':data['report']['interest_ltm']['facts']['interest_expense']['status']='unverified'
            else:data['report']['interest_ltm']['facts'].pop('operating_profit' if missing=='profit' else 'interest_expense')
            row=analyze(data)['loan_comparison']['rows'][0]
            with self.subTest(missing=missing):
                self.assertEqual(row['before']['display'],'Нет данных')
                self.assertEqual(row['after']['display'],'Нет данных')

    def test_invalid_or_outdated_ltm_evidence_preserves_the_existing_pdf(self):
        for wrong in ['nine_months','day_count','stale','future','fact_period','source','derivation']:
            data=self.case()
            ltm=data['report']['interest_ltm']
            if wrong=='nine_months':ltm['period']['start']='2026-01-01'
            if wrong=='day_count':ltm['period']={'start':'2025-10-01','end':'2026-10-01'}
            if wrong=='stale':ltm['period']={'start':'2025-07-01','end':'2026-06-30'}
            if wrong=='future':ltm['period']={'start':'2025-11-01','end':'2026-10-31'}
            for fact in ltm['facts'].values():fact.update(period_start=ltm['period']['start'],period_end=ltm['period']['end'])
            if wrong=='fact_period':ltm['facts']['interest_expense']['period_start']='2026-01-01'
            if wrong=='source':ltm['facts']['interest_expense']['sources']=['UNKNOWN']
            if wrong=='derivation':ltm['facts']['operating_profit'].pop('derivation')
            with self.subTest(wrong=wrong),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'report.pdf'
                path.write_bytes(b'previous report')
                with self.assertRaises(InputError):render(data,path)
                self.assertEqual(path.read_bytes(),b'previous report')

    def test_zero_interest_remains_not_applicable_with_a_reason(self):
        data=self.case()
        data['report']['interest_ltm']['facts']['interest_expense']['value']=0
        row=analyze(data)['loan_comparison']['rows'][0]
        self.assertEqual(row['before']['display'],'Не применимо')
        self.assertEqual(row['before']['reason'],'Проценты равны нулю')
        self.assertEqual(row['after']['value'],15)

    def test_ltm_cannot_disagree_with_confirmed_facts_for_the_exact_same_period(self):
        data=self.case()
        data['report'].pop('financial_history')
        ltm=data['report']['interest_ltm']
        ltm['period']=dict(data['period'])
        for fact in ltm['facts'].values():fact.update(period_start='2025-01-01',period_end='2025-12-31')
        with self.assertRaises(InputError):analyze(data)

    def test_intermediary_uses_the_same_ltm_for_its_adjusted_earnings(self):
        data=self.case()
        data['company']['business_model']='financial_intermediary'
        # Root facts needed by the unchanged internal analysis.
        for key,value in [('profit_before_tax',4000000),('one_off_net_income',0),('portfolio_net_cashflow',0)]:
            data['facts'][key]={'value':value,'status':'confirmed','sources':['S1'],
                                'period_start':'2025-01-01','period_end':'2025-12-31'}
        facts=data['report']['interest_ltm']['facts']
        for key,value in [('profit_before_tax',4500000),('one_off_net_income',1500000)]:
            facts[key]=dict(facts['operating_profit'],value=value)
        row=analyze(data)['loan_comparison']['rows'][0]
        self.assertEqual(row['before']['value'],1.4)  # (4.5 + 7.5 - 1.5) / 7.5
        self.assertAlmostEqual(row['after']['value'],1.206896551724138)

    def test_month_end_leap_year_is_twelve_months_even_with_366_days(self):
        data=self.case()
        data['period']={'start':'2023-01-01','end':'2023-12-31'}
        data['balance_date']='2023-12-31'
        for key,fact in data['facts'].items():
            if 'period_start' in fact:fact.update(period_start='2023-01-01',period_end='2023-12-31')
            if 'as_of' in fact and key!='unrestricted_cash':fact['as_of']='2023-12-31'
        ltm=data['report']['interest_ltm']
        data['report']={'interest_ltm':ltm}
        for start,end in [('2023-03-01','2024-02-29'),('2024-03-01','2025-02-28')]:
            ltm['period']={'start':start,'end':end}
            for fact in ltm['facts'].values():fact.update(period_start=start,period_end=end)
            with self.subTest(end=end):self.assertEqual(analyze(data)['loan_comparison']['rows'][0]['before']['value'],2.4)
