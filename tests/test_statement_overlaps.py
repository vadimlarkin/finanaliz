import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'skills/borrower-financial-analysis/scripts/reconcile_statement_overlaps.py'


class StatementOverlapsCLI(unittest.TestCase):
    def row(self, uid='a', source='S1', load='L1', locator='1', **changes):
        row = {'id':uid, 'source':source, 'page':locator, 'load_id':load, 'source_row':locator,
               'account':'A', 'date':'2026-01-15', 'debit':'0.00', 'credit':'100.10',
               'purpose':'Оплата по договору', 'category':'revenue', 'category_status':'confirmed'}
        row.update(changes)
        return row

    def case(self):
        return {'schema_version':'local-statement-overlaps-1.0',
                'loads':[{'id':'L1','source':'S1','sha256':'a'*64},
                         {'id':'L2','source':'S2','sha256':'a'*64}],
                'cashflow':{'schema_version':'local-cashflow-1.0',
                            'period':{'start':'2026-01-01','end':'2026-02-28'},
                            'accounts':[{'account':'A','owner_id':'OWNER'}],
                            'coverage':{'complete':True},
                            'sources':[{'id':'S1','file':'synthetic-1.csv','locator':'строки 1-2'},
                                       {'id':'S2','file':'synthetic-2.csv','locator':'строки 1-2'}],
                            'operations':[self.row(),self.row('b','S2','L2')]}}

    def cli(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'in.json'; output=Path(tmp)/'out.json'
            source.write_text(json.dumps(data,ensure_ascii=False)); before=source.read_bytes()
            result=subprocess.run([sys.executable,str(SCRIPT),str(source),str(output)],capture_output=True,text=True)
            self.assertEqual(source.read_bytes(),before)
            self.assertTrue(output.exists(),result.stderr)
            return result.returncode,json.loads(output.read_text()),output.read_bytes()

    def test_same_export_row_is_counted_once_and_keeps_both_origins(self):
        code,result,_=self.cli(self.case())
        self.assertEqual(code,0,result)
        self.assertEqual(result['summary']['totals']['credit']['value'],'100.10')
        self.assertEqual(len(result['registry']['operations']),1)
        self.assertEqual(result['summary']['pdf_patch']['bank_scope']['operations_count'],1)
        self.assertEqual({x['source'] for x in result['lineage'][0]['appearances']},{'S1','S2'})
        self.assertEqual(result['confirmed_duplicates'][0]['basis'],'source_hash_row')
        self.assertEqual(result['loads'],self.case()['loads'])

    def test_overlapping_exports_share_verified_bank_identity_and_recalculate_months(self):
        data=self.case();data['loads'][1]['sha256']='b'*64
        for row in data['cashflow']['operations']:
            row['bank_operation_id']='BANK-1'
        data['cashflow']['operations'].append(self.row('c','S2','L2','2',date='2026-02-03',credit='50.25',bank_operation_id='BANK-2'))
        code,result,_=self.cli(data)
        self.assertEqual(code,0,result)
        self.assertEqual(result['summary']['totals']['credit']['value'],'150.35')
        self.assertEqual([m['totals']['credit']['value'] for m in result['summary']['months']],['100.10','50.25'])
        self.assertEqual(result['confirmed_duplicates'][0]['basis'],'bank_operation_id')
        self.assertEqual({a['operation_id'] for a in result['lineage'][0]['appearances']},{'a','b'})

    def test_same_date_amount_purpose_without_identity_is_unresolved_and_keeps_both(self):
        data=self.case();data['loads'][1]['sha256']='b'*64
        code,result,_=self.cli(data)
        self.assertEqual(code,2,result)
        self.assertFalse(result['clean'])
        self.assertIsNone(result['summary'])
        self.assertEqual(len(result['registry']['operations']),2)
        self.assertEqual(result['provisional_summary']['totals']['credit']['value'],'200.20')
        self.assertIsNone(result['provisional_summary']['pdf_patch'])
        self.assertFalse(result['provisional_summary']['pdf_compatible'])
        self.assertEqual(result['candidates'][0]['ids'],['a','b'])
        self.assertEqual(result['unresolved_matches'][0]['code'],'possible_duplicate')

    def test_two_real_identical_payments_with_different_verified_bank_ids_survive(self):
        data=self.case();data['loads'][1]['sha256']='b'*64
        data['cashflow']['operations'][0]['bank_operation_id']='REAL-1'
        data['cashflow']['operations'][1]['bank_operation_id']='REAL-2'
        code,result,_=self.cli(data)
        self.assertEqual(code,0)
        self.assertTrue(result['clean'])
        self.assertEqual(result['summary']['totals']['credit']['value'],'200.20')
        self.assertEqual(len(result['registry']['operations']),2)
        self.assertEqual(result['confirmed_duplicates'],[])
        self.assertEqual(result['unresolved_matches'],[])

    def test_strong_identity_with_conflicting_facts_never_drops_a_row_automatically(self):
        for field,changed in [('counterparty_account','OTHER'),('date','2026-01-16'),
                              ('credit','101.10'),('category','other_in')]:
            with self.subTest(field=field):
                data=self.case();data['loads'][1]['sha256']='b'*64
                for row in data['cashflow']['operations']:
                    row.update(bank_operation_id='BANK-1',counterparty_account='CLIENT')
                data['cashflow']['operations'][1][field]=changed
                code,result,_=self.cli(data)
                self.assertEqual(code,2)
                self.assertEqual(len(result['registry']['operations']),2)
                self.assertEqual(result['confirmed_duplicates'],[])
                self.assertEqual(result['unresolved_matches'][0]['code'],'identity_conflict')
                self.assertIn(field,result['unresolved_matches'][0]['differing_fields'])

    def test_analyst_distinct_decision_preserves_two_real_payments_and_is_repeatable(self):
        data=self.case();data['loads'][1]['sha256']='b'*64
        _,pending,_=self.cli(data)
        decision={'ids':['a','b'],'action':'distinct','reason':'Два платежа сверены с оригиналом',
                  'fingerprints':pending['operation_fingerprints']}
        data['decisions']=[decision]
        code,result,first=self.cli(data)
        self.assertEqual(code,0)
        self.assertEqual(result['summary']['totals']['credit']['value'],'200.20')
        self.assertEqual(result['unresolved_matches'],[])
        self.assertEqual(result['applied_decisions'],[decision])
        self.assertEqual(self.cli(data)[2],first)

    def test_duplicate_decision_selects_confirmed_revision_and_keeps_all_evidence(self):
        data=self.case();data['loads'][1]['sha256']='b'*64
        for row in data['cashflow']['operations']:
            row['bank_operation_id']='BANK-1'
        data['cashflow']['operations'][1]['credit']='101.10'
        data['cashflow']['operations'][1]['transfer_evidence']={'document':'DOC-1','confirmed':False}
        _,pending,_=self.cli(data)
        data['decisions']=[{'ids':['a','b'],'action':'duplicate','keep_id':'b',
                            'reason':'Сумма и реквизиты редакции b сверены по оригиналу',
                            'fingerprints':pending['operation_fingerprints']}]
        code,result,_=self.cli(data)
        self.assertEqual(code,0)
        self.assertEqual(result['summary']['totals']['credit']['value'],'101.10')
        self.assertEqual(result['registry']['operations'][0]['id'],'b')
        self.assertEqual(result['registry']['operations'][0]['transfer_evidence'],{'document':'DOC-1','confirmed':False})
        self.assertEqual(result['confirmed_duplicates'][0]['basis'],'analyst_decision')
        self.assertEqual(result['raw_operations'],data['cashflow']['operations'])
        self.assertEqual({a['operation_id'] for a in result['lineage'][0]['appearances']},{'a','b'})

    def test_stale_decision_is_rejected_even_when_pair_no_longer_looks_similar(self):
        for change in ('amount','load-version','source','extra-evidence'):
            with self.subTest(change=change):
                data=self.case();data['loads'][1]['sha256']='b'*64
                _,pending,_=self.cli(data)
                decision={'ids':['a','b'],'action':'duplicate','keep_id':'a','reason':'Связь сверена',
                          'fingerprints':pending['operation_fingerprints']}
                data['decisions']=[decision]
                if change=='amount': data['cashflow']['operations'][1]['credit']='101.10'
                elif change=='load-version': data['loads'][1]['sha256']='c'*64
                elif change=='source': data['cashflow']['sources'][1]['locator']='исправленный источник'
                else: data['cashflow']['operations'][1]['transfer_evidence']={'document':'NEW'}
                code,result,_=self.cli(data)
                self.assertEqual(code,2)
                self.assertFalse(result['clean'])
                self.assertIsNone(result['summary'])
                self.assertEqual(result['applied_decisions'],[])
                self.assertIn('stale_decision',[issue['code'] for issue in result['issues']])
                self.assertEqual(result['decisions'],[decision])

    def test_invalid_registry_load_metadata_and_decisions_have_explicit_diagnostics(self):
        changes=[('schema',lambda d:d.update(schema_version='unknown')),
                 ('loads-type',lambda d:d.update(loads={})),
                 ('duplicate-load',lambda d:d['loads'].append(copy.deepcopy(d['loads'][0]))),
                 ('hash',lambda d:d['loads'][0].update(sha256='not-a-hash')),
                 ('source',lambda d:d['loads'][0].update(source='MISSING')),
                 ('load',lambda d:d['cashflow']['operations'][0].update(load_id='MISSING')),
                 ('row',lambda d:d['cashflow']['operations'][0].update(source_row='')),
                 ('bank-id',lambda d:d['cashflow']['operations'][0].update(bank_operation_id=[])),
                 ('amount-range',lambda d:d['cashflow']['operations'][0].update(credit='1E100')),
                 ('decision-type',lambda d:d.update(decisions={})),
                 ('decision-id',lambda d:d.update(decisions=[{'ids':['a','MISSING'],'action':'duplicate','reason':'Проверено','keep_id':'a','fingerprints':{}}])),
                 ('reason',lambda d:d.update(decisions=[{'ids':['a','b'],'action':'distinct','reason':'','fingerprints':{}}])),
                 ('keep',lambda d:d.update(decisions=[{'ids':['a','b'],'action':'duplicate','reason':'Проверено','keep_id':'MISSING','fingerprints':{}}]))]
        for name,change in changes:
            with self.subTest(name=name):
                data=self.case();change(data)
                code,result,_=self.cli(data)
                self.assertEqual(code,2)
                self.assertEqual(result['issues'][0]['code'],'invalid_input')
                self.assertTrue(result['issues'][0]['message'])

    def test_transitive_distinct_or_multiple_selected_revisions_are_not_silently_merged(self):
        for mode in ('distinct-inside-automatic-group','different-keep-revisions'):
            with self.subTest(mode=mode):
                data=self.case();data['loads'].append({'id':'L3','source':'S2','sha256':data['loads'][1]['sha256']})
                data['cashflow']['operations'].append(self.row('c','S2','L3','2'))
                if mode=='distinct-inside-automatic-group':
                    data['cashflow']['operations'][1]['bank_operation_id']='BANK-1'
                    data['cashflow']['operations'][2]['bank_operation_id']='BANK-1'
                else:
                    data['loads'][1]['sha256']='b'*64;data['loads'][2]['sha256']='b'*64
                _,pending,_=self.cli(data)
                snapshots=pending['operation_fingerprints']
                def decision(ids,action,keep=None):
                    value={'ids':ids,'action':action,'reason':'Проверено аналитиком',
                           'fingerprints':{uid:snapshots[uid] for uid in ids}}
                    if keep:value['keep_id']=keep
                    return value
                data['decisions']=([decision(['a','c'],'distinct')] if mode=='distinct-inside-automatic-group'
                                   else [decision(['a','b'],'duplicate','a'),decision(['b','c'],'duplicate','b')])
                code,result,_=self.cli(data)
                self.assertEqual(code,2)
                self.assertIsNone(result['summary'])
                self.assertEqual(len(result['registry']['operations']),3)
                self.assertEqual(result['applied_decisions'],[])
                self.assertIn('conflicting_decisions',[item['code'] for item in result['issues']])

    def test_consistent_transitive_duplicate_and_distinct_decisions_recalculate_once(self):
        for action,expected in [('duplicate','100.10'),('distinct','200.20')]:
            with self.subTest(action=action):
                data=self.case();data['loads'][1]['sha256']='b'*64
                data['cashflow']['operations'].append(self.row('c','S2','L2','2'))
                _,pending,_=self.cli(data);snapshots=pending['operation_fingerprints']
                data['decisions']=[{'ids':['a','b'],'action':'duplicate','keep_id':'a','reason':'Один платёж',
                                    'fingerprints':{uid:snapshots[uid] for uid in ['a','b']}}]
                ids=['a','c'] if action=='duplicate' else ['b','c']
                decision={'ids':ids,'action':action,'reason':'Связь проверена',
                          'fingerprints':{uid:snapshots[uid] for uid in ids}}
                if action=='duplicate':decision['keep_id']='a'
                data['decisions'].append(decision)
                code,result,_=self.cli(data)
                self.assertEqual(code,0)
                self.assertEqual(result['unresolved_matches'],[])
                self.assertEqual(result['summary']['totals']['credit']['value'],expected)

    def test_readding_identical_export_with_previous_local_id_does_not_double_count(self):
        data=self.case();data['cashflow']['operations'][1]['id']='a'
        code,result,_=self.cli(data)
        self.assertEqual(code,0)
        self.assertEqual(result['summary']['totals']['credit']['value'],'100.10')
        self.assertEqual(result['summary']['pdf_patch']['bank_scope']['operations_count'],1)
        self.assertEqual(len(result['lineage'][0]['appearances']),2)
        data['cashflow']['operations'][1]['credit']='101.10'
        code,result,_=self.cli(data)
        self.assertEqual(code,2)
        self.assertEqual(result['issues'][0]['code'],'invalid_input')

    def test_clean_registry_keeps_lineage_through_existing_cashflow_cli_and_bank_audit(self):
        data=self.case()
        data['cashflow']['sources'].append({'id':'CONTROL','file':'synthetic-bank.pdf','locator':'итоги'})
        data['cashflow']['bank_controls']=[{'account':'A','source':'CONTROL','opening':'0.00','closing':'100.10',
                                           'debit':'0.00','credit':'100.10','bank_count':1}]
        code,result,_=self.cli(data)
        self.assertEqual(code,0)
        self.assertEqual(result['registry']['bank_controls'],data['cashflow']['bank_controls'])
        self.assertEqual(result['registry']['operations'][0]['statement_appearances'],result['lineage'][0]['appearances'])
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'registry.json';output=Path(tmp)/'summary.json'
            source.write_text(json.dumps(result['registry'],ensure_ascii=False))
            proc=subprocess.run([sys.executable,str(SCRIPT.with_name('calculate_cashflow.py')),str(source),str(output)],capture_output=True,text=True)
            summary=json.loads(output.read_text())
            self.assertEqual(proc.returncode,0,summary)
            self.assertEqual(summary['totals']['credit']['value'],'100.10')
            self.assertEqual(summary['operations'][0]['statement_appearances'],result['lineage'][0]['appearances'])
            self.assertEqual(summary['pdf_patch']['bank']['closing']['value'],100.10)

    def test_adding_new_statement_preserves_unchanged_decisions_and_recomputes_months(self):
        data=self.case();data['loads'][1]['sha256']='b'*64
        _,pending,_=self.cli(data)
        decision={'ids':['a','b'],'action':'distinct','reason':'Подтверждены два платежа',
                  'fingerprints':pending['operation_fingerprints']}
        data['decisions']=[decision]
        self.assertEqual(self.cli(data)[0],0)
        data['cashflow']['sources'].append({'id':'S3','file':'new-synthetic.csv','locator':'строка 1'})
        data['loads'].append({'id':'L3','source':'S3','sha256':'c'*64})
        data['cashflow']['operations'].append(self.row('c','S3','L3',date='2026-02-03',credit='50.25'))
        code,result,_=self.cli(data)
        self.assertEqual(code,0)
        self.assertEqual(result['applied_decisions'],[decision])
        self.assertEqual(result['summary']['totals']['credit']['value'],'250.45')
        self.assertEqual([month['totals']['credit']['value'] for month in result['summary']['months']],['200.20','50.25'])
        self.assertEqual(result['summary']['pdf_patch']['bank_scope']['operations_count'],3)

    def test_conflicting_decisions_with_repeated_original_id_keep_diagnostic_output(self):
        data=self.case();data['loads'][1]['sha256']='b'*64
        data['cashflow']['operations'] += [copy.deepcopy(data['cashflow']['operations'][0]),self.row('c','S2','L2','2')]
        _,pending,_=self.cli(data);snapshots=pending['operation_fingerprints']
        data['decisions']=[{'ids':ids,'action':'duplicate','keep_id':keep,'reason':'Сверено',
                            'fingerprints':{uid:snapshots[uid] for uid in ids}}
                           for ids,keep in [(['a','b'],'a'),(['b','c'],'b')]]
        code,result,_=self.cli(data)
        self.assertEqual(code,2)
        self.assertIsNone(result['summary'])
        self.assertIn('conflicting_decisions',[issue['code'] for issue in result['issues']])
        self.assertEqual(len(result['raw_operations']),4)
        self.assertEqual(len(result['registry']['operations']),3)

    def test_output_cannot_overwrite_input_or_referenced_export_even_by_hardlink(self):
        for kind in ('input','export','export-hardlink'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                folder=Path(tmp);source=folder/'in.json';export=folder/'original.csv'
                export.write_text('Банковский исходник 100.10\n')
                data=self.case();data['cashflow']['sources'][0]['file']=str(export)
                source.write_text(json.dumps(data,ensure_ascii=False))
                before_source=source.read_bytes();before_export=export.read_bytes()
                target=source if kind=='input' else export
                if kind=='export-hardlink':
                    target=folder/'alias.json';target.hardlink_to(export)
                proc=subprocess.run([sys.executable,str(SCRIPT),str(source),str(target)],capture_output=True,text=True)
                self.assertEqual(proc.returncode,2)
                self.assertEqual(source.read_bytes(),before_source)
                self.assertEqual(export.read_bytes(),before_export)
                self.assertTrue(proc.stderr.strip())

    def test_missing_bank_id_cannot_bridge_conflicting_ids_into_clean_auto_group(self):
        data=self.case()
        data['cashflow']['operations'][0]['bank_operation_id']='BANK-1'
        data['cashflow']['operations'].append(self.row('c','S2','L2',bank_operation_id='BANK-2'))
        code,result,_=self.cli(data)
        self.assertEqual(code,2)
        self.assertFalse(result['clean'])
        self.assertIsNone(result['summary'])
        self.assertIsNone(result['provisional_summary']['pdf_patch'])
        self.assertEqual(len(result['registry']['operations']),3)
        self.assertEqual(result['confirmed_duplicates'],[])
        conflict=next(item for item in result['unresolved_matches'] if item['code']=='identity_conflict')
        self.assertEqual(conflict['ids'],['a','c'])
        self.assertIn('bank_operation_id',conflict['differing_fields'])
        self.assertEqual(result['raw_operations'],data['cashflow']['operations'])
        data['decisions']=[{'ids':['a','c'],'action':'duplicate','keep_id':'a',
                            'reason':'Редакция BANK-1 сверена, BANK-2 ошибочна',
                            'fingerprints':{uid:result['operation_fingerprints'][uid] for uid in ['a','c']}}]
        code,resolved,_=self.cli(data)
        self.assertEqual(code,0)
        self.assertEqual(resolved['summary']['totals']['credit']['value'],'100.10')
        self.assertEqual(resolved['registry']['operations'][0]['bank_operation_id'],'BANK-1')
        self.assertEqual(len(resolved['lineage'][0]['appearances']),3)

    def test_all_appearances_of_same_local_id_must_have_consistent_known_bank_id(self):
        for bank_ids in [('BANK-1',None,'BANK-2'),(None,'BANK-1','BANK-2'),('BANK-1','BANK-2',None)]:
            with self.subTest(bank_ids=bank_ids):
                data=self.case()
                data['cashflow']['operations']=[self.row('a'),self.row('a','S2','L2'),self.row('a','S2','L2')]
                for row,bank_id in zip(data['cashflow']['operations'],bank_ids):
                    if bank_id:row['bank_operation_id']=bank_id
                code,result,_=self.cli(data)
                self.assertEqual(code,2)
                self.assertFalse(result['clean'])
                self.assertIsNone(result.get('summary'))
                self.assertEqual(result['issues'][0]['code'],'invalid_input')
                self.assertTrue(result['issues'][0]['message'])
