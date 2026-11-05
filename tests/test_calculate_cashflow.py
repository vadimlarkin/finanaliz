import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/borrower-financial-analysis/scripts/calculate_cashflow.py'


def case():
    rows = []
    for oid, date, debit, credit, category in [
        ('r1', '2026-01-15', '0', '100.10', 'revenue'),
        ('p1', '2026-01-16', '20.01', '0', 'principal'),
        ('i1', '2026-01-17', '2.02', '0', 'interest'),
        ('f1', '2026-01-18', '0.03', '0', 'fee'),
        ('ir', '2026-01-19', '0', '0.11', 'interest'),
        ('fr', '2026-01-20', '0', '0.01', 'fee'),
        ('r2', '2026-02-03', '0', '50.25', 'revenue'),
    ]:
        rows.append(dict(id=oid, date=date, debit=debit, credit=credit, category=category,
                         category_status='confirmed', source='B', page=str(len(rows)+1),
                         account='A', purpose='Платёж по проверенному реестру', creditor_kind='bank'))
    return {'schema_version': 'local-cashflow-1.0',
            'period': {'start': '2026-01-15', 'end': '2026-02-28'},
            'accounts': [{'account': 'A', 'owner_id': 'IP'}], 'coverage': {'complete': True},
            'sources': [{'id': 'B', 'file': 'synthetic.csv', 'locator': 'строки 1–7'},
                        {'id': 'C', 'file': 'synthetic-bank.pdf', 'locator': 'итоги банка'}],
            'operations': rows,
            'bank_controls': [{'account': 'A', 'opening': '10.00', 'closing': '138.41',
                               'debit': '22.06', 'credit': '150.47', 'bank_count': 7, 'source': 'C'}]}


