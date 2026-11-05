import copy
import importlib.util
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/borrower-financial-analysis/scripts'
sys.path.insert(0, str(SCRIPTS))


class IpPdfContract(unittest.TestCase):
    def engine(self):
        spec = importlib.util.spec_from_file_location('ip_pdf_renderer', SCRIPTS / 'render_ip_dashboard.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def case(self):
        return json.loads((ROOT / 'examples/ip-investor.json').read_text())

    def test_ip_uses_approved_form_and_discloses_partial_bank_data(self):
        engine = self.engine()
        data = self.case()
        before = copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'ip.pdf'
            audit = engine.render(data, p)
            pdf = PdfReader(p)
            self.assertEqual(len(pdf.pages), 1)
            self.assertAlmostEqual(float(pdf.pages[0].mediabox.width), 595.28, places=1)
            text = pdf.pages[0].extract_text()
            for label in ['Финансовый отчёт', 'Финансовое состояние', 'Прибыль от продаж',
                          'Чистая прибыль', 'Собственный капитал', 'Кредиторская задолженность',
                          'Поступления и погашения', 'Кредитная нагрузка**', 'До займа', 'После займа',
                          'Процентный долг / капитал', 'Нет данных', '12,0*', '5,0*', '0,8*',
                          'ОКБ 30.09–27.10.2026', 'Банковские данные по 2 счетам; охват неполный']:
                self.assertIn(label, text)
            self.assertNotIn('восстановлены из ОСВ', text)
            self.assertEqual(audit['layout_version'], 'investor-one-page-ip-1.0')
            self.assertIsNone(audit['report']['chart']['facts']['revenue']['value'])
            self.assertIsNone(audit['loan_comparison']['rows'][0]['before']['value'])
        self.assertEqual(data, before)

    def test_accounting_fields_and_unknown_sources_are_rejected(self):
        for change in ('revenue', 'source', 'date', 'scope', 'debt_date'):
            with self.subTest(change=change):
                data = self.case()
                if change == 'revenue':
                    data['chart']['facts']['revenue'] = data['chart']['facts']['net_credit_turnover']
                elif change == 'source':
                    data['chart']['facts']['principal_paid']['sources'] = ['UNKNOWN']
                elif change == 'date':
                    data['chart']['facts']['principal_paid']['period_start'] = '2025-01-01'
                elif change == 'scope':
                    data['bank_scope']['accounts'] = []
                else:
                    data['bank_debt']['period']['end'] = '2026-10-28'
                with self.assertRaises(self.engine().InputError):
                    self.engine().prepare(data)

    def test_missing_bank_data_stays_unknown(self):
        data = self.case()
        data['bank_scope']['accounts'] = []
        data['cash_history'] = []
        data['chart'] = {}
        data['bank_debt'] = {}
        _, audit = self.engine().prepare(data)
        self.assertTrue(all(x['value'] is None for x in audit['report']['chart']['facts'].values()))
        self.assertIsNone(audit['report']['current_debt']['facts']['bank_principal']['value'])

    def test_invalid_or_overflowing_input_preserves_previous_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'ip.pdf'
            self.engine().render(self.case(), p)
            previous = p.read_bytes()
            for change in ('invalid', 'overflow'):
                data = self.case()
                if change == 'invalid':
                    data['cash_history'][0]['fact']['as_of'] = '2024-12-31'
                else:
                    data['conclusion'] = 'Очень длинный вывод. ' * 100
                with self.assertRaises((self.engine().InputError, self.engine().LayoutError)):
                    self.engine().render(data, p)
                self.assertEqual(previous, p.read_bytes())

    def test_cli_rejects_alias_before_overwriting_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'input.json'
            p.write_text(json.dumps(self.case(), ensure_ascii=False))
            output = Path(tmp) / 'alias.pdf'
            output.symlink_to(p)
            original = p.read_bytes()
            proc = subprocess.run([sys.executable, str(SCRIPTS / 'render_ip_dashboard.py'), str(p), str(output)], capture_output=True)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(original, p.read_bytes())

    def test_confirmed_full_bank_scope_has_accurate_footnote(self):
        data = self.case()
        data['bank_scope']['incomplete'] = False
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'full.pdf'
            self.engine().render(data, p)
            text = PdfReader(p).pages[0].extract_text()
            self.assertIn('Банковские данные по 2 счетам; не бухгалтерский баланс', text)
            self.assertNotIn('охват неполный.', text)
