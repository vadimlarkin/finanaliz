import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'skills/borrower-financial-analysis/scripts/calculate_loan_schedule.py'


def case():
    return {
        'schema_version':'local-loan-schedule-1.0','currency':'RUB',
        'issue_date':'2024-01-01','amount':'1000.00','term_months':'1',
        'rate':{'fixed':True,'meaning':'borrower_nominal_annual_percent','value':'36.5'},
        'accrual':{'interest_type':'simple','day_count':'actual_365_fixed','issue_day':'exclude','repayment_day':'include'},
        'repayments':[{'date':'2024-01-11','amount':'1000.00'}],
        'interest_payment_dates':['2024-01-11'],
        'fees':[{'id':'F1','date':'2024-01-01','calculation':'percent','rate_pct':'2','base':'original_principal'}],
        'rounding':{'mode':'half_up','interest_stage':'payment'},
        'first_12_months':{'end':'2025-01-01','include_issue_day':True,'include_end_day':False,'cash_include_issue_day':True,'cash_include_end_day':False},
        'sources':[{'id':'CONTRACT','file':'synthetic-contract.md','locator':'условия графика'}],
        'terms_sources':['CONTRACT'],
    }


class LoanScheduleCLI(unittest.TestCase):
    def run_schedule(self, data):
        with tempfile.TemporaryDirectory() as name:
            source=Path(name)/'input.json'; output=Path(name)/'result.json'
            source.write_text(json.dumps(data,ensure_ascii=False))
            before=source.read_bytes()
            result=subprocess.run([sys.executable,str(SCRIPT),str(source),str(output)],capture_output=True,text=True)
            self.assertEqual(source.read_bytes(),before)
            self.assertTrue(output.exists(),result.stderr)
            return result.returncode,json.loads(output.read_text())

    def test_bullet_keeps_principal_interest_and_fees_separate(self):
        code,result=self.run_schedule(case())
        self.assertEqual(code,0,result)
        self.assertEqual(result['totals'],{'principal':'1000.00','interest':'10.00','fees':'20.00','payment':'1030.00'})
        self.assertEqual(result['schedule'],[
            {'date':'2024-01-01','principal':'0.00','interest':'0.00','fees':'20.00','payment':'20.00','principal_remaining':'1000.00'},
            {'date':'2024-01-11','principal':'1000.00','interest':'10.00','fees':'0.00','payment':'1010.00','principal_remaining':'0.00'}])

    def test_partial_repayment_changes_accrual_balance_on_explicit_boundary(self):
        data=case();data['fees']=[]
        data['repayments']=[{'date':'2024-01-06','amount':'400.00'},{'date':'2024-01-11','amount':'600.00'}]
        for boundary,expected in [('include','8.00'),('exclude','7.00')]:
            with self.subTest(boundary=boundary):
                data['accrual']['repayment_day']=boundary
                code,result=self.run_schedule(data)
                self.assertEqual(code,0,result)
                self.assertEqual(result['totals']['interest'],expected)
                self.assertEqual(result['schedule'][-1]['principal_remaining'],'0.00')

    def test_leap_year_and_issue_day_follow_explicit_day_count(self):
        data=case();data.update(issue_date='2024-02-28',repayments=[{'date':'2024-03-01','amount':'1000.00'}],interest_payment_dates=['2024-03-01'],fees=[])
        data['rate']['value']='36.6'
        data['first_12_months']['end']='2025-02-28'
        for basis,issue_day,expected in [('actual_actual','exclude','2.00'),('actual_365_fixed','exclude','2.01'),('actual_366_fixed','include','3.00')]:
            with self.subTest(basis=basis,issue_day=issue_day):
                data['accrual'].update(day_count=basis,issue_day=issue_day)
                code,result=self.run_schedule(data)
                self.assertEqual(code,0,result)
                self.assertEqual(result['totals']['interest'],expected)

    def test_rounding_stage_and_mode_are_contractual(self):
        data=case();data.update(amount='1.00',repayments=[{'date':'2024-01-06','amount':'1.00'}],interest_payment_dates=['2024-01-06'],fees=[])
        for stage,mode,expected in [('payment','half_up','0.01'),('payment','half_even','0.00'),('payment','down','0.00'),('daily','half_up','0.00')]:
            with self.subTest(stage=stage,mode=mode):
                data['rounding'].update(interest_stage=stage,mode=mode)
                code,result=self.run_schedule(data)
                self.assertEqual(code,0,result)
                self.assertEqual(result['totals']['interest'],expected)

    def test_fee_base_and_collection_date_use_explicit_contract(self):
        data=case();data['repayments']=[{'date':'2024-01-06','amount':'400.00'},{'date':'2024-01-11','amount':'600.00'}]
        data['fees']=[{'id':'FIX','date':'2024-01-01','calculation':'fixed','amount':'3.01'},
                      {'id':'BEFORE','date':'2024-01-06','calculation':'percent','rate_pct':'2','base':'outstanding_before_repayment'},
                      {'id':'AFTER','date':'2024-01-06','calculation':'percent','rate_pct':'2','base':'outstanding_after_repayment'}]
        code,result=self.run_schedule(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['totals']['fees'],'35.01')
        self.assertEqual(result['schedule'][1]['fees'],'32.00')
        self.assertEqual(result['fees'][2]['base_amount'],'600.00')

    def test_first_twelve_months_separate_accrued_interest_from_dated_payments(self):
        data=case();data.update(issue_date='2025-01-01',term_months='18',repayments=[{'date':'2026-07-01','amount':'1000.00'}],interest_payment_dates=['2026-07-01'])
        data['fees']=[{'id':'UPFRONT','date':'2025-01-01','calculation':'fixed','amount':'10.00'},
                      {'id':'FINAL','date':'2026-07-01','calculation':'fixed','amount':'5.00'}]
        data['first_12_months']={'end':'2026-01-01','include_issue_day':False,'include_end_day':True,'cash_include_issue_day':True,'cash_include_end_day':False}
        code,result=self.run_schedule(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['totals']['interest'],'546.00')
        self.assertEqual(result['totals']['fees'],'15.00')
        self.assertEqual(result['first_12_months'],{'start':'2025-01-01','end':'2026-01-01','include_issue_day':False,'include_end_day':True,
            'cash_include_issue_day':True,'cash_include_end_day':False,'interest_accrued':'365.00','interest_paid':'0.00','fees':'10.00','principal_paid':'0.00','payments':'10.00'})
        self.assertEqual(result['corporate_patch']['loan_repayment'],'schedule')
        self.assertEqual(result['corporate_patch']['facts']['new_loan_interest_12m']['value'],365)
        self.assertEqual(result['corporate_patch']['facts']['new_loan_fees_12m']['value'],10)
        code,short=self.run_schedule(case())
        self.assertEqual(short['first_12_months']['fees'],'20.00')

    def test_missing_or_unsupported_material_terms_stop_without_calculated_amounts(self):
        examples=[]
        data=case();del data['accrual']['day_count'];examples.append((data,'accrual.day_count'))
        data=case();data['rate']['meaning']='investor_yield';examples.append((data,'rate.meaning'))
        data=case();data['rate']['fixed']=False;examples.append((data,'rate.fixed'))
        data=case();data['accrual']['interest_type']='compound';examples.append((data,'accrual.interest_type'))
        data=case();data['accrual']['day_count']='30_360';examples.append((data,'accrual.day_count'))
        data=case();del data['fees'];examples.append((data,'fees'))
        data=case();data['rounding']['interest_stage']='monthly';examples.append((data,'rounding.interest_stage'))
        for data,field in examples:
            with self.subTest(field=field):
                code,result=self.run_schedule(data)
                self.assertEqual(code,2,result)
                self.assertEqual(result['errors'][0]['field'],field)
                self.assertNotIn('schedule',result)
                self.assertNotIn('corporate_patch',result)

    def test_inconsistent_schedule_and_untraceable_terms_are_rejected(self):
        examples=[]
        data=case();data['repayments']*=2;examples.append((data,'repayments'))
        data=case();data['repayments'][0]['amount']='999.99';examples.append((data,'repayments'))
        data=case();data['repayments'][0]['date']='2023-12-31';examples.append((data,'repayments.0.date'))
        data=case();data['interest_payment_dates']=['2024-01-10'];examples.append((data,'interest_payment_dates'))
        data=case();data['first_12_months']['end']='2025-02-01';examples.append((data,'first_12_months.end'))
        data=case();data['first_12_months']['include_issue_day']=False;examples.append((data,'first_12_months'))
        data=case();data['fees'][0]['base']='investor_net_proceeds';examples.append((data,'fees.0.base'))
        data=case();data['fees'][0]['rate_pct']='-2';examples.append((data,'fees.0.rate_pct'))
        data=case();data['terms_sources']=['UNKNOWN'];examples.append((data,'terms_sources'))
        data=case();data['amount']='NaN';examples.append((data,'amount'))
        data=case();data['rate']['value']=True;examples.append((data,'rate.value'))
        data=case();data['rate']['value']='-1';examples.append((data,'rate.value'))
        data=case();data['amount']='1000.001';examples.append((data,'amount'))
        for data,field in examples:
            with self.subTest(field=field):
                code,result=self.run_schedule(data)
                self.assertEqual(code,2,result)
                self.assertEqual(result['errors'][0]['field'],field)
                self.assertNotIn('corporate_patch',result)

    def test_corporate_cli_accepts_patch_and_keeps_commissions_out_of_interest_only_ratio(self):
        code,schedule=self.run_schedule(case())
        self.assertEqual(code,0,schedule)
        data=json.loads((ROOT/'examples/investor.json').read_text())
        patch=schedule['corporate_patch']
        data.update({key:patch[key] for key in ('loan_repayment','loan_interest_basis')})
        data['facts'].update(patch['facts'])
        data['sources'].extend(schedule['sources'])
        with tempfile.TemporaryDirectory() as name:
            source=Path(name)/'corporate.json';output=Path(name)/'audit.json'
            source.write_text(json.dumps(data,ensure_ascii=False))
            result=subprocess.run([sys.executable,str(ROOT/'skills/borrower-financial-analysis/scripts/analyze.py'),str(source),str(output)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            audit=json.loads(output.read_text())
        self.assertEqual(audit['loan_comparison']['new_interest_only_12m'],10.0)
        coverage=next(metric for metric in audit['metrics'] if metric['id']=='new_loan_coverage')
        self.assertAlmostEqual(coverage['value'],1.999992500028125)
        self.assertAlmostEqual(audit['loan_comparison']['rows'][0]['after']['value'],2.399996800004267)

    def test_large_values_cannot_silently_lose_kopecks_in_corporate_numeric_facts(self):
        data=case();data['amount']='9007199254740992.01';data['repayments'][0]['amount']=data['amount']
        code,result=self.run_schedule(data)
        self.assertEqual(code,2,result)
        self.assertEqual(result['errors'][0]['field'],'corporate_patch')
        self.assertNotIn('corporate_patch',result)

    def test_unrecognized_contract_fields_are_not_silently_ignored(self):
        for path in ['investor_yield','rate.margin','rounding.increment','accrual.capitalization','fees']:
            data=case()
            if path=='fees':
                data['fees'][0]['amount']='99.00';expected='fees.0'
            elif '.' in path:
                section,key=path.split('.');data[section][key]='unsupported';expected=path
            else:
                data[path]='28';expected=path
            with self.subTest(path=path):
                code,result=self.run_schedule(data)
                self.assertEqual(code,2,result)
                self.assertEqual(result['errors'][0]['field'],expected)

    def test_payment_rounding_is_retained_in_twelve_month_expense(self):
        data=case();data.update(amount='1.00',repayments=[{'date':'2024-01-11','amount':'1.00'}],interest_payment_dates=['2024-01-06','2024-01-11'],fees=[])
        code,result=self.run_schedule(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['totals']['interest'],'0.02')
        self.assertEqual(result['first_12_months']['interest_accrued'],'0.02')
        self.assertEqual(result['corporate_patch']['facts']['new_loan_interest_12m']['value'],0.02)

    def test_calendar_year_switch_and_explicit_leap_anniversary(self):
        data=case();data.update(issue_date='2023-12-30',repayments=[{'date':'2024-01-02','amount':'1000.00'}],interest_payment_dates=['2024-01-02'],fees=[])
        data['accrual']['day_count']='actual_actual';data['first_12_months']['end']='2024-12-30'
        code,result=self.run_schedule(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['totals']['interest'],'2.99')
        data=case();data.update(issue_date='2024-02-29',repayments=[{'date':'2025-03-01','amount':'1000.00'}],interest_payment_dates=['2025-03-01'],fees=[])
        data['rate']['value']='36.6';data['accrual']['day_count']='actual_366_fixed'
        for end,expected in [('2025-02-28','365.00'),('2025-03-01','366.00')]:
            data['first_12_months']={'end':end,'include_issue_day':False,'include_end_day':True,'cash_include_issue_day':True,'cash_include_end_day':False}
            code,result=self.run_schedule(data)
            self.assertEqual(code,0,result)
            self.assertEqual(result['first_12_months']['interest_accrued'],expected)

    def test_identical_input_has_identical_output_bytes_and_source_provenance(self):
        with tempfile.TemporaryDirectory() as name:
            source=Path(name)/'input.json';output=Path(name)/'result.json'
            source.write_text(json.dumps(case(),ensure_ascii=False))
            command=[sys.executable,str(SCRIPT),str(source),str(output)]
            first=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(first.returncode,0,first.stderr)
            previous=output.read_bytes()
            second=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(second.returncode,0,second.stderr)
            self.assertEqual(output.read_bytes(),previous)
            result=json.loads(previous)
            self.assertEqual(result['terms'],case())
            self.assertEqual(result['sources'],case()['sources'])

    def test_duplicate_json_condition_does_not_select_one_value_silently(self):
        with tempfile.TemporaryDirectory() as name:
            source=Path(name)/'input.json';output=Path(name)/'result.json'
            encoded=json.dumps(case(),ensure_ascii=False).replace('"amount": "1000.00"','"amount": "999.00", "amount": "1000.00"',1)
            source.write_text(encoded)
            result=subprocess.run([sys.executable,str(SCRIPT),str(source),str(output)],capture_output=True,text=True)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertEqual(json.loads(output.read_text())['errors'][0]['field'],'json')

    def test_origination_fee_uses_cash_window_separately_from_interest_accrual_boundaries(self):
        data=case();data.update(issue_date='2025-01-01',repayments=[{'date':'2026-07-01','amount':'1000.00'}],interest_payment_dates=['2026-07-01'],term_months='18')
        data['fees'][0]['date']='2025-01-01'
        data['first_12_months']={'end':'2026-01-01','include_issue_day':False,'include_end_day':True,
                                 'cash_include_issue_day':True,'cash_include_end_day':False}
        code,result=self.run_schedule(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['first_12_months']['interest_accrued'],'365.00')
        self.assertEqual(result['first_12_months']['fees'],'20.00')
