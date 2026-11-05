import copy
import calendar
import json
import sys
import tempfile
import unittest
from pathlib import Path
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/borrower-financial-analysis/scripts'))
import render_ip_dashboard


class IpCashflowForm(unittest.TestCase):
    def case(self):
        return json.loads((ROOT / 'examples/ip-cashflow-v2.json').read_text())

    def test_cashflow_form_replaces_accounting_blocks_with_verified_flows_and_debt(self):
        data = self.case()
        before = copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'report.pdf'
            audit = render_ip_dashboard.render(data, p)
            pdf = PdfReader(p)
            self.assertEqual(len(pdf.pages), 1)
            self.assertAlmostEqual(float(pdf.pages[0].mediabox.width), 595.28, places=1)
            text = pdf.pages[0].extract_text()
            for expected in ['Денежные потоки', 'Поступления клиентов', 'Клиентские поступления по месяцам',
                             'Долговая нагрузка', 'Известный основной долг', 'Вывод по имеющимся данным',
                             '15,000', '14,700', '0,300', '0,800', '6,000', '19.11.2026', '40,000']:
                self.assertIn(expected, text)
            for absent in ['Финансовое состояние', 'Собственный капитал', 'Прибыль от продаж', 'Дебиторская задолженность']:
                self.assertNotIn(absent, text)
            self.assertEqual(audit['layout_version'], 'ip-cashflow-one-page-2.0.1')
            self.assertEqual(audit['calculations']['cash_change'], 300000)
        self.assertEqual(data, before)

    def test_inconsistent_totals_months_dates_and_payment_parts_preserve_previous_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'report.pdf'
            render_ip_dashboard.render(self.case(), p)
            original = p.read_bytes()
            for change in ('bank', 'groups', 'months', 'source', 'debt_date', 'large_payment', 'overflow'):
                with self.subTest(change=change):
                    d = self.case()
                    if change == 'bank':d['bank']['closing']['value'] += 1
                    elif change == 'groups':d['inflows'][0]['fact']['value'] += 1
                    elif change == 'months':d['monthly_client_receipts'][0]['fact']['value'] += 1
                    elif change == 'source':d['outflows'][0]['fact']['sources'] = ['UNKNOWN']
                    elif change == 'debt_date':d['debt']['principal']['period_end'] = '2026-09-01'
                    elif change == 'large_payment':d['debt']['large_payment']['total']['value'] += 1
                    else:d['banner'] = 'Очень длинный вывод. ' * 100
                    with self.assertRaises((render_ip_dashboard.InputError, render_ip_dashboard.LayoutError)):
                        render_ip_dashboard.render(d, p)
                    self.assertEqual(original, p.read_bytes())

    def test_flows_cannot_be_reported_without_identifying_account_scope(self):
        d = self.case()
        d['bank_scope']['accounts'] = []
        for k in ('opening', 'closing', 'credits', 'debits', 'internal_transfers'):
            d['bank'][k] = None
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(render_ip_dashboard.InputError):
                render_ip_dashboard.render(d, Path(tmp) / 'report.pdf')

    def case21(self):
        return json.loads((ROOT / 'examples/ip-cashflow-v21.json').read_text())

    def test_monthly_average_includes_zero_months_and_excludes_partial_month(self):
        data = self.case21()
        before = copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.pdf'
            audit = render_ip_dashboard.render(data, path)
            average = audit['calculations']['monthly_client_average']
            # The fixture has 7 million in nine full months and 5 million in October.
            self.assertAlmostEqual(average['value'], 777777.7777777778)
            self.assertEqual(average['month_count'], 9)
            self.assertEqual(average['excluded_partial_months'], ['2026-10'])
            self.assertEqual(average['sources'], ['B'])
            self.assertIn('Среднее за полный месяц: 0,8 млн ₽', PdfReader(path).pages[0].extract_text())
        self.assertEqual(data, before)

    def test_chart_distinguishes_small_receipts_and_partial_months_at_larger_height(self):
        data = self.case21()
        data['monthly_client_receipts'][1]['fact']['value'] = 35000
        data['inflows'][0]['fact']['value'] = 11835000
        data['inflows'][1]['fact']['value'] = 3165000
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.pdf'
            audit = render_ip_dashboard.render(data, path)
            pdf = PdfReader(path)
            self.assertEqual(len(pdf.pages), 1)
            page = pdf.pages[0]
            chart_text = []
            rectangles = []
            dashes = []
            fill = [None]
            def observe_text(text, cm, tm, font, size):
                if 841.89 - 615 < tm[5] < 841.89 - 480:
                    chart_text.append(text)
            def observe_drawing(operator, operands, cm, tm):
                if operator == b'rg':
                    fill[0] = tuple(float(x) for x in operands)
                if operator == b're' and 841.89 - 615 < float(operands[1]) < 841.89 - 480:
                    rectangles.append(([float(x) for x in operands], fill[0]))
                if operator == b'd' and operands[0]:
                    dashes.append(operands)
            page.extract_text(visitor_text=observe_text, visitor_operand_before=observe_drawing)
            text = ''.join(chart_text)
            self.assertIn('0,035', text)
            self.assertIn('1-27', text)
            self.assertNotIn('*', text)
            self.assertNotIn('0,0\n', text)
            largest, partial_color = max(rectangles, key=lambda item: item[0][3])
            self.assertAlmostEqual(largest[3], 54)
            self.assertEqual(tuple(round(x, 3) for x in partial_color), (.914, .961, 1))
            self.assertIn((0, .447, .937), [tuple(round(x, 3) for x in color) for rect, color in rectangles if color])
            self.assertTrue(dashes, 'Средняя линия должна отличаться пунктиром')
            self.assertEqual(audit['layout_version'], 'ip-cashflow-one-page-2.2')

    def test_missing_full_month_withholds_average_without_imputing_zero(self):
        data = self.case21()
        data['monthly_client_receipts'][1]['fact'] = None
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.pdf'
            audit = render_ip_dashboard.render(data, path)
            average = audit['calculations']['monthly_client_average']
            self.assertIsNone(average['value'])
            self.assertEqual(average['missing_months'], ['2026-02'])
            self.assertIn('есть месяцы без данных', PdfReader(path).pages[0].extract_text())

    def test_unknown_partial_month_does_not_hide_average_of_verified_full_months(self):
        data = self.case21()
        data['monthly_client_receipts'][-1]['fact'] = None
        with tempfile.TemporaryDirectory() as tmp:
            audit = render_ip_dashboard.render(data, Path(tmp) / 'report.pdf')
            self.assertAlmostEqual(audit['calculations']['monthly_client_average']['value'], 777777.7777777778)

    def test_thirteen_month_window_excludes_both_partial_edges(self):
        data = self.case21()
        data['period']['start'] = '2025-10-28'
        data['bank']['opening']['as_of'] = '2025-10-28'
        facts = [data['bank'][k] for k in ['credits', 'debits', 'internal_transfers']]
        facts += [row['fact'] for key in ['inflows', 'outflows', 'expense_breakdown'] for row in data[key]]
        for fact in facts:
            fact['period_start'] = '2025-10-28'
        data['monthly_client_receipts'] = []
        months = ['2025-10', '2025-11', '2025-12'] + [f'2026-{m:02d}' for m in range(1, 11)]
        for month in months:
            year, number = map(int, month.split('-'))
            start = max(month+'-01', '2025-10-28')
            end = min(f'{month}-{calendar.monthrange(year, number)[1]}', '2026-10-27')
            value = {'2025-10': 5000000, '2026-05': 7000000}.get(month, 0)
            data['monthly_client_receipts'].append({'month': month, 'fact': {
                'value': value, 'status': 'confirmed', 'sources': ['B'],
                'period_start': start, 'period_end': end,
            }})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.pdf'
            audit = render_ip_dashboard.render(data, path)
            average = audit['calculations']['monthly_client_average']
            self.assertEqual(average['month_count'], 11)
            self.assertAlmostEqual(average['value'], 636363.6363636364)
            self.assertEqual(average['excluded_partial_months'], ['2025-10', '2026-10'])
            pdf = PdfReader(path)
            self.assertEqual(len(pdf.pages), 1)
            for phrase in ['28-31', '1-27', 'Среднее за полный месяц: 0,6 млн ₽']:
                self.assertIn(phrase, pdf.pages[0].extract_text())

    def test_zero_receipts_and_no_detail_remain_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.pdf'
            for state in ['zero', 'missing', 'empty', 'partial_only']:
                with self.subTest(state=state):
                    data = self.case21()
                    if state == 'zero':
                        for month in data['monthly_client_receipts']:
                            month['fact']['value'] = 0
                        data['inflows'][0]['fact']['value'] = 0
                        data['inflows'][1]['fact']['value'] = 15000000
                    elif state == 'missing':
                        for month in data['monthly_client_receipts']:
                            month['fact'] = None
                    elif state == 'empty':
                        data['monthly_client_receipts'] = []
                    else:
                        data['period']['start'] = '2026-10-02'
                        data['bank']['opening']['as_of'] = '2026-10-02'
                        facts = [data['bank'][k] for k in ['credits', 'debits', 'internal_transfers']]
                        facts += [row['fact'] for key in ['inflows', 'outflows', 'expense_breakdown'] for row in data[key]]
                        for fact in facts:
                            fact['period_start'] = '2026-10-02'
                        data['monthly_client_receipts'] = [data['monthly_client_receipts'][-1]]
                        data['monthly_client_receipts'][0]['fact'].update(value=12000000, period_start='2026-10-02')
                    audit = render_ip_dashboard.render(data, path)
                    pdf = PdfReader(path)
                    self.assertEqual(len(pdf.pages), 1)
                    average = audit['calculations']['monthly_client_average']
                    text = pdf.pages[0].extract_text()
                    if state == 'zero':
                        self.assertEqual(average['value'], 0)
                        self.assertIn('Среднее за полный месяц: 0 млн ₽', text)
                    else:
                        self.assertIsNone(average['value'])
                        self.assertIn('Среднее за полный месяц: н/д', text)
                    if state == 'partial_only':
                        self.assertIn('2-27', text)
                    if state == 'empty':
                        self.assertIn('Месячная динамика не установлена', text)

    def test_investor_form_explains_business_expenses_and_payment_scope(self):
        data = self.case21()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'report.pdf'
            audit = render_ip_dashboard.render(data, p)
            page = PdfReader(p).pages[0]
            text = page.extract_text()
            for phrase in ['Подрядное строительство ИЖС', 'Пополнение оборотных средств',
                           'Контрагенты 5,000', 'Налоги 1,500', 'По 4 договорам',
                           'месячные ориентиры', 'Крупный платёж отдельно', 'На этих счетах']:
                self.assertIn(phrase, text)
            sizes = {}
            def observe(txt, cm, tm, font, size):
                for value in ['12,000', '0,300']:
                    if value in txt:sizes[value] = max(sizes.get(value, 0), size)
            page.extract_text(visitor_text=observe)
            self.assertGreater(sizes['12,000'], sizes['0,300'])
            self.assertEqual(audit['layout_version'], 'ip-cashflow-one-page-2.2')

    def test_investor_detail_errors_preserve_previous_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'report.pdf'
            render_ip_dashboard.render(self.case21(), p)
            original = p.read_bytes()
            for change in ('expenses','contracts','frequency','inclusion','business_source'):
                with self.subTest(change=change):
                    data = self.case21()
                    if change=='expenses':data['expense_breakdown'][0]['fact']['value'] += 1
                    elif change=='contracts':data['debt']['payment_scope']['contracts'] = 5
                    elif change=='frequency':data['debt']['payment_scope']['frequency'] = 'not_checked'
                    elif change=='inclusion':data['debt']['payment_scope']['large_payment_included'] = True
                    else:data['business']['sources'] = ['UNKNOWN']
                    with self.assertRaises(render_ip_dashboard.InputError):
                        render_ip_dashboard.render(data, p)
                    self.assertEqual(original, p.read_bytes())

    def test_unknown_payment_frequency_is_explained_without_guessing_a_monthly_schedule(self):
        data=self.case21()
        data['debt']['payment_scope']['frequency']='unknown'
        data['debt']['payment_scope']['large_payment_included']=None
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'report.pdf'
            render_ip_dashboard.render(data,p)
            text=PdfReader(p).pages[0].extract_text()
            self.assertIn('периодичность не установлена',text)
            self.assertIn('Включение крупного платежа не установлено',text)
            self.assertNotIn('месячные ориентиры',text)

    def test_mixed_payment_frequency_discloses_monthly_contract_count(self):
        data=self.case21()
        data['debt']['payment_scope'].update(frequency='mixed',monthly_contracts=3)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'report.pdf'
            render_ip_dashboard.render(data,p)
            text=PdfReader(p).pages[0].extract_text()
            self.assertIn('3 месячных, 1 «иное»',text)
            self.assertIn('Не расчёт месячной нагрузки',text)
            self.assertIn('не точный график',text)
