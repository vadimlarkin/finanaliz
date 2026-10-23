import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
import os

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'skills/borrower-financial-analysis/scripts'))
from pypdf import PdfReader
from pypdf.generic import ContentStream
from render_dashboard import render
from analyze import InputError


class InvestorReport(unittest.TestCase):
    def case(self):
        return json.loads((ROOT/'examples/investor.json').read_text())

    def test_new_report_renders_history_half_width_chart_current_debt_and_two_ratios(self):
        data = self.case()
        original = copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'report.pdf'
            audit = render(data, output)
            pdf = PdfReader(output)
            self.assertEqual(len(pdf.pages), 1)
            text = pdf.pages[0].extract_text()
            for label in ['Финансовый отчёт', 'Прибыль от продаж', 'Чистая прибыль',
                          'Краткосрочные финансовые вложения', 'Кредиторская задолженность',
                          'ЧКО', '68,0', '30.09.2026*', 'Прибыль от продаж / проценты',
                          'Процентный долг / капитал', 'До займа', 'После займа',
                          'Кредитная нагрузка**', 'Небанковские, без связанных',
                          'Данные текущего периода восстановлены из ОСВ']:
                self.assertIn(label, text)
            for label in ['Стресс', 'Деньги на 90 дней', 'Ликвидность баланса',
                          'Источники:', 'НДС', '9137', '8246', '[S1', 'УСЛОВИЯ ДЛЯ', 'Денежное покрытие']:
                self.assertNotIn(label, text)
            self.assertIn('2025', text)
            self.assertEqual(audit['report']['chart']['facts']['net_credit_turnover']['value'], 68000000)
            self.assertEqual(len(audit['metrics']), 5)
            by_id = {m['id']:m['value'] for m in audit['metrics']}
            self.assertEqual(by_id['interest_coverage'], 2)
            self.assertEqual(by_id['leverage'], 1.08)
            self.assertEqual(by_id['cash_coverage'], 1.14)
            self.assertAlmostEqual(by_id['new_loan_coverage'], 1.7391304348)
            self.assertIn('9137', str(audit['observations']))
        self.assertEqual(data, original)

    def test_invalid_history_or_chart_evidence_cannot_replace_an_existing_report(self):
        edits = [
            lambda d: d['report']['financial_history'][2]['facts']['cash'].update(sources=['UNKNOWN']),
            lambda d: d['report']['financial_history'][2]['facts']['cash'].pop('derivation'),
            lambda d: d['report']['financial_history'][2]['facts']['cash'].update(as_of='2026-08-31'),
            lambda d: d['report']['financial_history'][0]['period'].update(start='2024-02-01'),
            lambda d: d['report']['financial_history'].append(copy.deepcopy(d['report']['financial_history'][0])),
            lambda d: d['report']['chart']['facts']['principal_paid'].update(period_end='2026-08-31'),
            lambda d: d['report']['chart']['facts']['net_credit_turnover'].update(value=float('inf')),
            lambda d: d['report']['chart'].update(bank_data_complete='false'),
            lambda d: d['report']['chart']['facts']['revenue'].update(value=61000000),
            lambda d: d['report']['financial_history'][1]['facts']['operating_profit'].update(value=999),
            lambda d: d['report']['financial_history'][2]['facts'].update(cash=0),
            lambda d: d['report']['financial_history'][2]['facts']['cash'].update(value=10**400),
        ]
        for edit in edits:
            with self.subTest(edit=edit), tempfile.TemporaryDirectory() as folder:
                data = self.case()
                edit(data)
                output = Path(folder)/'report.pdf'
                output.write_bytes(b'previous report')
                with self.assertRaises(InputError):
                    render(data, output)
                self.assertEqual(output.read_bytes(), b'previous report')

    def test_missing_conflicting_and_zero_values_are_distinct_and_incomplete_banks_hide_totals(self):
        data = self.case()
        current = data['report']['financial_history'][2]['facts']
        current['cash']['value'] = 0
        current['receivables'].update(status='conflicting')
        current.pop('inventory')
        data['report']['chart']['bank_data_complete'] = False
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.pdf'
            audit = render(data, path)
            text = PdfReader(path).pages[0].extract_text()
            self.assertIn('0,0*', text)
            self.assertIn('Противоречие', text)
            self.assertIn('Нет данных', text)
            self.assertIn('Неполные данные', text)
            self.assertNotIn('68,0', text)
            self.assertEqual(audit['report']['financial_history'][2]['facts']['receivables']['reported_value'], 24000000)
            self.assertEqual(audit['report']['chart']['facts']['net_credit_turnover']['value'], 68000000)

    def test_not_applicable_metrics_keep_explanation_and_are_not_called_missing(self):
        data = self.case()
        data['facts']['interest_expense']['value'] = 0
        data['report']['interest_ltm']['facts']['interest_expense']['value'] = 0
        data['facts']['new_loan_rate_pct']['value'] = 0
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.pdf'
            render(data, path)
            text = PdfReader(path).pages[0].extract_text()
            self.assertIn('Не применимо', text)
            self.assertIn('Проценты равны нулю', text)
            self.assertNotIn('Нет данных', text)

    def test_cli_rejects_hardlink_aliases_before_writing_pdf_or_audit(self):
        for alias in ['pdf', 'audit']:
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                source, pdf, audit = folder/'input.json', folder/'report.pdf', folder/'audit.json'
                content = json.dumps(self.case(), ensure_ascii=False).encode()
                source.write_bytes(content)
                os.link(source, pdf if alias=='pdf' else audit)
                result = subprocess.run([sys.executable, str(ROOT/'skills/borrower-financial-analysis/scripts/render_dashboard.py'),
                                         str(source), str(pdf), '--audit-json', str(audit)], capture_output=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(source.read_bytes(), content)
                if alias=='audit':
                    self.assertFalse(pdf.exists())

    def test_negative_and_all_zero_chart_values_stay_inside_plot_without_fake_positive_bars(self):
        for negative in [True, False]:
            data = self.case()
            if negative:
                data['report']['chart']['facts']['net_credit_turnover']['value'] = -5000000
            else:
                for fact in data['report']['chart']['facts'].values():
                    fact['value'] = 0
                data['report']['financial_history'][2]['facts']['revenue']['value'] = 0
            with self.subTest(negative=negative), tempfile.TemporaryDirectory() as folder:
                path = Path(folder)/'report.pdf'
                render(data, path)
                pdf = PdfReader(path)
                text = pdf.pages[0].extract_text()
                self.assertIn('-5,0' if negative else '0,0', text)
                plot_rectangles = [tuple(float(v) for v in args) for args,op in ContentStream(pdf.pages[0].get_contents(),pdf).operations
                                   if op==b're' and 219<float(args[1])<276 and float(args[3])==12]
                self.assertEqual(len(plot_rectangles),8)
                for x,y,width,height in plot_rectangles:
                    self.assertGreaterEqual(x,123.99)
                    self.assertGreaterEqual(width,0)
                    self.assertLessEqual(x+width,244.01)

    def test_small_nonzero_cash_is_not_printed_as_zero_millions(self):
        data = self.case()
        data['report']['financial_history'][2]['facts']['cash']['value'] = 10000
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.pdf'
            render(data, path)
            self.assertIn('0,01*', PdfReader(path).pages[0].extract_text())

    def test_365_days_across_calendar_years_do_not_prove_a_full_reporting_year(self):
        data = self.case()
        data['period'] = {'start':'2025-01-02','end':'2026-01-01'}
        data['balance_date'] = '2026-01-01'
        for key,fact in data['facts'].items():
            if 'period_start' in fact:
                fact.update(period_start='2025-01-02',period_end='2026-01-01')
            if 'as_of' in fact and key!='unrestricted_cash':
                fact['as_of']='2026-01-01'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.pdf'
            audit = render(data, path)
            metric = next(m for m in audit['metrics'] if m['id']=='new_loan_coverage')
            self.assertIsNone(metric['value'])
            self.assertEqual(audit['loan_comparison']['rows'][0]['after']['display'],'2,07')

    def test_year_columns_roll_with_assessment_year_without_adding_older_history(self):
        data = self.case()
        def shift(value):
            return str(int(value[:4])+1)+value[4:]
        data['assessment_date'] = shift(data['assessment_date'])
        data['balance_date'] = shift(data['balance_date'])
        for key in data['period']:
            data['period'][key] = shift(data['period'][key])
        for fact in data['facts'].values():
            for key in ['period_start','period_end','as_of','forecast_as_of']:
                if key in fact:
                    fact[key]=shift(fact[key])
        for entry in data['report']['financial_history']:
            entry['year']+=1
            entry['balance_date']=shift(entry['balance_date'])
            entry['period']={key:shift(value) for key,value in entry['period'].items()}
            for fact in entry['facts'].values():
                for key in ['period_start','period_end','as_of']:
                    if key in fact:fact[key]=shift(fact[key])
        chart=data['report']['chart']
        chart['period']={key:shift(value) for key,value in chart['period'].items()}
        for fact in chart['facts'].values():
            for key in ['period_start','period_end']:fact[key]=shift(fact[key])
        debt=data['report']['current_debt']
        debt['as_of']=shift(debt['as_of'])
        for fact in debt['facts'].values():fact['as_of']=shift(fact['as_of'])
        ltm=data['report']['interest_ltm']
        ltm['period']={key:shift(value) for key,value in ltm['period'].items()}
        for fact in ltm['facts'].values():
            for key in ['period_start','period_end']:fact[key]=shift(fact[key])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.pdf'
            audit=render(data,path)
            self.assertEqual([e['year'] for e in audit['report']['financial_history']],[2025,2026,2027])
            text=PdfReader(path).pages[0].extract_text()
            self.assertIn('9 мес. 2027*',text)
            self.assertNotIn('2024',text)

    def test_packaged_example_reproduces_the_visually_checked_reference(self):
        from analyze import load_input
        assets=ROOT/'skills/borrower-financial-analysis/assets/report'
        self.assertEqual((assets/'investor-one-page-2.2.input.json').read_bytes(),(ROOT/'examples/investor.json').read_bytes())
        self.assertEqual((assets/'synthetic-report-history.md').read_bytes(),(ROOT/'examples/synthetic-report-history.md').read_bytes())
        self.assertEqual((assets/'synthetic-current-debt-2.1.md').read_bytes(),(ROOT/'examples/synthetic-current-debt-2.1.md').read_bytes())
        self.assertEqual((assets/'synthetic-interest-ltm-2.2.md').read_bytes(),(ROOT/'examples/synthetic-interest-ltm-2.2.md').read_bytes())
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.pdf'
            render(load_input(assets/'investor-one-page-2.2.input.json'),path)
            self.assertEqual(path.read_bytes(),(assets/'investor-one-page-2.2.pdf').read_bytes())
