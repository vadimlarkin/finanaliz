import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/borrower-financial-analysis/scripts/check_cashflow.py'
FIXTURES = ROOT / 'test-cases/ip-cashflow-20261027/fixtures'


class CashflowAuditContract(unittest.TestCase):
    def audit(self, rows=None, controls=None):
        with tempfile.TemporaryDirectory() as tmp:
            bank = FIXTURES / 'bank-controls.json'
            transactions = FIXTURES / 'classified-transactions.csv'
            if rows is not None:
                transactions = Path(tmp) / 'transactions.csv'
                with transactions.open('w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
            if controls is not None:
                bank = Path(tmp) / 'controls.json'
                bank.write_text(json.dumps(controls))
            before = transactions.read_bytes()
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(transactions), '--bank-controls', str(bank)],
                capture_output=True, text=True)
            self.assertEqual(transactions.read_bytes(), before)
            self.assertTrue(result.stdout.strip(), result.stderr)
            return result.returncode, json.loads(result.stdout)

    def rows(self):
        with (FIXTURES / 'classified-transactions.csv').open() as f:
            return list(csv.DictReader(f))

    def test_cli_checks_categories_and_bank_totals_without_netting_refunds(self):
        code, report = self.audit()
        self.assertEqual(code, 0, report)
        self.assertTrue(report['ok'])
        self.assertEqual(report['totals']['debit'], '2424.00')
        self.assertEqual(report['totals']['credit'], '2812.00')
        self.assertEqual(report['totals']['by_category']['проценты по долгу'],
                         {'debit': '200.00', 'credit': '5.00'})
        self.assertEqual(report['checks']['bank_accounts'], 2)
        self.assertEqual(report['checks']['operation_count'], 13)

    def test_rejects_missing_explicit_interest_even_when_bank_totals_match(self):
        rows = self.rows()
        for row in rows:
            if row['id'] in ('SYN-05', 'SYN-06'):
                row['category'] = 'оплаты контрагентам, экономический вид уточняется'
            if row['id'] == 'SYN-08':
                row['category'] = 'проценты по долгу'
        code, report = self.audit(rows)
        self.assertEqual(code, 2, report)
        self.assertEqual({x['operation_id'] for x in report['issues']},
                         {'SYN-05', 'SYN-06', 'SYN-08'})
        self.assertEqual(report['totals']['debit'], '2424.00')

    def test_penalties_fees_and_principal_cannot_be_mixed_with_interest(self):
        for identifier, wrong, expected_code in [
            ('SYN-07', 'проценты по долгу', 'penalty_misclassified'),
            ('SYN-09', 'погашение основного долга', 'fee_misclassified'),
            ('SYN-10', 'проценты по долгу', 'principal_misclassified'),
        ]:
            with self.subTest(operation=identifier):
                rows = self.rows()
                next(x for x in rows if x['id'] == identifier)['category'] = wrong
                code, report = self.audit(rows)
                self.assertEqual(code, 2, report)
                self.assertEqual(report['issues'][0]['code'], expected_code)

    def test_transfer_pair_requires_both_counterparty_accounts_and_same_date(self):
        rows = self.rows()
        next(x for x in rows if x['id'] == 'SYN-04')['counterparty_account'] = 'OTHER-ACCOUNT'
        code, report = self.audit(rows)
        self.assertEqual(code, 2, report)
        self.assertIn('invalid_transfer_pair', [x['code'] for x in report['issues']])
        self.assertEqual(report['totals']['credit'], '2812.00')

    def test_rejects_short_csv_row_with_missing_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'short.csv'
            for line in ['T1,fictional,1,A,2026-09-01,10,0,Оплата',
                         'T1,fictional,1,A,2026-09-01,10,0,Оплата,прочее,ЛИШНЕЕ']:
                with self.subTest(line=line):
                    p.write_text('id,source,page,account,date,debit,credit,purpose,category\n' + line + '\n')
                    proc = subprocess.run([sys.executable, str(SCRIPT), str(p)], capture_output=True, text=True)
                    report = json.loads(proc.stdout)
                    self.assertEqual(proc.returncode, 2, report)
                    self.assertEqual(report['issues'][0]['code'], 'invalid_operation')

    def test_short_loan_return_name_does_not_make_it_interest(self):
        rows = self.rows()
        row = next(x for x in rows if x['id'] == 'SYN-08')
        row['purpose'] = 'Возврат займа с процентами'
        row['category'] = 'проценты по долгу'
        code, report = self.audit(rows)
        self.assertEqual(code, 2, report)
        self.assertIn('loan_return_is_not_interest', [x['code'] for x in report['issues']])

    def test_reconciles_turnovers_balances_and_counts_independently(self):
        controls = json.loads((FIXTURES / 'bank-controls.json').read_text())
        for field, value, issue in [
            ('closing', '0.00', 'bank_balance_mismatch'),
            ('debit', '2425.00', 'bank_turnover_mismatch'),
            ('bank_count', 10, 'bank_count_mismatch'),
        ]:
            with self.subTest(field=field):
                changed = [dict(x) for x in controls]
                changed[0][field] = value
                code, report = self.audit(controls=changed)
                self.assertEqual(code, 2, report)
                self.assertIn(issue, {x['code'] for x in report['issues']})

    def test_normalizes_case_hidden_separators_and_date_formats(self):
        rows = self.rows()
        next(x for x in rows if x['id'] == 'SYN-05')['purpose'] = 'ВЫПЛАТА\u200b   ПРОЦЕНТОВ по договору'
        next(x for x in rows if x['id'] == 'SYN-04')['date'] = '02.09.2026'
        code, report = self.audit(rows)
        self.assertEqual(code, 0, report)
        self.assertEqual(report['checks']['matched_pairs'], 1)
        self.assertEqual(report['totals']['paired_transfer_each_side'], '300.00')
        self.assertEqual(report['totals']['after_matched_transfers'],
                         {'debit': '2124.00', 'credit': '2512.00'})

    def test_rejects_invalid_money_duplicate_ids_and_mixed_currency(self):
        for change in [{'debit': 'NaN'}, {'debit': '-1'}, {'debit': '0.001'},
                       {'id': 'SYN-01'}, {'currency': 'USD'}, {'date': '2026-02-30'}]:
            with self.subTest(change=change):
                rows = self.rows()
                if 'currency' in change:
                    for row in rows:
                        row['currency'] = 'RUB'
                rows[4].update(change)
                code, report = self.audit(rows)
                self.assertEqual(code, 2, report)
                self.assertIn('invalid_operation', {x['code'] for x in report['issues']})

    def test_missing_pair_and_refund_misclassification_are_detected(self):
        rows = self.rows()
        next(x for x in rows if x['id'] == 'SYN-03')['pair_id'] = ''
        next(x for x in rows if x['id'] == 'SYN-12')['category'] = 'оплаты клиентов'
        code, report = self.audit(rows)
        self.assertEqual(code, 2, report)
        self.assertTrue({'missing_transfer_pair', 'invalid_transfer_pair', 'interest_misclassified'}
                        .issubset({x['code'] for x in report['issues']}))

    def test_mention_of_absent_penalties_does_not_create_a_penalty_payment(self):
        rows = self.rows()
        next(x for x in rows if x['id'] == 'SYN-10')['purpose'] = 'Погашение основного долга без пеней и штрафов'
        code, report = self.audit(rows)
        self.assertEqual(code, 0, report)
