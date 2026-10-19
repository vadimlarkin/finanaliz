import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / 'skills/borrower-financial-analysis/scripts'
sys.path.insert(0, str(SCRIPT_DIR))
try:
    import reportlab
    from pypdf import PdfReader
    from pypdf.generic import ContentStream
    import render_dashboard
except ImportError:
    render_dashboard = None


@unittest.skipIf(render_dashboard is None, 'PDF checks require reportlab and pypdf')
class PdfContract(unittest.TestCase):
    def test_fixed_approved_geometry_is_independent_of_financial_case(self):
        # Independently specified rectangles of the selected A4 mockup.
        expected = [(0, 0, 595.28, 841.89), (36, 619.89, 523, 78),
                    (36, 619.89, 3, 78), (36, 270.89, 523, 43)]
        with tempfile.TemporaryDirectory() as folder:
            for name in ['operating', 'risk', 'incomplete', 'intermediary']:
                data = json.loads((ROOT/'examples'/f'{name}.json').read_text())
                path = Path(folder)/(name+'.pdf')
                render_dashboard.render(data, path)
                pdf = PdfReader(path)
                rectangles = [tuple(round(float(v), 2) for v in args)
                              for args, op in ContentStream(pdf.pages[0].get_contents(), pdf).operations
                              if op == b're']
                self.assertEqual(rectangles, expected, name)
                expected_anchors = {'Заключение по новому займу': (36, 723.89, 27),
                                    'ПОКАЗАТЕЛЬ': (36, 502.89, 7),
                                    'ЗНАЧЕНИЕ': (292, 502.89, 7),
                                    'ПРОЧТЕНИЕ': (412, 502.89, 7)}
                actual = {}
                def collect(text, cm, tm, font, size):
                    label = text.strip()
                    if label in expected_anchors:
                        actual[label] = (round(cm[4]+tm[4], 2), round(cm[5]+tm[5], 2), size)
                pdf.pages[0].extract_text(visitor_text=collect)
                self.assertEqual(actual, expected_anchors, name)

    def test_unavailable_fixed_fonts_fail_instead_of_selecting_system_fonts(self):
        data = json.loads((ROOT/'examples/operating.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.pdf'
            with self.assertRaises(render_dashboard.LayoutError):
                render_dashboard.render(data, path, font_dir=folder)
            self.assertFalse(path.exists())

    def test_missing_loan_values_stay_missing_in_the_fixed_template(self):
        data = json.loads((ROOT/'examples/operating.json').read_text())
        for key in ['new_loan_amount', 'new_loan_rate_pct', 'new_loan_term_months']:
            data['facts'].pop(key, None)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.pdf'
            render_dashboard.render(data, path)
            text = PdfReader(path).pages[0].extract_text()
            self.assertGreaterEqual(text.count('Нет данных'), 3)
            self.assertNotIn('24%', text)

    def test_default_report_has_the_selected_credit_committee_structure(self):
        data = json.loads((ROOT/'examples/operating.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'report.pdf'
            render_dashboard.render(data, output)
            text = PdfReader(str(output)).pages[0].extract_text()
            for label in ['Заключение по новому займу', 'ПОКАЗАТЕЛЬ', 'ЗНАЧЕНИЕ',
                          'ПРОЧТЕНИЕ', 'УСЛОВИЯ ДЛЯ ДАЛЬНЕЙШЕГО РАССМОТРЕНИЯ']:
                self.assertIn(label, text)
            self.assertNotIn('КАК ЧИТАТЬ ДАТЧИКИ', text)

    def test_each_fictional_case_is_one_page_with_cyrillic_and_source_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ['operating', 'risk', 'incomplete', 'intermediary']:
                data = json.loads((ROOT/'examples'/f'{name}.json').read_text())
                output = Path(folder)/(name+'.pdf')
                render_dashboard.render(data, output)
                pdf = PdfReader(str(output))
                self.assertEqual(len(pdf.pages), 1)
                text = pdf.pages[0].extract_text()
                self.assertIn('ФИНАНСЫ ЗАЁМЩИКА', text)
                self.assertIn('ВЫМЫШЛЕННЫЙ ПРИМЕР', text)
                self.assertIn('[S1', text)
                self.assertNotIn('\ufffd', text)
                self.assertEqual(round(float(pdf.pages[0].mediabox.width)), 595)

    def test_overflow_fails_without_creating_or_replacing_a_pdf(self):
        data = json.loads((ROOT/'examples/operating.json').read_text())
        data['context']['observations'] = ['Very long disclosure '*500]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.pdf'
            path.write_bytes(b'existing result')
            with self.assertRaises(render_dashboard.LayoutError):
                render_dashboard.render(data, path)
            self.assertEqual(path.read_bytes(), b'existing result')

    def test_identical_input_has_identical_pdf_bytes_in_same_environment(self):
        data = json.loads((ROOT/'examples/operating.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            a, b = Path(folder)/'a.pdf', Path(folder)/'b.pdf'
            render_dashboard.render(data, a)
            render_dashboard.render(data, b)
            self.assertEqual(a.read_bytes(), b.read_bytes())


if __name__ == '__main__':
    unittest.main()
