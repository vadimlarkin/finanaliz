import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'skills/borrower-financial-analysis/scripts/import_statement_csv.py'
SCRIPTS = ROOT/'skills/borrower-financial-analysis/scripts'


class StatementCSVImport(unittest.TestCase):
    def profile(self):
        return {
            'schema_version':'local-statement-csv-1.0','encoding':'utf-8','delimiter':';',
            'date_format':'%d.%m.%Y',
            'amount_format':{'decimal_separator':',','group_separator':' ','blank_is_zero':True},
            'direction':{'mode':'separate','debit_column':'Расход','credit_column':'Приход'},
            'columns':{'date':'Дата','purpose':'Назначение','currency':'Валюта','category':'Категория'},
            'category_map':{'Покупатели':{'category':'revenue','status':'confirmed'},
                            'Поставщики':{'category':'suppliers','status':'confirmed'}},
            'currency':'RUB','account':{'account':'A','owner_id':'OWNER'},
            'period':{'start':'2026-01-01','end':'2026-02-28'},
            'coverage':{'complete':True},'source_id':'STATEMENT','service_rows':[]}

    def csv_text(self):
        return ('Дата;Назначение;Расход;Приход;Валюта;Категория\n'
                '15.01.2026;Оплата заказа;;100,10;RUB;Покупатели\n'
                '20.01.2026;Оплата поставки;20,02;;RUB;Поставщики\n'
                '10.02.2026;Оплата заказа;;50,25;RUB;Покупатели\n')

    def run_cli(self, text=None, profile=None, before=None, verify_consumers=False):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=folder/'statement.csv';settings=folder/'profile.json'
            output=folder/'result.json';prepared=folder/'prepared.csv'
            profile=self.profile() if profile is None else profile
            raw=(self.csv_text() if text is None else text)
            source.write_bytes(raw.encode(profile.get('encoding','utf-8')) if isinstance(raw,str) else raw)
            settings.write_text(json.dumps(profile,ensure_ascii=False))
            originals=(source.read_bytes(),settings.read_bytes())
            if before is not None:prepared.write_bytes(before)
            proc=subprocess.run([sys.executable,str(SCRIPT),str(source),str(settings),str(output),
                                 '--csv-output',str(prepared)],capture_output=True,text=True)
            self.assertEqual((source.read_bytes(),settings.read_bytes()),originals)
            self.assertTrue(output.exists(),proc.stderr)
            result=json.loads(output.read_text())
            if verify_consumers:
                command=[sys.executable,str(SCRIPTS/'check_cashflow.py'),str(prepared)]
                if profile.get('bank_controls'):
                    controls=folder/'controls.json';controls.write_text(json.dumps(profile['bank_controls']))
                    command+=['--bank-controls',str(controls)]
                check=subprocess.run(command,capture_output=True,text=True)
                self.assertEqual(check.returncode,0,check.stdout or check.stderr)
                self.assertEqual(json.loads(check.stdout)['totals']['credit'],'150.35')
                registry=folder/'registry.json';registry.write_text(json.dumps(result['cashflow'],ensure_ascii=False))
                calculated=folder/'calculated.json'
                calculation=subprocess.run([sys.executable,str(SCRIPTS/'calculate_cashflow.py'),str(registry),str(calculated)],capture_output=True,text=True)
                self.assertEqual(calculation.returncode,0,calculation.stderr)
                self.assertEqual(json.loads(calculated.read_text()),result['summary'])
            return proc.returncode,result,prepared.read_bytes() if prepared.exists() else None

    def test_separate_columns_import_exact_amounts_into_existing_calculation(self):
        code,result,prepared=self.run_cli()
        self.assertEqual(code,0,result)
        self.assertTrue(result['ok'])
        self.assertEqual(result['summary']['totals']['credit']['value'],'150.35')
        self.assertEqual(result['summary']['totals']['debit']['value'],'20.02')
        self.assertEqual(result['summary']['totals']['cash_change']['value'],'130.33')
        self.assertEqual([r['date'] for r in result['cashflow']['operations']],
                         ['2026-01-15','2026-01-20','2026-02-10'])
        self.assertIsNotNone(prepared)

    def test_indicator_and_different_column_order_have_same_substantive_registry(self):
        profile=self.profile()
        profile.update(delimiter=',',date_format='%Y-%m-%d',
                       amount_format={'decimal_separator':'.','group_separator':'','blank_is_zero':False},
                       columns={'date':'Date','purpose':'Text','currency':'ISO','category':'Class'},
                       category_map={'R':{'category':'revenue','status':'confirmed'},
                                     'P':{'category':'suppliers','status':'confirmed'}},
                       direction={'mode':'indicator','amount_column':'Amount','indicator_column':'Kind',
                                  'values':{'D':'debit','C':'credit'}})
        text=('Kind,Amount,Text,Date,ISO,Class\n'
              'C,100.10,Оплата заказа,2026-01-15,RUB,R\n'
              'D,20.02,Оплата поставки,2026-01-20,RUB,P\n'
              'C,50.25,Оплата заказа,2026-02-10,RUB,R\n')
        code,result,_=self.run_cli(text,profile)
        self.assertEqual(code,0,result)
        _,other,_=self.run_cli()
        keys=('date','purpose','debit','credit','currency','category','category_status','account')
        self.assertEqual([{k:r[k] for k in keys} for r in result['cashflow']['operations']],
                         [{k:r[k] for k in keys} for r in other['cashflow']['operations']])
        self.assertEqual(result['summary']['totals']['credit']['value'],'150.35')

    def test_every_physical_line_has_a_source_locator_or_explicit_service_reason(self):
        profile=self.profile();profile['service_rows']=[
            {'column':'Назначение','equals':'ИТОГО','reason':'Служебный разделитель выгрузки'},
            {'blank':True,'reason':'Разрешённый пустой разделитель'}]
        text=('Дата;Назначение;Расход;Приход;Валюта;Категория\n'
              ';ИТОГО;;;;\n\n'
              '15.01.2026;"Оплата\nзаказа";;100,10;RUB;Покупатели\n')
        code,result,_=self.run_cli(text,profile)
        self.assertEqual(code,0,result)
        self.assertEqual([(r['line_start'],r['line_end'],r['kind']) for r in result['row_results']],
                         [(1,1,'header'),(2,2,'service'),(3,3,'service'),(4,5,'operation')])
        self.assertTrue(all(r.get('reason') for r in result['row_results'] if r['kind']=='service'))
        self.assertEqual(result['source']['sha256'],hashlib.sha256(text.encode()).hexdigest())
        row=result['cashflow']['operations'][0]
        self.assertEqual(row['page'],'4-5')
        self.assertEqual(row['source_row'],'4-5')
        self.assertEqual(row['purpose'],'Оплата\nзаказа')

    def test_invalid_rows_are_numbered_and_never_silently_dropped_or_rounded(self):
        text=('Дата;Назначение;Расход;Приход;Валюта;Категория\n'
              '31.02.2026;Неверная дата;;1,00;RUB;Покупатели\n'
              '15.01.2026;Лишняя точность;;1,001;RUB;Покупатели\n'
              '15.01.2026;Другая валюта;;1,00;USD;Покупатели\n'
              '15.01.2026;Недостаточно\n'
              '15.01.2026;Лишняя колонка;;1,00;RUB;Покупатели;ЛИШНЕЕ\n'
              '\n'
              '15.01.2026;Корректная строка;;100,10;RUB;Покупатели\n')
        code,result,prepared=self.run_cli(text,before=b'previous prepared csv')
        self.assertEqual(code,2,result)
        self.assertEqual(prepared,b'previous prepared csv')
        self.assertEqual([r['line_start'] for r in result['row_results'] if r['kind']=='error'],[2,3,4,5,6,7])
        self.assertEqual([i['line_start'] for i in result['issues']],[2,3,4,5,6,7])
        self.assertTrue(all(i['message'] for i in result['issues']))
        self.assertEqual(result['row_results'][-1]['kind'],'operation')
        self.assertEqual(len(result['cashflow']['operations']),1)
        self.assertFalse(result['cashflow']['coverage']['complete'])
        self.assertIsNone(result['summary'])
        self.assertIsNone(result['csv_audit'])

    def test_unsupported_or_ambiguous_profile_is_rejected_before_import(self):
        defects=[lambda p:p.update(schema_version='unknown'),
                 lambda p:p.update(date_format='%m/%d/%Y'),
                 lambda p:p['amount_format'].update(decimal_separator=' ',group_separator=' '),
                 lambda p:p['amount_format'].update(blank_is_zero='yes'),
                 lambda p:p['direction'].update(mode='guess_from_purpose'),
                 lambda p:p.update(delimiter=';;'),
                 lambda p:p['columns'].update(guessed_profit='Приход'),
                 lambda p:p.update(service_rows=[{'blank':False,'reason':'Ошибочная настройка'}]),
                 lambda p:p['account'].update(owner_id=''),
                 lambda p:p['coverage'].update(complete=1),
                 lambda p:p['category_map']['Покупатели'].update(status='guessed'),
                 lambda p:p.update(unknown_option=True)]
        for mutate in defects:
            with self.subTest(mutate=mutate):
                profile=self.profile();mutate(profile)
                code,result,prepared=self.run_cli(profile=profile,before=b'previous')
                self.assertEqual(code,2,result.get('issues'))
                self.assertEqual(result['issues'][0]['code'],'invalid_profile')
                self.assertTrue(result['issues'][0]['message'])
                self.assertEqual(prepared,b'previous')


    def test_independent_controls_are_kept_separate_and_both_existing_clis_accept_import(self):
        profile=self.profile()
        profile['control_sources']=[{'id':'BANK','file':'synthetic-bank-control.json','locator':'Независимые итоги'}]
        profile['bank_controls']=[{'account':'A','source':'BANK','opening':'10.00','debit':'20.02',
                                   'credit':'150.35','closing':'140.33','bank_count':3,'period':copy.deepcopy(profile['period'])}]
        code,result,_=self.run_cli(profile=profile,verify_consumers=True)
        self.assertEqual(code,0,result.get('issues'))
        self.assertTrue(result['csv_audit']['checks']['bank_controls_supplied'])
        self.assertEqual(result['summary']['balances']['closing']['value'],'140.33')
        self.assertEqual(result['cashflow']['bank_controls'],profile['bank_controls'])
        self.assertEqual(result['summary']['balances']['closing']['sources'],['BANK'])
        code,missing,_=self.run_cli()
        self.assertEqual(code,0)
        self.assertFalse(missing['csv_audit']['checks']['bank_controls_supplied'])
        self.assertNotIn('bank_controls',missing['cashflow'])
        self.assertIsNone(missing['summary']['balances']['closing']['value'])
        self.assertTrue(any('Независимые банковские итоги не предоставлены' in s for s in missing['summary']['limits']))

    def test_missing_duplicate_headers_and_broken_csv_syntax_are_explicit(self):
        cases=[('', 'invalid_header'),
               (self.csv_text().replace('Категория\n','Дата\n',1),'invalid_header'),
               (self.csv_text().replace('Назначение;','Неизвестное;',1),'invalid_header'),
               ('Дата;Назначение;Расход;Приход;Валюта;Категория\n15.01.2026;"Незавершённое\nназначение','invalid_csv')]
        for text,expected in cases:
            with self.subTest(expected=expected,text=text[:30]):
                code,result,prepared=self.run_cli(text,before=b'previous')
                self.assertEqual(code,2,result.get('issues'))
                self.assertEqual(result['issues'][0]['code'],expected)
                self.assertEqual(result['source']['sha256'],hashlib.sha256(text.encode()).hexdigest())
                self.assertIsNone(result['summary'])
                self.assertEqual(prepared,b'previous')
                if expected=='invalid_csv':
                    issue=result['issues'][0]
                    self.assertEqual((issue['line_start'],issue['line_end']),(2,3))
                    self.assertEqual(result['row_results'][0]['kind'],'header')
                    self.assertEqual(result['row_results'][-1]['kind'],'error')

    def test_outputs_cannot_replace_csv_profile_or_each_other_including_hard_links(self):
        cases=['json-input','json-profile','csv-input','csv-profile','same-output',
               'json-input-link','csv-profile-link','output-links']
        for defect in cases:
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temp:
                folder=Path(temp);source=folder/'statement.csv';profile=folder/'profile.json'
                output=folder/'result.json';prepared=folder/'prepared.csv'
                source.write_text(self.csv_text());profile.write_text(json.dumps(self.profile(),ensure_ascii=False))
                source_bytes=source.read_bytes();profile_bytes=profile.read_bytes()
                if defect=='json-input':output=source
                elif defect=='json-profile':output=profile
                elif defect=='csv-input':prepared=source
                elif defect=='csv-profile':prepared=profile
                elif defect=='same-output':prepared=output
                elif defect=='json-input-link':output.hardlink_to(source)
                elif defect=='csv-profile-link':prepared.hardlink_to(profile)
                else:
                    output.write_text('previous');prepared.hardlink_to(output)
                proc=subprocess.run([sys.executable,str(SCRIPT),str(source),str(profile),str(output),
                                     '--csv-output',str(prepared)],capture_output=True,text=True)
                self.assertEqual(proc.returncode,2,defect)
                self.assertNotIn('Traceback',proc.stderr)
                self.assertEqual(source.read_bytes(),source_bytes)
                self.assertEqual(profile.read_bytes(),profile_bytes)

    def test_explicit_extra_columns_keep_transfer_details_without_inventing_bank_identity(self):
        profile=self.profile();profile['columns'].update(counterparty_account='Корр',document_number='Док',
                                                          operation_date='ДатаОперации',bank_operation_id='БанкИД')
        text=('Дата;Назначение;Расход;Приход;Валюта;Категория;Корр;Док;ДатаОперации;БанкИД\n'
              '15.01.2026;Оплата заказа;;100,10;RUB;Покупатели;B;DOC-1;14.01.2026;BANK-7\n')
        code,result,_=self.run_cli(text,profile)
        self.assertEqual(code,0,result.get('issues'))
        row=result['cashflow']['operations'][0]
        self.assertEqual((row['counterparty_account'],row['document_number'],row['operation_date'],row['bank_operation_id']),
                         ('B','DOC-1','2026-01-14','BANK-7'))
        self.assertEqual(row['source_row'],'2')
        self.assertEqual(result['loads'][0]['sha256'],result['source']['sha256'])
        self.assertEqual(row['load_id'],result['loads'][0]['id'])
        del profile['columns']['bank_operation_id']
        code,result,_=self.run_cli(text,profile)
        self.assertEqual(code,0,result.get('issues'))
        self.assertNotIn('bank_operation_id',result['cashflow']['operations'][0])
        self.assertEqual(result['cashflow']['operations'][0]['document_number'],'DOC-1')
        code,result,_=self.run_cli(text.replace('14.01.2026','31.02.2026'),profile)
        self.assertEqual(code,2)
        self.assertEqual(result['issues'][0]['line_start'],2)
        self.assertIsNone(result['summary'])

    def test_missing_category_mapping_remains_unresolved_with_original_bank_label(self):
        profile=self.profile();profile.pop('category_map');profile['coverage']['complete']=False
        code,result,_=self.run_cli(profile=profile)
        self.assertEqual(code,0,result.get('issues'))
        row=result['cashflow']['operations'][0]
        self.assertEqual((row['category'],row['category_status']),('unresolved','unresolved'))
        self.assertEqual(row['source_category'],'Покупатели')
        self.assertFalse(result['summary']['complete'])
        self.assertEqual(result['summary']['unresolved']['credit']['value'],'150.35')
        self.assertIsNone(result['summary']['pdf_patch']['flows']['revenue']['value'])
        explicit=self.csv_text().replace('Оплата заказа','Погашение основного долга',1)
        code,result,prepared=self.run_cli(explicit,profile,before=b'previous')
        self.assertEqual(code,2,result.get('issues'))
        self.assertEqual(prepared,b'previous')
        self.assertIn('principal_misclassified',[i['code'] for i in result['csv_audit']['issues']])

    def test_grouped_amounts_and_encoding_are_explicit_and_bad_direction_is_named(self):
        profile=self.profile();profile['encoding']='cp1251';profile['amount_format']['group_separator']='\u00a0'
        text=('Дата;Назначение;Расход;Приход;Валюта;Категория\n'
              '15.01.2026;Оплата;;1\u00a0234,56;RUB;Покупатели\n')
        code,result,_=self.run_cli(text,profile)
        self.assertEqual(code,0,result.get('issues'))
        self.assertEqual(result['summary']['totals']['credit']['value'],'1234.56')
        self.assertEqual(result['source']['sha256'],hashlib.sha256(text.encode('cp1251')).hexdigest())
        for bad in ['12\u00a034,56','-1,00','NaN','1e3']:
            code,result,_=self.run_cli(text.replace('1\u00a0234,56',bad),profile)
            self.assertEqual(code,2)
            self.assertEqual(result['issues'][0]['line_start'],2)
        indicator=self.profile()
        indicator['direction']={'mode':'indicator','amount_column':'Приход','indicator_column':'Метка',
                                'values':{'D':'debit','C':'credit'}}
        bad_side=('Дата;Назначение;Расход;Приход;Валюта;Категория;Метка\n'
                  '15.01.2026;Оплата;;100,10;RUB;Покупатели;X\n')
        code,result,_=self.run_cli(bad_side,indicator)
        self.assertEqual(code,2)
        self.assertIn('направления',result['issues'][0]['message'])
        self.assertEqual(result['issues'][0]['line_start'],2)

    def test_unreadable_encoding_and_unsupported_money_range_have_controlled_diagnostics(self):
        code,result,_=self.run_cli(b'\xff\xfe\xff')
        self.assertEqual(code,2)
        self.assertEqual(result['issues'][0]['code'],'invalid_encoding')
        self.assertEqual(result['source']['sha256'],hashlib.sha256(b'\xff\xfe\xff').hexdigest())
        huge=self.csv_text().replace('100,10','9'*40+',99')
        code,result,_=self.run_cli(huge)
        self.assertEqual(code,2)
        self.assertEqual(result['issues'][0]['line_start'],2)
        self.assertIn('диапазон',result['issues'][0]['message'])

    def test_repeated_run_is_identical_and_changed_statement_recalculates_from_source(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=folder/'statement.csv';profile=folder/'profile.json'
            output=folder/'result.json';prepared=folder/'prepared.csv'
            source.write_text(self.csv_text());profile.write_text(json.dumps(self.profile(),ensure_ascii=False))
            command=[sys.executable,str(SCRIPT),str(source),str(profile),str(output),'--csv-output',str(prepared)]
            original=source.read_bytes();settings=profile.read_bytes()
            first=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(first.returncode,0,first.stderr)
            expected=(output.read_bytes(),prepared.read_bytes())
            second=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(second.returncode,0,second.stderr)
            self.assertEqual((output.read_bytes(),prepared.read_bytes()),expected)
            self.assertEqual(source.read_bytes(),original);self.assertEqual(profile.read_bytes(),settings)
            before=json.loads(expected[0]);source.write_text(self.csv_text().replace('50,25','60,25'))
            updated=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(updated.returncode,0,updated.stderr)
            after=json.loads(output.read_text())
            self.assertNotEqual(before['source']['sha256'],after['source']['sha256'])
            self.assertEqual(after['summary']['totals']['credit']['value'],'160.35')
            self.assertEqual(after['summary']['months'][1]['totals']['credit']['value'],'60.25')

    def test_registered_binary_codec_is_a_profile_diagnostic_and_preserves_previous_files(self):
        profile=self.profile();profile['encoding']='base64_codec'
        code,result,prepared=self.run_cli(self.csv_text().encode('utf-8'),profile,before=b'previous prepared csv')
        self.assertEqual(code,2,result.get('issues'))
        self.assertEqual(result['issues'][0]['code'],'invalid_profile')
        self.assertIn('кодиров',result['issues'][0]['message'])
        self.assertEqual(prepared,b'previous prepared csv')
