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
    import render_dashboard
except ImportError:
    render_dashboard = None


@unittest.skipIf(render_dashboard is None, 'PDF checks require reportlab and pypdf')
class PdfContract(unittest.TestCase):
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
