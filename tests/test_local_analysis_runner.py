import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'skills/borrower-financial-analysis/scripts/run_local_analysis.py'
REQUIRED=('borrower','loan','annual_statements','trial_balance','debt','statements')


class LocalAnalysisRunnerCLI(unittest.TestCase):
    def case(self,mode='corporate'):
        return {'schema_version':'local-analysis-runner-1.0','case_id':'SYNTHETIC',
                'mode':mode,'borrower':{'name':'ООО «Пример»' if mode=='corporate' else 'ИП Примеров А.А.',
                                         'inn':'0000000000' if mode=='corporate' else '000000000000'},
                'assessment_date':'2026-10-22' if mode=='corporate' else '2026-11-04',
                'scope':'Предварительная проверка выбранного заёмщика по подготовленным данным.',
                'sources':[{'id':'INTAKE','file':'synthetic-intake.md','locator':'Проверка аналитика'}],
                'intake':{'reviewed_by':'Синтетический аналитик','checks':[
                    {'id':key,'status':'confirmed','sources':['INTAKE']} for key in REQUIRED]},
                'report_input':str(ROOT/'examples/investor.json') if mode=='corporate' else str(ROOT/'examples/ip-investor-v3.1.json'),
                'render_pdf':True,'steps':{}}

    def run_case(self,root,data):
        case=root/'case.json';case.write_text(json.dumps(data,ensure_ascii=False))
        result=subprocess.run([sys.executable,str(SCRIPT),str(case),str(root/'run')],capture_output=True,text=True)
        self.assertTrue(result.stdout.strip(),result.stderr)
        return result.returncode,json.loads(result.stdout)

    def test_corporate_missing_minimum_stops_before_financial_input_and_separates_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();data['report_input']=str(root/'MISSING-FINANCIAL.json')
            checks=data['intake']['checks']
            next(c for c in checks if c['id']=='trial_balance').update(status='missing',borrower_request='Предоставить общую ОСВ за 01.01.2026–30.09.2026.')
            next(c for c in checks if c['id']=='loan').update(status='missing',creditor_question='Уточнить комиссию нового займа.')
            code,result=self.run_case(root,data)
            self.assertEqual(code,2,result);self.assertEqual(result['status'],'stopped')
            attempt=root/'run'/result['attempt_dir']
            self.assertIn('ОСВ',(attempt/'request-to-borrower.txt').read_text())
            self.assertNotIn('комиссию',(attempt/'request-to-borrower.txt').read_text())
            self.assertIn('комиссию',(attempt/'questions-to-creditor.txt').read_text())
            manifest=json.loads((attempt/'manifest.json').read_text())
            self.assertEqual(manifest['status'],'stopped')
            self.assertEqual(len(manifest['inputs']),1)
            self.assertEqual((attempt/manifest['inputs'][0]['snapshot']).read_bytes(),(root/'case.json').read_bytes())
            self.assertFalse((root/'run/current').exists())
            self.assertFalse(list((root/'run').rglob('analysis.json')))
            self.assertFalse(list((root/'run').rglob('*.pdf')))

    def test_full_corporate_intake_runs_existing_report_and_preserves_reproducible_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case()
            code,result=self.run_case(root,data)
            self.assertEqual(code,0,result)
            current=root/'run/current';manifest=json.loads((current/'manifest.json').read_text())
            self.assertEqual(manifest['case_id'],'SYNTHETIC')
            self.assertEqual(manifest['pdf'],{'pages':1,'visual_review':'pending','visual_review_required':True})
            self.assertEqual(json.loads((current/'analysis.json').read_text())['layout_version'],'investor-one-page-2.2')
            self.assertTrue((current/'report.pdf').exists())
            self.assertEqual(len(manifest['inputs']),2)
            for item in manifest['inputs']:
                snapshot=current/item['snapshot']
                self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(),item['sha256'])
            self.assertTrue(manifest['tools']['scripts']['run_local_analysis.py'])
            self.assertEqual([s['name'] for s in manifest['stages']],['intake','report','terminology','pdf_check'])

    def test_ip_without_accounting_or_loan_term_gets_limited_existing_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case('ip');data.pop('intake')
            report=json.loads((ROOT/'examples/ip-investor-v3.1.json').read_text())
            report['loan']['term_months']=None
            source=root/'ip-input.json';source.write_text(json.dumps(report,ensure_ascii=False));data['report_input']=str(source)
            code,result=self.run_case(root,data)
            self.assertEqual(code,0,result)
            current=root/'run/current';manifest=json.loads((current/'manifest.json').read_text())
            self.assertEqual(manifest['data_status'],'limited')
            audit=json.loads((current/'analysis.json').read_text())
            self.assertEqual(audit['layout_version'],'ip-investor-one-page-3.2')
            self.assertIsNone(audit['credit_load']['period']['end_month'])
            self.assertIsNone(json.loads((current/'input.json').read_text())['loan']['term_months'])
            self.assertEqual(manifest['pdf']['visual_review'],'pending')

    def test_failure_preserves_current_and_correction_publishes_backup_of_previous_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();source=root/'financial.json'
            report=json.loads((ROOT/'examples/investor.json').read_text());source.write_text(json.dumps(report,ensure_ascii=False));data['report_input']=str(source)
            code,first=self.run_case(root,data);self.assertEqual(code,0,first)
            current=root/'run/current'
            before={p.relative_to(current).as_posix():p.read_bytes() for p in current.rglob('*') if p.is_file()}
            report['currency']='USD';source.write_text(json.dumps(report,ensure_ascii=False))
            code,error=self.run_case(root,data)
            self.assertEqual(code,2,error);self.assertEqual(error['stage'],'report')
            self.assertEqual(error['status'],'failed');self.assertIn('currency',error['message'])
            self.assertEqual(json.loads((root/'run/attempt.json').read_text()),error)
            self.assertEqual({p.relative_to(current).as_posix():p.read_bytes() for p in current.rglob('*') if p.is_file()},before)
            report['currency']='RUB';report['company']['name']='ООО «Пример 2»';data['borrower']['name']=report['company']['name'];source.write_text(json.dumps(report,ensure_ascii=False))
            code,updated=self.run_case(root,data);self.assertEqual(code,0,updated)
            self.assertNotEqual(updated['run_id'],first['run_id'])
            backup=root/'run'/updated['backup']
            self.assertEqual({p.relative_to(backup).as_posix():p.read_bytes() for p in backup.rglob('*') if p.is_file()},before)
            self.assertEqual(json.loads((current/'input.json').read_text())['company']['name'],'ООО «Пример 2»')

    def test_invalid_case_and_intake_context_is_diagnostic_instead_of_choosing_a_route(self):
        defects=[('schema',lambda d:d.update(schema_version='unknown')),
                 ('mode',lambda d:d.update(mode='unknown')),
                 ('name',lambda d:d['borrower'].update(name='')),
                 ('inn',lambda d:d['borrower'].update(inn='other')),
                 ('date',lambda d:d.update(assessment_date='2026-02-30')),
                 ('scope',lambda d:d.update(scope='')),
                 ('sources',lambda d:d['sources'].append(copy.deepcopy(d['sources'][0]))),
                 ('duplicate-check',lambda d:d['intake']['checks'].append(copy.deepcopy(d['intake']['checks'][0]))),
                 ('unknown-ref',lambda d:d['intake']['checks'][0].update(sources=['UNKNOWN'])),
                 ('empty-ref',lambda d:d['intake']['checks'][0].update(sources=[])),
                 ('unknown-status',lambda d:d['intake']['checks'][0].update(status='maybe')),
                 ('unknown-step',lambda d:d['steps'].update(unsupported={})),
                 ('unknown-field',lambda d:d.update(unsupported=True))]
        for name,mutate in defects:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);data=self.case();mutate(data)
                code,result=self.run_case(root,data)
                self.assertEqual(code,2,result);self.assertEqual(result['status'],'failed')
                self.assertEqual(result['stage'],'case')
                self.assertTrue(result['message'])
                self.assertFalse(list((root/'run').rglob('*.pdf')))

    def test_report_identity_date_and_current_workspace_context_cannot_be_mixed(self):
        for defect in ('name','date','inn'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);data=self.case('ip')
                report=json.loads((ROOT/'examples/ip-investor-v3.1.json').read_text())
                report[{'name':'borrower_name','date':'assessment_date','inn':'inn'}[defect]]={'name':'ИП Другой','date':'2026-11-03','inn':'111111111111'}[defect]
                source=root/'input.json';source.write_text(json.dumps(report,ensure_ascii=False));data['report_input']=str(source)
                code,result=self.run_case(root,data)
                self.assertEqual(code,2,result);self.assertEqual(result['stage'],'report_context')
                self.assertFalse((root/'run/current').exists())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();code,first=self.run_case(root,data);self.assertEqual(code,0,first)
            manifest=(root/'run/current/manifest.json').read_bytes();data['case_id']='OTHER'
            code,result=self.run_case(root,data)
            self.assertEqual(code,2,result);self.assertEqual(result['stage'],'case')
            self.assertEqual((root/'run/current/manifest.json').read_bytes(),manifest)

        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case('ip');data.pop('intake');data.pop('report_input');data['render_pdf']=False
            data['assessment_date']='2026-01-01';data['steps']={'cashflow':{'input':str(ROOT/'examples/local-cashflow.json')}}
            code,result=self.run_case(root,data)
            self.assertEqual(code,2,result);self.assertEqual(result['stage'],'cashflow')
            self.assertFalse((root/'run/current').exists())

    def test_inputs_inside_managed_workdir_and_symlinked_managed_paths_are_protected(self):
        for location in ('attempt.json','current/input.json'):
            with self.subTest(location=location),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);data=self.case();source=root/'run'/location;source.parent.mkdir(parents=True)
                source.write_bytes((ROOT/'examples/investor.json').read_bytes());before=source.read_bytes();data['report_input']=str(source)
                code,result=self.run_case(root,data)
                self.assertEqual(code,2,result)
                self.assertEqual(source.read_bytes(),before)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();outside=root/'outside';outside.mkdir();(root/'run').mkdir();(root/'run/backups').symlink_to(outside,target_is_directory=True)
            code,result=self.run_case(root,data)
            self.assertEqual(code,2,result);self.assertFalse(list(outside.iterdir()))

        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();(root/'run').mkdir()
            original=root/'run/attempt.json';original.write_text('Самостоятельный исходный документ.',encoding='utf-8');before=original.read_bytes()
            code,result=self.run_case(root,data)
            self.assertEqual(code,2,result);self.assertEqual(original.read_bytes(),before)
            self.assertFalse((root/'run/current').exists())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();(root/'run').mkdir()
            original=root/'run/source-proof.txt';original.write_text('Исходное доказательство.',encoding='utf-8')
            report=json.loads((ROOT/'examples/investor.json').read_text());report['sources'][0]['file']=str(original)
            source=root/'report.json';source.write_text(json.dumps(report,ensure_ascii=False));data['report_input']=str(source)
            code,result=self.run_case(root,data)
            self.assertEqual(code,2,result);self.assertFalse((root/'run/current').exists())

    def test_selected_prepared_cashflow_runs_without_unrelated_inputs_or_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case('ip');data.pop('intake');data.pop('report_input');data['render_pdf']=False
            data['steps']={'cashflow':{'input':str(ROOT/'examples/local-cashflow.json')}}
            code,result=self.run_case(root,data);self.assertEqual(code,0,result)
            current=root/'run/current';flow=json.loads((current/'stages/cashflow.json').read_text());manifest=json.loads((current/'manifest.json').read_text())
            self.assertEqual(flow['totals']['credit']['value'],'150.47')
            self.assertEqual(flow['totals']['debit']['value'],'22.06')
            self.assertEqual(flow['totals']['cash_change']['value'],'128.41')
            self.assertEqual([s['name'] for s in manifest['stages']],['cashflow','terminology'])
            self.assertIsNone(manifest['pdf'])
            self.assertFalse((current/'report.pdf').exists());self.assertFalse((current/'analysis.json').exists())
            self.assertEqual(len(manifest['inputs']),2)

    def transfer_case(self,root):
        import csv
        data=self.case('ip');data.pop('intake');data.pop('report_input');data['render_pdf']=False
        flow={'schema_version':'local-cashflow-1.0','period':{'start':'2026-01-01','end':'2026-02-28'},
              'accounts':[{'account':x,'owner_id':'OWNER'} for x in ('A','B')],
              'coverage':{'complete':True},'sources':[],'operations':[]}
        base=root/'base.json';base.write_text(json.dumps(flow));imports=[]
        for account,rows in [('A',[['2026-01-31','Перевод','100.01','','own_out','B','D1','2026-01-31'],['2026-01-31','Расчёт','20.02','','suppliers','','','']]),
                             ('B',[['2026-02-01','Перевод','','100.01','own_in','A','D1','2026-01-31'],['2026-02-03','Выручка','','50.03','revenue','','','']])]:
            source=root/(account+'.csv');profile=root/(account+'-profile.json')
            with source.open('w',newline='') as stream:
                writer=csv.writer(stream,delimiter=';');writer.writerow(['Date','Purpose','Debit','Credit','Category','Counterparty','Document','OperationDate']);writer.writerows(rows)
            settings={'schema_version':'local-statement-csv-1.0','encoding':'utf-8','delimiter':';','date_format':'%Y-%m-%d',
                      'amount_format':{'decimal_separator':'.','group_separator':'','blank_is_zero':True},
                      'direction':{'mode':'separate','debit_column':'Debit','credit_column':'Credit'},
                      'columns':{'date':'Date','purpose':'Purpose','category':'Category','counterparty_account':'Counterparty','document_number':'Document','operation_date':'OperationDate'},
                      'category_map':{k:{'category':k,'status':'confirmed'} for k in ('own_out','own_in','suppliers','revenue')},
                      'currency':'RUB','account':{'account':account,'owner_id':'OWNER'},'period':flow['period'],'coverage':flow['coverage'],
                      'source_id':'BANK-'+account,'service_rows':[]}
            profile.write_text(json.dumps(settings,ensure_ascii=False));imports.append({'path':str(source),'profile':str(profile)})
        data['steps']={'cashflow':{'input':str(base),'imports':imports,'overlaps':{'loads':[],'decisions':[]},'transfers':{'external_accounts':[],'decisions':[]}}}
        return data

    def test_import_overlap_transfer_chain_recomputes_new_statement_and_rejects_stale_decision(self):
        import csv
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.transfer_case(root)
            code,pending=self.run_case(root,data);self.assertEqual(code,2,pending);self.assertEqual(pending['stage'],'transfers')
            candidate=pending['details']['candidates'][0]
            decision={'pair_id':'P1','operation_ids':candidate['operation_ids'],'fingerprints':candidate['fingerprints'],
                      'action':'confirm','reason':'Аналитик сверил документ и владельцев',
                      'evidence':{'sources':['BANK-A','BANK-B'],'document_number':'D1','operation_date':'2026-01-31'}}
            data['steps']['cashflow']['transfers']['decisions']=[decision]
            code,first=self.run_case(root,data);self.assertEqual(code,0,first)
            current=root/'run/current';flow=json.loads((current/'stages/cashflow.json').read_text())
            self.assertEqual(flow['after_matched_transfers']['debit']['value'],'20.02')
            self.assertEqual(flow['after_matched_transfers']['credit']['value'],'50.03')
            source=root/'C.csv'
            with source.open('w',newline='') as stream:
                writer=csv.writer(stream,delimiter=';');writer.writerow(['Date','Purpose','Debit','Credit','Category','Counterparty','Document','OperationDate']);writer.writerow(['2026-02-04','Выручка','','20.00','revenue','','',''])
            profile=json.loads((root/'B-profile.json').read_text());profile['source_id']='BANK-C';p=root/'C-profile.json';p.write_text(json.dumps(profile,ensure_ascii=False))
            data['steps']['cashflow']['imports'].append({'path':str(source),'profile':str(p)})
            code,updated=self.run_case(root,data);self.assertEqual(code,0,updated)
            flow=json.loads((current/'stages/cashflow.json').read_text())
            self.assertEqual(flow['after_matched_transfers']['credit']['value'],'70.03')
            self.assertEqual(flow['audit']['checks']['matched_pairs'],1)
            before={p.relative_to(current).as_posix():p.read_bytes() for p in current.rglob('*') if p.is_file()}
            changed=(root/'A.csv').read_text().replace('D1','D2');(root/'A.csv').write_text(changed)
            code,error=self.run_case(root,data);self.assertEqual(code,2,error);self.assertEqual(error['stage'],'transfers')
            self.assertEqual({p.relative_to(current).as_posix():p.read_bytes() for p in current.rglob('*') if p.is_file()},before)

    def test_unchanged_transfer_decision_requires_reconfirmation_after_owner_or_evidence_version_change(self):
        for change in ('owner','evidence'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);data=self.transfer_case(root)
                proof=root/'verified/proof.txt';proof.parent.mkdir();proof.write_text('Первоначальная сверка документа D1.',encoding='utf-8');data['source_files']=[str(proof)]
                raw=json.loads((root/'base.json').read_text());base=root/'verified/base.json';data['steps']['cashflow']['input']=str(base);raw['sources']=[{'id':'PROOF','file':'proof.txt','locator':'Сверка D1'}];base.write_text(json.dumps(raw,ensure_ascii=False))
                code,pending=self.run_case(root,data);self.assertEqual(code,2,pending)
                candidate=pending['details']['candidates'][0]
                decision={'pair_id':'P1','operation_ids':candidate['operation_ids'],'fingerprints':candidate['fingerprints'],'action':'confirm',
                          'reason':'Сверено по документу D1','evidence':{'sources':['PROOF'],'document_number':'D1','operation_date':'2026-01-31'}}
                data['steps']['cashflow']['transfers']['decisions']=[decision]
                code,first=self.run_case(root,data);self.assertEqual(code,0,first)
                current=root/'run/current';before=(current/'manifest.json').read_bytes()
                if change=='owner':
                    raw=json.loads(base.read_text())
                    for account in raw['accounts']:account['owner_id']='OTHER'
                    base.write_text(json.dumps(raw,ensure_ascii=False))
                    for name in ('A','B'):
                        path=root/(name+'-profile.json');profile=json.loads(path.read_text());profile['account']['owner_id']='OTHER';path.write_text(json.dumps(profile,ensure_ascii=False))
                else:proof.write_text('Исправленная версия сверки документа D1.',encoding='utf-8')
                code,result=self.run_case(root,data)
                self.assertEqual(code,2,result);self.assertEqual(result['stage'],'transfers')
                self.assertIn('контекст',result['message'])
                self.assertEqual((current/'manifest.json').read_bytes(),before)
                decision['reason']='  '+decision['reason']+'  '
                code,space_only=self.run_case(root,data);self.assertEqual(code,2,space_only)
                decision['reason']+='; аналитик повторно сверил изменённый контекст'
                code,reviewed=self.run_case(root,data);self.assertEqual(code,0,reviewed)

    def test_debt_and_loan_stages_use_existing_modules_and_replace_unknown_report_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();terms=json.loads((ROOT/'examples/local-loan-schedule.json').read_text())
            terms['issue_date']='2026-10-22';terms['repayments'][0]['date']='2026-11-01';terms['interest_payment_dates']=['2026-11-01'];terms['fees'][0]['date']='2026-10-22';terms['first_12_months']['end']='2027-10-22'
            path=root/'loan.json';path.write_text(json.dumps(terms,ensure_ascii=False));data['steps']={'loan_schedule':str(path)}
            code,result=self.run_case(root,data);self.assertEqual(code,0,result)
            current=root/'run/current';report=json.loads((current/'input.json').read_text());schedule=json.loads((current/'stages/loan_schedule.json').read_text())
            self.assertEqual(schedule['totals']['interest'],'10.00')
            self.assertEqual(report['facts']['new_loan_interest_12m']['value'],10)
            self.assertEqual(report['facts']['new_loan_fees_12m']['value'],20)
            self.assertEqual(report['loan_repayment'],'schedule')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case('ip');data.pop('intake')
            registry={'schema_version':'local-debt-register-1.0','borrower_id':data['borrower']['inn'],'kind':'bki','report_date':'2026-11-01',
                      'sources':[{'id':'BKI','file':'synthetic-bki.pdf','locator':'1'}],
                      'records':[{'id':'A','contract_id':'A','subject_id':data['borrower']['inn'],'role':'borrower','creditor_id':'BANK-A','status':'active',
                                  'as_of':'2026-10-20','sources':['BKI'],'amounts':{'principal':'100.01','interest':'2.02','total':'102.03','overdue':'0.00'},
                                  'monthly_payment':None,'payments':[]},
                                 {'id':'B','contract_id':'B','subject_id':data['borrower']['inn'],'role':'borrower','creditor_id':'BANK-B','status':'active',
                                  'as_of':'2026-10-25','sources':['BKI'],'amounts':{'principal':None,'interest':None,'total':None,'overdue':None},
                                  'monthly_payment':None,'payments':[]}]}
            path=root/'debt.json';path.write_text(json.dumps(registry,ensure_ascii=False));data['steps']={'debt_register':str(path)}
            code,result=self.run_case(root,data);self.assertEqual(code,0,result)
            report=json.loads((root/'run/current/input.json').read_text());debt=json.loads((root/'run/current/stages/debt_register.json').read_text())
            self.assertIsNone(report['bki']['total']['value']);self.assertEqual(report['bki']['total']['status'],'missing')
            self.assertEqual(debt['totals']['total']['known_subtotal'],'102.03')
            self.assertFalse('active_contracts' in report['bki'])
            self.assertEqual(report['loan']['investor_rate_pct'],json.loads((ROOT/'examples/ip-investor-v3.1.json').read_text())['loan']['investor_rate_pct'])

    def test_absent_intake_or_check_does_not_invent_a_full_document_request(self):
        for missing in ('intake','check','specific-gap'):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);data=self.case();data['report_input']=str(root/'not-read.json')
                if missing=='intake':data.pop('intake')
                elif missing=='check':data['intake']['checks']=[c for c in data['intake']['checks'] if c['id']!='statements']
                else:next(c for c in data['intake']['checks'] if c['id']=='statements')['status']='missing'
                code,result=self.run_case(root,data);self.assertEqual(code,2,result);self.assertEqual(result['status'],'stopped')
                attempt=root/'run'/result['attempt_dir']
                self.assertFalse((attempt/'request-to-borrower.txt').exists())
                self.assertIn('аналитик',(attempt/'intake.md').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();data['report_input']=str(root/'not-read.json')
            text='Предоставить выписки только по счёту A за 01.10.2025–30.06.2026.'
            next(c for c in data['intake']['checks'] if c['id']=='statements').update(status='missing',borrower_request=text)
            code,result=self.run_case(root,data);attempt=root/'run'/result['attempt_dir']
            self.assertIn(text,(attempt/'request-to-borrower.txt').read_text())
            self.assertIn(text,(attempt/'intake.md').read_text())

    def test_selected_pdf_extraction_reuses_cache_and_preserves_readable_snapshot_after_promotion(self):
        from reportlab.pdfgen.canvas import Canvas
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.pdf'
            def pdf(text):
                c=Canvas(str(source),invariant=1);c.drawString(50,750,text);c.save()
            pdf('SOURCE 123.45')
            data=self.case('ip');data.pop('report_input');data.pop('intake');data['render_pdf']=False
            data['steps']={'pdf_extractions':[{'path':str(source),'language':'eng','ocr':'none'}]}
            code,first=self.run_case(root,data);self.assertEqual(code,0,first)
            current=root/'run/current';extracted=json.loads((current/'stages/pdf_extraction_1.json').read_text());manifest=json.loads((current/'manifest.json').read_text())
            self.assertIn('SOURCE 123.45',(current/extracted['text_path']).read_text())
            snapshot=current/extracted['source']['snapshot']
            self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(),extracted['source']['sha256'])
            self.assertEqual(next(i['sha256'] for i in manifest['inputs'] if i['path']==str(source.resolve())),extracted['source']['sha256'])
            code,second=self.run_case(root,data);self.assertEqual(code,0,second)
            again=json.loads((current/'stages/pdf_extraction_1.json').read_text());self.assertTrue(again['cache_hit'])
            self.assertEqual(first['run_id'],second['run_id'])
            pdf('UPDATED 678.90')
            code,third=self.run_case(root,data);self.assertEqual(code,0,third);self.assertNotEqual(third['run_id'],second['run_id'])
            changed=json.loads((current/'stages/pdf_extraction_1.json').read_text());self.assertNotEqual(changed['cache_key'],again['cache_key'])
            self.assertIn('UPDATED 678.90',(current/changed['text_path']).read_text())

    def test_failed_audit_keeps_previous_current_and_retains_failed_input_versions_and_stage_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case('ip');data.pop('intake');data.pop('report_input');data['render_pdf']=False
            source=root/'flow.json';flow=json.loads((ROOT/'examples/local-cashflow.json').read_text());source.write_text(json.dumps(flow,ensure_ascii=False));data['steps']={'cashflow':{'input':str(source)}}
            code,first=self.run_case(root,data);self.assertEqual(code,0,first)
            current=root/'run/current';before=(current/'manifest.json').read_bytes()
            flow['bank_controls'][0]['closing']='999.99';source.write_text(json.dumps(flow,ensure_ascii=False))
            code,error=self.run_case(root,data);self.assertEqual(code,2,error);self.assertEqual(error['stage'],'cashflow')
            self.assertIn('bank_balance_mismatch',error['message'])
            self.assertEqual((current/'manifest.json').read_bytes(),before)
            attempt=root/'run'/error['attempt_dir'];manifest=json.loads((attempt/'manifest.json').read_text())
            self.assertEqual(manifest['status'],'failed')
            self.assertEqual(manifest['stage'],'cashflow')
            snapshot=next(item for item in manifest['inputs'] if item['path']==str(source.resolve()))
            self.assertEqual((attempt/snapshot['snapshot']).read_bytes(),source.read_bytes())
            failed=json.loads((attempt/'stages/cashflow.json').read_text());self.assertFalse(failed['ok'])
            self.assertIsNone(failed['balances']['closing']['value'])

    def test_repeat_run_reproduces_calculations_and_versions_settings_and_all_bundle_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();code,first=self.run_case(root,data);self.assertEqual(code,0,first)
            current=root/'run/current';original={name:(current/name).read_bytes() for name in ('input.json','analysis.json','report.pdf')}
            code,second=self.run_case(root,data);self.assertEqual(code,0,second);self.assertEqual(first['run_id'],second['run_id'])
            self.assertEqual({name:(current/name).read_bytes() for name in original},original)
            manifest=json.loads((current/'manifest.json').read_text())
            actual={p.relative_to(current).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in current.rglob('*') if p.is_file() and p.name!='manifest.json'}
            self.assertEqual(manifest['artifacts'],actual)
            self.assertEqual(manifest['tools']['scripts']['run_local_analysis.py'],hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
            data['render_pdf']=False;code,third=self.run_case(root,data);self.assertEqual(code,0,third)
            self.assertNotEqual(third['run_id'],second['run_id']);self.assertFalse((current/'report.pdf').exists())
            manifest=json.loads((current/'manifest.json').read_text());case_snapshot=current/manifest['inputs'][0]['snapshot']
            self.assertFalse(json.loads(case_snapshot.read_text())['render_pdf'])
            self.assertIsNone(manifest['pdf'])

    def test_duplicate_json_keys_in_case_and_prepared_input_are_structured_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();case=root/'case.json'
            raw=json.dumps(data,ensure_ascii=False).replace('"mode": "corporate"','"mode": "corporate", "mode": "corporate"')
            case.write_text(raw)
            proc=subprocess.run([sys.executable,str(SCRIPT),str(case),str(root/'run')],capture_output=True,text=True)
            self.assertEqual(proc.returncode,2,proc.stdout);self.assertNotIn('Traceback',proc.stderr)
            self.assertIn('Повтор',json.loads(proc.stdout)['message'])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=self.case();source=root/'financial.json'
            source.write_text((ROOT/'examples/investor.json').read_text().replace('"currency": "RUB"','"currency": "RUB", "currency": "RUB"'))
            data['report_input']=str(source);code,result=self.run_case(root,data)
            self.assertEqual(code,2,result);self.assertEqual(result['stage'],'report_context')
            self.assertFalse((root/'run/current').exists())

    def test_prepared_source_and_load_sha_must_match_actually_selected_source_version(self):
        for claim in ('source','load'):
            with self.subTest(claim=claim),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=root/'statement.csv';source.write_bytes((ROOT/'examples/local-statement-overlaps-source.csv').read_bytes())
                raw=json.loads((ROOT/'examples/local-statement-overlaps.json').read_text());flow=raw['cashflow']
                for ref in flow['sources']:ref['file']=str(source)
                if claim=='source':
                    flow['sources']=flow['sources'][:1];flow['operations']=flow['operations'][:1];flow['sources'][0]['sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
                path=root/'flow.json';path.write_text(json.dumps(flow,ensure_ascii=False));data=self.case('ip');data.pop('intake');data.pop('report_input');data['render_pdf']=False;data['source_files']=[str(source)];data['steps']={'cashflow':{'input':str(path)}}
                if claim=='load':data['steps']['cashflow']['overlaps']={'loads':raw['loads'],'decisions':[]}
                code,first=self.run_case(root,data);self.assertEqual(code,0,first)
                current=root/'run/current';before=(current/'manifest.json').read_bytes()
                source.write_text(source.read_text().replace('100.10','200.20'))
                code,error=self.run_case(root,data)
                self.assertEqual(code,2,error);self.assertIn('SHA256',error['message'])
                self.assertEqual((current/'manifest.json').read_bytes(),before)
