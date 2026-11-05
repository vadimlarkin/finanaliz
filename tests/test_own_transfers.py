import copy
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'skills/borrower-financial-analysis/scripts/match_own_transfers.py'
AUDIT = ROOT/'skills/borrower-financial-analysis/scripts/check_cashflow.py'


class OwnTransfersCLI(unittest.TestCase):
    def case(self, cross_date=False):
        def row(uid, account, stamp, debit, credit, category, **extra):
            return {'id':uid,'source':'S','page':uid,'account':account,'date':stamp,
                    'debit':debit,'credit':credit,'purpose':'Перевод средств' if uid in {'OUT','IN'} else 'Расчёт',
                    'category':category,'category_status':'confirmed',**extra}
        return {'schema_version':'local-own-transfers-1.0','cashflow':{
            'schema_version':'local-cashflow-1.0','period':{'start':'2026-01-01','end':'2026-02-28'},
            'accounts':[{'account':'A','owner_id':'OWNER'},{'account':'B','owner_id':'OWNER'}],
            'coverage':{'complete':True},'sources':[{'id':'S','file':'synthetic.csv','locator':'1-4'}],
            'operations':[
                row('OUT','A','2026-01-31','100.01','0','own_out',counterparty_account='B',document_number='D-1',operation_date='2026-01-31'),
                row('IN','B','2026-02-01' if cross_date else '2026-01-31','0','100.01','own_in',counterparty_account='A',document_number='D-1',operation_date='2026-01-31'),
                row('SUP','A','2026-01-31','20.02','0','suppliers'),
                row('REV','B','2026-02-03','0','50.03','revenue')
            ]},'decisions':[]}

    def run_cli(self, data):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'input.json'; output=Path(temp)/'output.json'
            source.write_text(json.dumps(data,ensure_ascii=False))
            before=source.read_bytes()
            p=subprocess.run([sys.executable,str(SCRIPT),str(source),str(output)],capture_output=True,text=True)
            self.assertEqual(source.read_bytes(),before)
            self.assertTrue(output.exists(),p.stderr)
            return p.returncode,json.loads(output.read_text()),output.read_bytes()

    def decision(self, data, pair='P1', ids=('OUT','IN')):
        # Fingerprints follow the documented input contract, not the matching algorithm.
        snapshots={}
        for r in data['cashflow']['operations']:
            if r['id'] in ids:
                raw={k:v for k,v in r.items() if k not in {'category','category_status','pair_id','transfer_confirmation'}}
                snapshots[r['id']]=hashlib.sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return {'pair_id':pair,'operation_ids':list(ids),'action':'confirm','fingerprints':snapshots,
                'reason':'Аналитик сверил документ и встречные счета',
                'evidence':{'sources':['S'],'document_number':'D-1','operation_date':'2026-01-31'}}

    def test_confirmed_same_day_pair_excludes_both_sides_once(self):
        d=self.case();d['decisions']=[self.decision(d)]
        code,r,_=self.run_cli(d)
        self.assertEqual(code,0,r)
        self.assertEqual(len(r['confirmed_pairs']),1)
        self.assertEqual(r['calculation']['after_matched_transfers']['debit']['value'],'20.02')
        self.assertEqual(r['calculation']['after_matched_transfers']['credit']['value'],'50.03')
        self.assertEqual(r['calculation']['totals']['debit']['value'],'120.03')
        self.assertEqual(r['calculation']['totals']['credit']['value'],'150.04')

    def test_cross_date_evidence_passes_csv_audit_and_preserves_months(self):
        d=self.case(cross_date=True);d['decisions']=[self.decision(d)]
        code,r,_=self.run_cli(d)
        self.assertEqual(code,0,r)
        self.assertEqual([m['after_matched_transfers']['debit']['value'] for m in r['calculation']['months']],['20.02','0.00'])
        self.assertEqual([m['after_matched_transfers']['credit']['value'] for m in r['calculation']['months']],['0.00','50.03'])
        self.assertEqual([x['date'] for x in r['cashflow_input']['operations'][:2]],['2026-01-31','2026-02-01'])
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'rows.csv';rows=r['cashflow_input']['operations']
            fields=sorted(set().union(*(row for row in rows)))
            with p.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
            proc=subprocess.run([sys.executable,str(AUDIT),str(p)],capture_output=True,text=True)
            report=json.loads(proc.stdout)
            self.assertEqual(proc.returncode,0,report)
            self.assertEqual(report['checks']['matched_pairs'],1)
            self.assertEqual(report['transfer_evidence'][0]['basis'],'document_and_operation_date')

    def test_unconfirmed_or_incomplete_pair_is_never_excluded(self):
        for defect in ('no-decision','missing-counterparty','missing-side'):
            with self.subTest(defect=defect):
                d=self.case(cross_date=True)
                if defect=='missing-counterparty':
                    d['cashflow']['operations'][1].pop('counterparty_account')
                elif defect=='missing-side':
                    d['cashflow']['operations'].pop(1)
                code,r,_=self.run_cli(d)
                self.assertEqual(code,2,r)
                self.assertEqual(r['confirmed_pairs'],[])
                self.assertTrue(r['unresolved'])
                self.assertEqual(r['calculation']['after_matched_transfers']['debit']['value'],'120.03')

    def test_unlinked_or_stale_cross_date_decision_does_not_confirm(self):
        for defect in ('missing-document','wrong-operation-date','unknown-source','stale-source'):
            with self.subTest(defect=defect):
                d=self.case(cross_date=True);decision=self.decision(d);d['decisions']=[decision]
                if defect=='missing-document':decision['evidence'].pop('document_number')
                elif defect=='wrong-operation-date':decision['evidence']['operation_date']='2026-01-30'
                elif defect=='unknown-source':decision['evidence']['sources']=['UNKNOWN']
                else:d['cashflow']['operations'][0]['page']='new-source-line'
                code,r,_=self.run_cli(d)
                self.assertEqual(code,2)
                self.assertEqual(r['confirmed_pairs'],[],defect)
                self.assertTrue(r['issues'])
                self.assertEqual(r['calculation']['audit']['checks']['matched_pairs'],0)
                self.assertEqual(r['calculation']['after_matched_transfers']['debit']['value'],'120.03')

    def test_operation_cannot_be_confirmed_in_two_pairs(self):
        d=self.case(cross_date=True)
        extra=copy.deepcopy(d['cashflow']['operations'][1]);extra.update(id='IN-2',page='line-5')
        d['cashflow']['operations'].append(extra)
        d['decisions']=[self.decision(d),self.decision(d,pair='P2',ids=('OUT','IN-2'))]
        code,r,_=self.run_cli(d)
        self.assertEqual(code,2)
        self.assertEqual(r['confirmed_pairs'],[])
        self.assertIn('operation_reused',{x['code'] for x in r['issues']})
        self.assertEqual(r['calculation']['audit']['checks']['matched_pairs'],0)

    def test_ambiguous_candidates_keep_unmatched_third_operation_unresolved(self):
        d=self.case(cross_date=True)
        extra=copy.deepcopy(d['cashflow']['operations'][1]);extra.update(id='IN-2',page='line-5')
        d['cashflow']['operations'].append(extra)
        code,r,_=self.run_cli(d)
        self.assertEqual(code,2)
        self.assertEqual(len(r['candidates']),2)
        self.assertEqual(r['confirmed_pairs'],[])
        d['decisions']=[self.decision(d)]
        code,r,_=self.run_cli(d)
        self.assertEqual(code,2)
        self.assertEqual(len(r['confirmed_pairs']),1)
        self.assertTrue(any('IN-2' in item['operation_ids'] for item in r['unresolved']))
        self.assertEqual(r['calculation']['after_matched_transfers']['credit']['value'],'150.04')

    def test_outside_own_account_stays_separate_and_other_owner_is_not_own(self):
        d=self.case();d['cashflow']['operations'].pop(1)
        d['cashflow']['operations'][0].update(counterparty_account='EXTERNAL',category='other_out')
        d['external_accounts']=[{'account':'EXTERNAL','owner_id':'OWNER'}]
        code,r,_=self.run_cli(d)
        self.assertEqual(code,0,r)
        self.assertEqual(r['cashflow_input']['operations'][0]['category'],'own_out')
        self.assertEqual(r['outside_transfers'][0]['operation_id'],'OUT')
        self.assertEqual(r['calculation']['after_matched_transfers']['debit']['value'],'120.03')
        d['external_accounts'][0]['owner_id']='OTHER'
        code,r,_=self.run_cli(d)
        self.assertEqual(code,0,r)
        self.assertEqual(r['outside_transfers'],[])
        self.assertEqual(r['cashflow_input']['operations'][0]['category'],'other_out')

    def test_legacy_same_day_pair_stays_valid_and_cross_date_needs_confirmation(self):
        d=self.case()
        for row in d['cashflow']['operations'][:2]:
            row.update(pair_id='LEGACY',category='own_transfer_matched')
        code,r,_=self.run_cli(d)
        self.assertEqual(code,0,r)
        self.assertEqual(r['calculation']['audit']['checks']['matched_pairs'],1)
        self.assertEqual(r['confirmed_pairs'][0]['basis'],'legacy_same_day')
        d['cashflow']['operations'][1]['date']='2026-02-01'
        code,r,_=self.run_cli(d)
        self.assertEqual(code,2)
        self.assertEqual(r['confirmed_pairs'],[])
        self.assertEqual(r['calculation']['audit']['checks']['matched_pairs'],0)

    def test_csv_rejects_tampered_or_missing_cross_date_confirmation(self):
        d=self.case(cross_date=True);d['decisions']=[self.decision(d)]
        _,result,_=self.run_cli(d)
        for defect in ('missing','source-line','document','different-confirmations'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temp:
                rows=copy.deepcopy(result['cashflow_input']['operations'])
                if defect=='missing':
                    for row in rows[:2]:row.pop('transfer_confirmation')
                elif defect=='source-line':rows[0]['page']='changed'
                elif defect=='document':rows[0]['document_number']='OTHER-DOC'
                else:
                    changed=json.loads(rows[0]['transfer_confirmation']);changed['reason']='Другое основание'
                    rows[0]['transfer_confirmation']=json.dumps(changed)
                p=Path(temp)/'rows.csv';fields=sorted(set().union(*(row for row in rows)))
                with p.open('w',newline='') as f:
                    writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
                proc=subprocess.run([sys.executable,str(AUDIT),str(p)],capture_output=True,text=True)
                report=json.loads(proc.stdout)
                self.assertEqual(proc.returncode,2,report)
                self.assertEqual(report['checks']['matched_pairs'],0)
                self.assertEqual(report['totals']['after_matched_transfers'],{'debit':'120.03','credit':'150.04'})

    def test_dates_normalize_and_repeated_result_is_deterministic(self):
        d=self.case(cross_date=True)
        d['cashflow']['operations'][0]['date']='31.01.2026'
        # ISO-equivalent dates produce the documented canonical fingerprints.
        canonical=copy.deepcopy(d);canonical['cashflow']['operations'][0]['date']='2026-01-31'
        d['decisions']=[self.decision(canonical)]
        first=self.run_cli(d);second=self.run_cli(d)
        self.assertEqual(first[0],0,first[1])
        self.assertEqual(first[2],second[2])
        self.assertEqual(first[1]['cashflow_input']['operations'][0]['date'],'31.01.2026')

    def test_invalid_root_is_diagnostic_and_preserves_previous_source(self):
        for defect in ([],None,{'schema_version':'unsupported'}):
            with self.subTest(defect=defect):
                code,r,_=self.run_cli(defect)
                self.assertEqual(code,2)
                self.assertEqual(r['issues'][0]['code'],'invalid_input')

    def test_contradictory_pair_decisions_do_not_pick_a_verdict_by_order(self):
        d=self.case();a=self.decision(d);b=copy.deepcopy(a);b.update(pair_id='P2',action='reject')
        d['decisions']=[a,b]
        code,r,_=self.run_cli(d)
        self.assertEqual(code,2)
        self.assertEqual(r['confirmed_pairs'],[])
        self.assertIn('duplicate_operation_decision',{x['code'] for x in r['issues']})

    def test_saved_pair_rechecks_sources_and_owner_context_before_exclusion(self):
        d=self.case(cross_date=True)
        d['cashflow']['sources'].append({'id':'PROOF','file':'proof.pdf','locator':'1'})
        decision=self.decision(d);decision['evidence']['sources']=['PROOF'];d['decisions']=[decision]
        code,first,_=self.run_cli(d)
        self.assertEqual(code,0)
        saved={'schema_version':'local-own-transfers-1.0','cashflow':first['cashflow_input'],'decisions':[]}
        code,valid,_=self.run_cli(saved)
        self.assertEqual(code,0,valid)
        for defect in ('missing-proof-source','changed-owner'):
            with self.subTest(defect=defect):
                changed=copy.deepcopy(saved)
                if defect=='missing-proof-source':
                    changed['cashflow']['sources']=[s for s in changed['cashflow']['sources'] if s['id']!='PROOF']
                else:
                    for account in changed['cashflow']['accounts']:account['owner_id']='OTHER'
                code,r,_=self.run_cli(changed)
                self.assertEqual(code,2)
                self.assertEqual(r['confirmed_pairs'],[])
                self.assertIn('stale_saved_confirmation',{x['code'] for x in r['issues']})
                self.assertEqual(r['calculation']['audit']['checks']['matched_pairs'],0)
                self.assertEqual(r['calculation']['after_matched_transfers']['debit']['value'],'120.03')
                self.assertEqual(r['calculation']['after_matched_transfers']['credit']['value'],'150.04')
                self.assertEqual(r['original_cashflow'],changed['cashflow'])
                self.assertTrue(all(not row.get('pair_id') and not row.get('transfer_confirmation')
                                    for row in r['cashflow_input']['operations'][:2]))
