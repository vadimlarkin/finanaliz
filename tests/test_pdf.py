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
    def test_report_uses_vbeton_palette_embedded_roboto_and_vector_logo(self):
        data = json.loads((ROOT/'examples/operating.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'report.pdf'
            render_dashboard.render(data, output)
            pdf = PdfReader(output)
            page = pdf.pages[0]
            allowed = {(0.0, .4471, .9373), (.9137, .9608, 1.0),
                       (.1373, .1569, .1765), (1.0, 1.0, 1.0)}
            for args, op in ContentStream(page.get_contents(), pdf).operations:
                if op in {b'rg', b'RG'}:
                    self.assertIn(tuple(round(float(v), 4) for v in args), allowed)
            fonts = [font.get_object() for font in page['/Resources']['/Font'].values()]
            embedded = [font for font in fonts if '/FontDescriptor' in font]
            self.assertEqual(len(embedded), 2)
            self.assertTrue(all('RobotoFlex' in str(font['/BaseFont']) for font in embedded))
            self.assertTrue(all('/FontFile2' in font['/FontDescriptor'] for font in embedded))
            forms = page['/Resources'].get('/XObject', {})
            logos = [form.get_object() for name, form in forms.items() if 'VBetonLogo' in name]
            self.assertEqual(len(logos), 1)
            self.assertEqual(logos[0]['/Subtype'], '/Form')

    def test_forbidden_principal_jargon_blocks_release_and_preserves_existing_pdf(self):
        data = json.loads((ROOT/'examples/operating.json').read_text())
        data['context']['observations'] = ['Нужно проверить тело долга.']
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.pdf'
            path.write_bytes(b'previous approved PDF')
            with self.assertRaisesRegex(ValueError, 'основной долг'):
                render_dashboard.render(data, path)
            self.assertEqual(path.read_bytes(), b'previous approved PDF')

    def test_fixed_approved_geometry_is_independent_of_financial_case(self):
        # Independently specified rectangles of the selected A4 mockup.
        expected = [(0, 0, 595.28, 841.89), (36, 693.89, 523, 34),
                    (36, 693.89, 3, 34), (36, 599.89, 523, 20),
                    (36, 519.89, 523, 20), (36, 387.89, 523, 16),
                    (36, 155.89, 523, 20), (446, 67.89, 113, 88)]
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
                expected_anchors = {'Финансовый отчёт': (36, 760.89, 22),
                                    'Финансовое состояние': (36, 630.89, 12.5),
                                    'Поступления и погашения': (36, 315.89, 11.5),
                                    'Кредитная нагрузка**': (310, 315.89, 11.5),
                                    'Влияние нового займа': (36, 185.89, 12.5)}
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
            for label in ['Финансовый отчёт', 'Соотношение', 'До займа',
                          'После займа', 'Финансовое состояние', 'Поступления и погашения']:
                self.assertIn(label, text)
            self.assertNotIn('КАК ЧИТАТЬ ДАТЧИКИ', text)

    def test_each_fictional_case_is_one_page_with_cyrillic_and_source_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ['operating', 'risk', 'incomplete', 'intermediary']:
                data = json.loads((ROOT/'examples'/f'{name}.json').read_text())
                output = Path(folder)/(name+'.pdf')
                audit = render_dashboard.render(data, output)
                pdf = PdfReader(str(output))
                self.assertEqual(len(pdf.pages), 1)
                text = pdf.pages[0].extract_text()
                self.assertIn('Финансовый отчёт', text)
                self.assertIn('УСЛОВНЫЕ ДАННЫЕ', text)
                self.assertNotIn('[S1', text)
                self.assertTrue(any(s['id']=='S1' for s in audit['sources']))
                self.assertNotIn('\ufffd', text)
                self.assertEqual(round(float(pdf.pages[0].mediabox.width)), 595)

    def test_overflow_fails_without_creating_or_replacing_a_pdf(self):
        data = json.loads((ROOT/'examples/operating.json').read_text())
        data['company']['name'] = 'Очень длинное наименование заёмщика '*100
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
