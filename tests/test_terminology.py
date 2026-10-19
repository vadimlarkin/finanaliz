import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT/'skills/borrower-financial-analysis/scripts'
sys.path.insert(0, str(SCRIPTS))
from check_terminology import TerminologyError, check_file, check_payload, check_text


class TerminologyContract(unittest.TestCase):
    def test_case_and_declensions_of_jargon_are_rejected(self):
        for text in ['тело долга', 'тела займа', 'к телу кредита', 'телом долга',
                     'о теле займа', 'ТЕЛО ДОЛГА', 'тела кредитов', 'к телам займов',
                     'телами займов', 'о телах кредитов', 'тела\nдолга', 'тело\u00a0долга',
                     'те\u200bло долга', 'те\u00adло долга']:
            with self.subTest(text=text), self.assertRaises(TerminologyError):
                check_text(text)

    def test_approved_term_in_all_cases_is_accepted(self):
        check_text('Основной долг; основного долга; основному долгу; основным долгом; об основном долге.')

    def test_unrelated_words_do_not_trigger_a_false_alarm(self):
        check_text('Телефон кредитора, платежи по процентам, заёмное финансирование.')

    def test_nested_payload_is_checked_including_sources(self):
        with self.assertRaises(TerminologyError):
            check_payload({'sources': [{'name': 'возврат тела займа'}]})

    def test_cli_rejects_escaped_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder)/'report.json', Path(folder)/'report.md']
            paths[0].write_text(json.dumps({'note': 'Возврат тела долга'}, ensure_ascii=True))
            paths[1].write_text('# Отчёт\n\nПогашение тела займа.')
            for path in paths:
                result = subprocess.run([sys.executable, str(SCRIPTS/'check_terminology.py'), str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('основной долг', result.stderr)

    def test_cli_accepts_canonical_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.txt'
            path.write_text('Подтвердить источник возврата основного долга.')
            result = subprocess.run([sys.executable, str(SCRIPTS/'check_terminology.py'), str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)

    def test_all_distributed_examples_and_instructions_use_canonical_terms(self):
        paths = [ROOT/'AGENTS.md', ROOT/'GLOSSARY.md', ROOT/'README.md']
        for directory in ['skills', 'examples']:
            paths.extend(p for p in (ROOT/directory).rglob('*') if p.suffix in {'.md', '.json', '.yaml', '.py'})
        for path in paths:
            with self.subTest(path=path):
                check_file(path)

    def test_pdf_with_no_extractable_text_is_not_reported_as_checked(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'blank.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            with path.open('wb') as stream:
                writer.write(stream)
            with self.assertRaises(TerminologyError):
                check_file(path)

    def test_pdf_check_examines_every_page(self):
        from reportlab.pdfgen.canvas import Canvas
        from render_dashboard import register_fonts
        register_fonts()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'two-pages.pdf'
            canvas = Canvas(str(path))
            for text in ['Возврат основного долга', 'Возврат тела займа']:
                canvas.setFont('Body', 12)
                canvas.drawString(36, 750, text)
                canvas.showPage()
            canvas.save()
            with self.assertRaises(TerminologyError):
                check_file(path)


if __name__ == '__main__':
    unittest.main()