class CalculateCashflowCLI(unittest.TestCase):
    def run_case(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'input.json'; out = Path(tmp)/'result.json'
            raw = json.dumps(data, ensure_ascii=False).encode(); source.write_bytes(raw)
            proc = subprocess.run([sys.executable, str(SCRIPT), str(source), str(out)], capture_output=True, text=True)
            self.assertEqual(source.read_bytes(), raw)
            self.assertTrue(out.exists(), proc.stderr)
            return proc.returncode, json.loads(out.read_text()), out.read_bytes()

    def test_exact_totals_months_and_partial_month(self):
        code, result, _ = self.run_case(case())
        self.assertEqual(code, 0, result)
        self.assertEqual(result['totals']['credit']['value'], '150.47')
        self.assertEqual(result['totals']['debit']['value'], '22.06')
        self.assertEqual(result['totals']['cash_change']['value'], '128.41')
        self.assertEqual([m['revenue']['value'] for m in result['months']], ['100.10', '50.25'])
        self.assertEqual([m['partial'] for m in result['months']], [True, False])
        self.assertEqual(result['months'][0]['period'], {'start':'2026-01-15','end':'2026-01-31'})

    def test_payments_and_returns_are_separate_and_principal_is_not_expense(self):
        code, result, _ = self.run_case(case())
        self.assertEqual(code, 0)
        expected = {'principal_paid':'20.01', 'interest_paid':'2.02', 'interest_returned':'0.11',
                    'fees_paid':'0.03', 'fees_returned':'0.01'}
        self.assertEqual({k:result['components'][k]['value'] for k in expected}, expected)
        self.assertEqual(result['components']['operating_outflows']['value'], '0.03')
        self.assertEqual(result['categories']['interest']['debit']['operation_ids'], ['i1'])

    def test_unresolved_categories_and_incomplete_coverage_remain_explicit(self):
        data = case(); data['coverage'] = {'complete':False, 'missing_months':['2026-02']}
        data['operations'][0]['category_status'] = 'unresolved'
        code, result, _ = self.run_case(data)
        self.assertEqual(code, 0, result)
        self.assertFalse(result['complete'])
        self.assertEqual(result['unresolved']['credit']['value'], '100.10')
        self.assertEqual(result['unresolved']['credit']['operation_ids'], ['r1'])
        self.assertEqual(result['months'][0]['revenue']['value'], '0.00')
        self.assertIsNone(result['pdf_patch']['monthly_revenue'][1]['fact']['value'])
        self.assertIsNone(result['free_cashflow'])
        self.assertTrue(result['pdf_patch']['bank_scope']['incomplete'])

    def test_independent_bank_mismatch_is_reported_without_replacing_it_by_rows(self):
        data = case(); data['bank_controls'][0]['credit'] = '151.47'
        code, result, _ = self.run_case(data)
        self.assertEqual(code, 2)
        self.assertFalse(result['audit']['ok'])
        self.assertIn('bank_turnover_mismatch', [i['code'] for i in result['audit']['issues']])
        self.assertEqual(result['bank_controls'][0]['credit'], '151.47')
        self.assertEqual(result['totals']['credit']['value'], '150.47')
        data.pop('bank_controls'); code, result, _ = self.run_case(data)
        self.assertEqual(code, 0)
        self.assertFalse(result['audit']['checks']['bank_controls_supplied'])
        self.assertIsNone(result['balances']['opening']['value'])

    def test_confirmed_pairs_exclude_both_sides_and_preserve_raw_totals(self):
        data = case(); data.pop('bank_controls'); data['accounts'].append({'account':'B2','owner_id':'IP'})
        for oid, account, counterparty, debit, credit in [('t1','A','B2','5.00','0'),('t2','B2','A','0','5.00')]:
            row = copy.deepcopy(data['operations'][0])
            row.update(id=oid, account=account, counterparty_account=counterparty,
                       debit=debit, credit=credit, category='own_transfer_matched', pair_id='pair-1')
            data['operations'].append(row)
        code, result, _ = self.run_case(data)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['totals']['credit']['value'], '155.47')
        self.assertEqual(result['after_matched_transfers']['credit']['value'], '150.47')
        self.assertEqual(result['after_matched_transfers']['debit']['value'], '22.06')
        self.assertEqual(result['transfers']['operation_ids'], ['t1','t2'])
        self.assertEqual(result['months'][0]['after_matched_transfers']['debit']['value'], '22.06')
        self.assertEqual(result['pdf_patch']['bank']['internal_transfers']['value'], 5.0)
        data['operations'][-1]['debit']='0'; data['operations'][-1]['credit']='6'
        code, result, _ = self.run_case(data)
        self.assertEqual(code, 2)
        self.assertIsNone(result['after_matched_transfers'])

    def test_invalid_scope_money_status_and_sources_are_diagnosed(self):
        changes = [
            lambda d:d.update(schema_version='unexpected'),
            lambda d:d['period'].update(end='2026-01-01'),
            lambda d:d['operations'][0].update(date='2025-01-15'),
            lambda d:d['operations'][0].update(source='missing'),
            lambda d:d['operations'][0].update(debit='NaN'),
            lambda d:d['operations'][0].update(credit='0.001'),
            lambda d:d['operations'][0].update(account='missing'),
            lambda d:d['operations'][0].update(category_status='guess'),
            lambda d:d['accounts'].append(d['accounts'][0]),
            lambda d:d['coverage'].update(complete='yes'),
            lambda d:d['bank_controls'][0].update(source='missing'),
            lambda d:d['sources'].append(d['sources'][0]),
        ]
        for change in changes:
            with self.subTest(change=change):
                data = case(); change(data); code, result, _ = self.run_case(data)
                self.assertEqual(code, 2, result)
                self.assertFalse(result['ok'])
                self.assertTrue(result.get('issues') or result.get('audit',{}).get('issues'))

    def test_monthly_categories_and_provenance_are_reproducible(self):
        data = case(); _, first, raw = self.run_case(data); _, second, raw2 = self.run_case(data)
        self.assertEqual(raw, raw2)
        self.assertEqual(first['months'][0]['totals']['credit']['value'], '100.22')
        self.assertEqual(first['months'][1]['totals']['credit']['value'], '50.25')
        self.assertEqual(first['months'][0]['categories']['fee']['credit']['operation_ids'], ['fr'])
        self.assertEqual(first['operations'], data['operations'])
        self.assertEqual(first['totals']['credit']['accounts'], ['A'])
        self.assertEqual(first['totals']['credit']['period'], data['period'])

    def test_numeric_patch_is_accepted_by_current_ip_form_without_recalculation(self):
        code, result, _ = self.run_case(case()); self.assertEqual(code, 0)
        sys.path.insert(0,str(SCRIPT.parent))
        from ip_investor_report import prepare
        # Keep unrelated template evidence distinct from the new banking sources.
        data = json.loads((ROOT/'examples/ip-investor-v3.json').read_text().replace('"B"','"LEGACY-B"').replace('"C"','"LEGACY-C"'))
        for key,value in result['pdf_patch'].items():
            if key in {'bank','flows','misc_breakdown','debt_payments'}:
                data[key].update(value)
            else:
                data[key] = value
        merged_sources = {s['id']:s for s in data['sources']}
        merged_sources.update({s['id']:s for s in result['sources']})
        data['sources'] = list(merged_sources.values())
        data['schema_version'] = 'ip-investor-3.1'
        prepared = prepare(data)
        self.assertEqual(prepared['layout_version'],'ip-investor-one-page-3.2')
        self.assertEqual(prepared['flows']['revenue']['value'],150.35)
        self.assertEqual(prepared['flows']['debt_payments']['value'],22.03)
        self.assertEqual(prepared['misc_breakdown']['fees_services']['value'],0.03)
        self.assertEqual(prepared['debt_payments']['bank']['value'],22.03)
        self.assertEqual(prepared['calculations']['monthly_client_average']['value'],50.25)

    def test_refunds_reduce_revenue_once_and_unusual_directions_remain_unresolved(self):
        data = case(); data.pop('bank_controls')
        row = copy.deepcopy(data['operations'][0]); row.update(id='refund',category='customer_refund',debit='1.05',credit='0')
        data['operations'].append(row)
        _, result, _ = self.run_case(data)
        self.assertEqual(result['pdf_patch']['customer_refunds']['value'],1.05)
        self.assertEqual(result['pdf_patch']['flows']['revenue']['value'],149.30)
        self.assertEqual(result['months'][0]['revenue']['value'],'99.05')
        self.assertEqual(result['pdf_patch']['flows']['other_out']['value'],0.03)
        data['operations'][0].update(debit='100.10',credit='0')
        _, result, _ = self.run_case(data)
        self.assertFalse(result['complete'])
        self.assertIn('r1',result['unresolved']['debit']['operation_ids'])
        self.assertIsNone(result['pdf_patch']['flows']['revenue']['value'])

    def test_pair_requires_confirmed_categories_and_same_owner(self):
        for change in ('owner','status'):
            data = case(); data.pop('bank_controls')
            data['accounts'].append({'account':'B2','owner_id':'OTHER' if change=='owner' else 'IP'})
            for oid, account, counterparty, debit, credit in [('t1','A','B2','5.00','0'),('t2','B2','A','0','5.00')]:
                row = copy.deepcopy(data['operations'][0])
                row.update(id=oid, account=account, counterparty_account=counterparty,
                           debit=debit,credit=credit,category='own_transfer_matched',pair_id='pair-1')
                if change=='status': row['category_status']='unresolved'
                data['operations'].append(row)
            code, result, _ = self.run_case(data)
            self.assertEqual(code,2)
            self.assertIsNone(result['after_matched_transfers'])

    def test_unknown_category_and_missing_creditor_are_not_invented(self):
        data = case(); data['operations'][0]['category']='unverified_sale'
        _, result, _ = self.run_case(data)
        self.assertFalse(result['complete'])
        self.assertEqual(result['categories']['unverified_sale']['credit']['value'],'100.10')
        self.assertEqual(result['unresolved']['credit']['operation_ids'],['r1'])
        data = case(); data['operations'][1].pop('creditor_kind')
        _, result, _ = self.run_case(data)
        self.assertIsNone(result['pdf_patch']['debt_payments']['bank']['value'])
        self.assertIsNone(result['pdf_patch']['debt_payments']['nonbank']['value'])
        self.assertIsNone(result['pdf_patch']['debt_payments']['own_platform']['value'])

    def test_negative_revenue_month_and_long_window_keep_amounts_and_block_unsupported_pdf(self):
        data = case(); data.pop('bank_controls')
        data['period']['end']='2027-02-28'
        row = copy.deepcopy(data['operations'][0]);row.update(id='refund',category='customer_refund',debit='200.00',credit='0')
        data['operations'].append(row)
        _, result, _ = self.run_case(data)
        self.assertEqual(result['months'][0]['revenue']['value'],'-99.90')
        self.assertFalse(result['pdf_compatible'])
        self.assertIn('negative_monthly_revenue',result['pdf_limits'])
        self.assertIn('more_than_13_months',result['pdf_limits'])
        self.assertIsNone(result['pdf_patch']['monthly_revenue'][0]['fact']['value'])

    def test_bank_controls_cover_declared_zero_accounts_and_same_period(self):
        data = case();data['accounts'].append({'account':'EMPTY','owner_id':'IP'})
        code,result,_=self.run_case(data)
        self.assertEqual(code,2)
        self.assertIn('missing_bank_control',[i['code'] for i in result['audit']['issues']])
        data=case();data['bank_controls'][0]['period']={'start':'2026-01-01','end':'2026-02-28'}
        code,result,_=self.run_case(data)
        self.assertEqual(code,2)

    def test_conflicting_bank_controls_never_publish_confirmed_aggregate_balances(self):
        for mutation,expected_issue in [('duplicate','invalid_controls'),('closing','bank_balance_mismatch')]:
            with self.subTest(mutation=mutation):
                data=case()
                if mutation=='duplicate':data['bank_controls'].append(copy.deepcopy(data['bank_controls'][0]))
                else:data['bank_controls'][0]['closing']='999.99'
                original_controls=copy.deepcopy(data['bank_controls'])
                code,result,_=self.run_case(data)
                self.assertEqual(code,2)
                self.assertIn(expected_issue,[i['code'] for i in result['audit']['issues']])
                self.assertEqual(result['bank_controls'],original_controls)
                for key in ('opening','closing'):
                    self.assertIsNone(result['balances'][key]['value'])
                    self.assertIsNone(result['pdf_patch']['bank'][key]['value'])
                    self.assertEqual(result['pdf_patch']['bank'][key]['status'],'missing')
                    self.assertEqual(result['pdf_patch']['bank'][key]['sources'],['C'])
