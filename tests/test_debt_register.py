import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'skills/borrower-financial-analysis/scripts/summarize_debt_register.py'


class DebtRegisterCLI(unittest.TestCase):
    def record(self, rid='A-new', contract='A', date='2026-10-20', **updates):
        row = {'id':rid, 'contract_id':contract, 'subject_id':'BORROWER', 'role':'borrower',
               'creditor_id':'BANK-A', 'status':'active', 'as_of':date, 'sources':['BKI'],
               'amounts':{'principal':'100.01', 'interest':'2.02', 'total':'102.03', 'overdue':'0.00', 'limit':None},
               'monthly_payment':{'amount':'10.01','as_of':date,'frequency':'monthly','sources':['BKI']},
               'payments':[], 'maturity':None}
        row.update(updates)
        return row

    def case(self):
        old=self.record('A-old',date='2026-09-20');old['amounts']['principal']='120.00';old['amounts']['total']='122.02'
        guarantor=self.record('A-guarantor',subject_id='GUARANTOR',role='guarantor')
        other=self.record('B',contract='B',date='2026-10-25',creditor_id='BANK-B')
        other['amounts'].update(principal='50.05',interest='1.01',total='51.06',overdue='5.00')
        other['monthly_payment']['amount']='20.02'
        return {'schema_version':'local-debt-register-1.0','kind':'bki','borrower_id':'BORROWER','report_date':'2026-11-01',
                'sources':[{'id':'BKI','file':'synthetic-bki.pdf','locator':'1-3'}],
                'records':[old,self.record(),guarantor,other]}

    def run_cli(self, data):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'input.json'; output=Path(temp)/'output.json'
            source.write_text(json.dumps(data,ensure_ascii=False))
            before=source.read_bytes()
            proc=subprocess.run([sys.executable,str(SCRIPT),str(source),str(output)],capture_output=True,text=True)
            self.assertEqual(source.read_bytes(),before)
            self.assertTrue(output.exists(),proc.stderr)
            return proc.returncode,json.loads(output.read_text()),output.read_bytes()

    def test_latest_records_and_guarantor_do_not_double_direct_debt(self):
        code,result,_=self.run_cli(self.case())
        self.assertEqual(code,0,result)
        self.assertEqual({k:v['value'] for k,v in result['totals'].items()},
                         {'principal':'150.06','interest':'3.03','total':'153.09','overdue':'5.00','limit':None})
        self.assertEqual(result['totals']['total']['record_ids'],['A-new','B'])
        self.assertEqual(result['record_period'],{'start':'2026-10-20','end':'2026-10-25'})
        self.assertEqual(result['other_roles'][0]['id'],'A-guarantor')

    def test_technical_zeros_and_unknown_amounts_preserve_dated_evidence(self):
        data=self.case()
        technical=self.record('A-tech',date=None)
        technical['amounts']={k:'0.00' for k in technical['amounts']}
        technical['monthly_payment']=None
        data['records'].append(technical)
        data['records'][-2]['amounts']['interest']=None
        data['records'][-2]['amounts']['total']=None
        data['records'][-2]['maturity']={'date':'9999-12-31','confirmed':False}
        code,result,_=self.run_cli(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['totals']['principal']['value'],'150.06')
        self.assertIsNone(result['totals']['total']['value'])
        self.assertEqual(result['totals']['total']['known_subtotal'],'102.03')
        self.assertEqual(result['contracts'][0]['ignored_record_ids'],['A-tech'])
        self.assertIsNone(result['contracts'][1]['confirmed_maturity'])

    def test_same_creditor_needs_explicit_merge_or_separate_decision(self):
        data=self.case();data['records'][-1]['creditor_id']='BANK-A'
        code,result,_=self.run_cli(data)
        self.assertEqual(code,2,result)
        self.assertEqual(result['unresolved_matches'][0]['contracts'],['A','B'])
        self.assertIsNone(result['totals']['total']['value'])
        decision={'contracts':['A','B'],'action':'separate','reason':'Разные проверенные договоры','sources':['BKI']}
        data['decisions']=[decision]
        code,result,_=self.run_cli(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['totals']['total']['value'],'153.09')
        self.assertEqual(result['decisions'],[decision])
        decision['action']='merge';decision['reason']='Две записи одного договора'
        code,result,_=self.run_cli(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['totals']['total']['value'],'51.06')
        self.assertEqual(result['contracts'][0]['record_ids'],['A-new','A-old','B'])

    def test_invalid_dates_money_identity_sources_and_decisions_are_diagnostic(self):
        defects=[('schema',lambda d:d.update(schema_version='unknown')),
                 ('date',lambda d:d['records'][0].update(as_of='2026-02-30')),
                 ('future',lambda d:d['records'][0].update(as_of='2026-12-01')),
                 ('amount',lambda d:d['records'][0]['amounts'].update(principal='NaN')),
                 ('precision',lambda d:d['records'][0]['amounts'].update(principal='0.001')),
                 ('negative',lambda d:d['records'][0]['amounts'].update(principal='-1')),
                 ('bool',lambda d:d['records'][0]['amounts'].update(principal=True)),
                 ('id',lambda d:d['records'][0].update(id='A-new')),
                 ('source',lambda d:d['records'][0].update(sources=['MISSING'])),
                 ('role',lambda d:d['records'][0].update(role='unknown')),
                 ('decision',lambda d:d.update(decisions=[{'contracts':['A','B'],'action':'merge'}]))]
        for name,mutate in defects:
            with self.subTest(name=name):
                data=self.case();mutate(data)
                code,result,_=self.run_cli(data)
                self.assertEqual(code,2,result)
                self.assertEqual(result['issues'][0]['code'],'invalid_input')
                self.assertTrue(result['issues'][0]['message'])

    def test_conflicting_latest_or_undated_record_cannot_become_confirmed_total(self):
        for defect in ('same-date','undated','unknown-status','impossible-components'):
            with self.subTest(defect=defect):
                data=self.case()
                if defect=='same-date':
                    row=self.record('A-conflict');row['amounts']['principal']='99.00';data['records'].append(row)
                elif defect=='undated':
                    data['records'].append(self.record('A-undated',date=None))
                elif defect=='unknown-status':
                    data['records'][1]['status']='unknown'
                else:
                    data['records'][1]['amounts']['total']='90.00'
                code,result,_=self.run_cli(data)
                self.assertEqual(code,2,result)
                self.assertTrue(result['issues'])
                self.assertIsNone(result['totals']['total']['value'])
                self.assertEqual(result['totals']['total']['known_subtotal'],'51.06')

    def test_payment_guides_and_numeric_patch_fit_current_ip_form(self):
        data=self.case()
        data['records'][1]['payments']=[{'id':'A-large','date':'2026-12-15','amount':'40.04','treatment':'replace','sources':['BKI']}]
        code,result,_=self.run_cli(data)
        self.assertEqual(code,0,result)
        guide=result['payment_guide']
        self.assertEqual(guide['regular_monthly']['value'],'30.03')
        self.assertFalse(guide['regular_monthly']['includes_dated_payments'])
        self.assertEqual(guide['dated_payments'][0]['treatment'],'replace')
        self.assertTrue(guide['dated_payments'][0]['replaces_monthly_base'])
        self.assertEqual(guide['dated_payments'][0]['base_frequency'],'monthly')
        self.assertEqual(guide['dated_payments'][0]['base_as_of'],'2026-10-20')
        sys.path.insert(0,str(ROOT/'skills/borrower-financial-analysis/scripts'))
        import ip_investor_report
        report=json.loads((ROOT/'examples/ip-investor-v3.json').read_text())
        report['schema_version']='ip-investor-3.1';report['assessment_date']='2026-11-05'
        report['sources']+=result['pdf_patch']['sources']
        report['bki']=result['pdf_patch']['bki']
        report['credit_load']={**result['pdf_patch']['credit_load'],'start_month':'2026-12','start_basis':'user_scenario','start_sources':['BKI']}
        report['loan']['term_months']['value']=3
        prepared=ip_investor_report.prepare(report)
        self.assertEqual(prepared['bki']['total']['value'],153.09)
        self.assertEqual(prepared['credit_load']['months'][0]['included_rub'],'60.06')
        self.assertEqual(prepared['credit_load']['total_rub'],'120.12')

    def test_partial_guides_and_non_bki_register_preserve_unknowns(self):
        data=self.case();data['kind']='verified_register'
        data['records'][1]['monthly_payment']['frequency']='quarterly'
        data['records'][-1]['payments']=[{'id':'B-large','date':'2026-12-15','amount':'40.04','treatment':'unknown','sources':['BKI']}]
        code,result,_=self.run_cli(data)
        self.assertEqual(code,0,result)
        guide=result['payment_guide']['regular_monthly']
        self.assertIsNone(guide['value'])
        self.assertEqual(guide['known_subtotal'],'20.02')
        self.assertEqual(guide['missing_contracts'],['A'])
        self.assertIsNone(result['payment_guide']['dated_payments'][0]['replaces_monthly_base'])
        self.assertEqual(result['pdf_patch']['bki'],{})
        self.assertIsNone(result['pdf_patch']['credit_load']['contracts'][1]['monthly_payment'])
        self.assertEqual(result['source_record_period'],{'start':'2026-09-20','end':'2026-10-25'})
        self.assertEqual(result['totals']['total']['entries'][0]['as_of'],'2026-10-20')
        self.assertEqual(result['totals']['total']['entries'][0]['sources'],['BKI'])

    def test_determinism_empty_register_and_chained_decisions(self):
        data=self.case()
        self.assertEqual(self.run_cli(data)[2],self.run_cli(data)[2])
        data['records']=[]
        code,result,_=self.run_cli(data)
        self.assertEqual(code,0,result)
        self.assertIsNone(result['totals']['total']['value'])
        data=self.case();row=self.record('C','C','2026-10-26');data['records'].append(row)
        data['decisions']=[{'contracts':members,'action':'merge','reason':'Единый договор','sources':['BKI']}
                           for members in (['A','B'],['B','C'])]
        code,result,_=self.run_cli(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['totals']['total']['value'],'102.03')
        self.assertEqual(len(result['contracts']),1)

    def test_wrong_container_types_are_diagnostic(self):
        for data in ([],None,{'schema_version':'local-debt-register-1.0','borrower_id':'B','report_date':'2026-11-01','sources':[None],'records':[]}):
            with self.subTest(data=data):
                code,result,_=self.run_cli(data)
                self.assertEqual(code,2,result)
                self.assertEqual(result['issues'][0]['code'],'invalid_input')

    def test_output_cannot_replace_or_hardlink_source(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'input.json';source.write_text(json.dumps(self.case()))
            alias=Path(temp)/'alias.json';alias.hardlink_to(source)
            before=source.read_bytes()
            for output in (source,alias):
                proc=subprocess.run([sys.executable,str(SCRIPT),str(source),str(output)],capture_output=True,text=True)
                self.assertEqual(proc.returncode,2,proc.stderr)
                self.assertEqual(source.read_bytes(),before)

    def test_guarantor_target_and_other_person_loans_are_separate(self):
        data=self.case()
        data['records'].append(self.record('OWN-GUARANTEE','C',role='guarantor'))
        data['records'].append(self.record('OTHER-LOAN','D',subject_id='OTHER'))
        code,result,_=self.run_cli(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['totals']['total']['value'],'153.09')
        self.assertEqual({r['id'] for r in result['other_roles']},{'A-guarantor','OWN-GUARANTEE','OTHER-LOAN'})

    def test_missing_input_with_existing_output_gets_json_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'missing.json';output=Path(temp)/'summary.json';output.write_text('{}')
            proc=subprocess.run([sys.executable,str(SCRIPT),str(source),str(output)],capture_output=True,text=True)
            self.assertEqual(proc.returncode,2,proc.stderr)
            self.assertEqual(json.loads(output.read_text())['issues'][0]['code'],'invalid_input')

    def test_unknown_contract_components_stay_unknown_in_current_credit_load(self):
        sys.path.insert(0,str(ROOT/'skills/borrower-financial-analysis/scripts'))
        from ip_credit_load import prepare_credit_load
        for defect in ('unknown-treatment','missing-base','undated-base','nonmonthly-base'):
            with self.subTest(defect=defect):
                row=self.record()
                row['payments']=[{'id':'extra','date':'2026-12-15','amount':'5.00','treatment':'additional','sources':['BKI']}]
                if defect=='unknown-treatment':
                    row['payments'].append({'id':'unclear','date':'2026-12-15','amount':'40.00','treatment':'unknown','sources':['BKI']})
                elif defect=='missing-base':
                    row['monthly_payment']=None
                elif defect=='undated-base':
                    row['monthly_payment']['as_of']=None
                else:
                    row['monthly_payment']['frequency']='quarterly'
                data=self.case();data['records']=[row]
                code,result,_=self.run_cli(data)
                self.assertEqual(code,0,result)
                load=prepare_credit_load({**result['pdf_patch']['credit_load'],'start_month':'2026-12','start_basis':'user_scenario','start_sources':['BKI']},1,'2026-11-05',{'BKI'})
                self.assertIsNone(load['total_rub'],load)
                self.assertEqual(load['months'][0]['unknown_contracts'],['A'])
                self.assertEqual(result['payment_guide']['dated_payments'][0]['amount'],'5.00')
                self.assertTrue(result['payment_guide']['patch_limitations'])

    def test_known_replacement_with_unknown_base_stays_available(self):
        sys.path.insert(0,str(ROOT/'skills/borrower-financial-analysis/scripts'))
        from ip_credit_load import prepare_credit_load
        row=self.record();row['monthly_payment']=None
        row['payments']=[{'id':'replacement','date':'2026-12-15','amount':'5.00','treatment':'replace','sources':['BKI']}]
        data=self.case();data['records']=[row]
        code,result,_=self.run_cli(data)
        self.assertEqual(code,0,result)
        load=prepare_credit_load({**result['pdf_patch']['credit_load'],'start_month':'2026-12','start_basis':'user_scenario','start_sources':['BKI']},1,'2026-11-05',{'BKI'})
        self.assertEqual(load['total_rub'],'5.00')
        self.assertEqual(load['months'][0]['unknown_contracts'],[])


if __name__ == '__main__':
    unittest.main()
