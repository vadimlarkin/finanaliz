import importlib.util
import copy
import json
import tempfile
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/borrower-financial-analysis/scripts/analyze.py'
sys.path.insert(0, str(SCRIPT.parent))


class CalculationContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('borrower_analysis', SCRIPT)
        cls.engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.engine)

    def case(self, name='operating'):
        return json.loads((ROOT / ('examples/'+name+'.json')).read_text())

    def metric(self, result, name):
        return next(m for m in result['metrics'] if m['id'] == name)

    def test_interest_is_covered_by_documented_earnings(self):
        spec = importlib.util.spec_from_file_location('borrower_analysis', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = json.loads((ROOT / 'examples/operating.json').read_text())
        result = module.analyze(data)
        metric = result['metrics'][0]
        self.assertEqual(metric['id'], 'interest_coverage')
        self.assertEqual(metric['value'], 3.75)
        self.assertEqual(metric['status'], 'green')
        self.assertIn('S1', metric['sources'])

    def test_expected_independent_cash_and_new_loan_examples(self):
        result = self.engine.analyze(self.case())
        self.assertAlmostEqual(self.metric(result, 'cash_coverage')['value'], 1.4444444444)
        self.assertAlmostEqual(self.metric(result, 'liquidity')['value'], 2.5)
        self.assertAlmostEqual(self.metric(result, 'new_loan_coverage')['value'], 2.8846153846)
        self.assertAlmostEqual(result['stress']['interest_coverage'], 2.3076923077)
        self.assertEqual(result['new_interest_12m'], 2400000)

    def test_missing_data_cannot_create_green_gauges(self):
        result = self.engine.analyze(self.case('incomplete'))
        self.assertEqual(self.metric(result, 'liquidity')['status'], 'context')
        self.assertEqual(self.metric(result, 'cash_coverage')['status'], 'unknown')
        self.assertEqual(self.metric(result, 'new_loan_coverage')['status'], 'unknown')
        self.assertEqual(result['data_status'], 'limited')
        self.assertNotEqual(result['risk'], 'not_seen_in_metrics')

    def test_financial_intermediary_reconciles_portfolio_outflow(self):
        result = self.engine.analyze(self.case('intermediary'))
        self.assertEqual(self.metric(result, 'interest_coverage')['value'], 1.5)
        self.assertAlmostEqual(self.metric(result, 'cash_coverage')['value'], 1.1111111111)
        self.assertEqual(self.metric(result, 'leverage')['status'], 'context')
        self.assertEqual(result['stress']['status'], 'red')

    def test_missing_portfolio_adjustment_is_not_assumed_zero(self):
        data = self.case('intermediary')
        del data['facts']['portfolio_net_cashflow']
        result = self.engine.analyze(data)
        self.assertEqual(self.metric(result, 'cash_coverage')['status'], 'unknown')

    def test_subsidy_is_excluded_from_intermediary_earnings(self):
        data = self.case('intermediary')
        data['facts']['one_off_net_income']['value'] = 5000000
        result = self.engine.analyze(data)
        self.assertEqual(self.metric(result, 'interest_coverage')['value'], 0.875)
        self.assertEqual(result['risk'], 'high')

    def test_conflicting_fact_is_excluded(self):
        data = self.case()
        data['facts']['operating_profit']['status'] = 'conflicting'
        result = self.engine.analyze(data)
        self.assertIsNone(self.metric(result, 'interest_coverage')['value'])
        self.assertEqual(result['data_status'], 'limited')

    def test_unverified_fact_is_excluded(self):
        data = self.case()
        data['facts']['interest_expense']['status'] = 'unverified'
        self.assertIsNone(self.metric(self.engine.analyze(data), 'interest_coverage')['value'])

    def test_negative_equity_is_a_risk_not_a_favorable_negative_ratio(self):
        data = self.case()
        data['facts']['equity']['value'] = -100
        result = self.engine.analyze(data)
        self.assertIsNone(self.metric(result, 'leverage')['value'])
        self.assertEqual(result['risk'], 'high')

    def test_zero_interest_has_no_infinite_green_ratio(self):
        data = self.case()
        data['facts']['interest_expense']['value'] = 0
        metric = self.metric(self.engine.analyze(data), 'interest_coverage')
        self.assertIsNone(metric['value'])
        self.assertEqual(metric['status'], 'unknown')

    def test_double_addback_of_interest_is_rejected(self):
        data = self.case()
        data['facts']['interest_paid_in_cfo']['value'] = 9000000
        with self.assertRaises(self.engine.InputError):
            self.engine.analyze(data)

    def test_nonoperating_interest_is_not_added_back_twice(self):
        data = self.case()
        data['facts']['interest_paid_in_cfo']['value'] = 0
        self.assertEqual(self.metric(self.engine.analyze(data), 'cash_coverage')['value'], 1)

    def test_bad_calendar_date_and_future_period_are_rejected(self):
        for key, value in [('assessment_date', '2026-02-30'), ('balance_date', '2026-04-01')]:
            data = self.case()
            data[key] = value
            with self.assertRaises(self.engine.InputError):
                self.engine.analyze(data)

    def test_mixed_periods_are_rejected(self):
        data = self.case()
        data['facts']['interest_expense']['period_start'] = '2025-07-01'
        with self.assertRaises(self.engine.InputError):
            self.engine.analyze(data)

    def test_interim_period_is_not_annualized(self):
        data = self.case()
        data['period']['start'] = '2025-07-01'
        for key in self.engine.FLOW:
            if key in data['facts']:
                data['facts'][key]['period_start'] = '2025-07-01'
        result = self.engine.analyze(data)
        self.assertIsNone(self.metric(result, 'new_loan_coverage')['value'])
        self.assertIsNone(result['stress']['interest_coverage'])

    def test_bad_values_and_unknown_sources_are_rejected(self):
        for value in [True, '30000000', float('nan'), float('inf')]:
            data = self.case()
            data['facts']['operating_profit']['value'] = value
            with self.assertRaises(self.engine.InputError):
                self.engine.analyze(data)
        data = self.case()
        data['facts']['operating_profit']['sources'] = ['NONEXISTENT']
        with self.assertRaises(self.engine.InputError):
            self.engine.analyze(data)

    def test_confirmed_risk_flag_overrides_healthy_metrics(self):
        data = self.case()
        data['context']['flags'] = [{'text': 'Documented material overdue payment', 'severity': 'high', 'sources': ['S1']}]
        self.assertEqual(self.engine.analyze(data)['risk'], 'high')

    def test_unknown_loan_convention_cannot_produce_estimated_interest(self):
        data = self.case()
        del data['loan_interest_basis']
        result = self.engine.analyze(data)
        self.assertIsNone(result['new_interest_12m'])

    def test_explicit_amortizing_schedule_overrides_simple_interest(self):
        data = self.case()
        data.update(loan_repayment='schedule', loan_interest_basis='schedule')
        data['facts']['new_loan_interest_12m'] = {'value': 1200000, 'status': 'confirmed', 'sources': ['S2']}
        result = self.engine.analyze(data)
        self.assertEqual(result['new_interest_12m'], 1200000)
        self.assertAlmostEqual(self.metric(result, 'new_loan_coverage')['value'], 3.2608695652)

    def test_old_cash_is_rejected_even_when_balance_is_valid(self):
        data = self.case()
        data['facts']['unrestricted_cash']['as_of'] = data['balance_date']
        with self.assertRaises(self.engine.InputError):
            self.engine.analyze(data)

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.json'
            path.write_text('{"schema_version":"1.0","schema_version":"1.0"}')
            with self.assertRaises(self.engine.InputError):
                self.engine.load_input(path)

    def test_negative_risk_case_is_high(self):
        result = self.engine.analyze(self.case('risk'))
        self.assertEqual(result['risk'], 'high')
        self.assertEqual(self.metric(result, 'cash_coverage')['status'], 'red')

    def test_stress_warning_cannot_hide_behind_green_baseline(self):
        data = self.case()
        data['facts']['new_loan_amount']['value'] = 1000000
        data['stress']['earnings_drop_pct'] = 80
        result = self.engine.analyze(data)
        self.assertEqual(result['stress']['status'], 'red')
        self.assertNotEqual(result['risk'], 'not_seen_in_metrics')


if __name__ == '__main__':
    unittest.main()
