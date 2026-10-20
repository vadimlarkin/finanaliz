"""Observable guarantees for the approved one-page renderer; synthetic data only."""
from pathlib import Path
import tempfile
import unittest
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"skills/borrower-financial-analysis/scripts"))
from pypdf import PdfReader
from render_report import render, LAYOUT_VERSION, LayoutError

def example():
    return {
      'layout_version':LAYOUT_VERSION,
      'company':'ООО «УЧЕБНЫЙ ПРИМЕР»',
      'metadata':'Вымышленный пример / Проверка 20.10.2026 / Отчётность 2025',
      'summary':'Для решения нужны дополнительные данные.',
      'loan_note':'Условия нового займа не представлены. Покрытие после выдачи не рассчитано.',
      'headlines':[{'value':'12,000','label':'ВЫРУЧКА 2025, МЛН ₽ [S1]'},
                   {'value':'Нет данных','label':'ОСНОВНОЙ ДОЛГ [S2]'},
                   {'value':'Нет данных','label':'ПОКРЫТИЕ ПОСЛЕ ЗАЙМА'}],
      'earnings_period_label':'2025, МЛН ₽',
      'earnings_rows':[{'label':'Выручка без НДС','value':'12,000'},
                       {'label':'Результат до процентов и налога','value':'1,200'},
                       {'label':'Маржа по учёту','value':'10,0%'},
                       {'label':'Процентный расход за период','value':'Нет данных'}],
      'scenario_title':'ПРОЦЕНТЫ: СЦЕНАРИИ',
      'scenario_rows':[{'margin':'Нет данных','earnings':'Нет данных','coverage':'Нет данных'} for _ in range(3)],
      'current_note':'Текущий результат не установлен. Нужна общая ОСВ. [S2]',
      'scenario_note':'¹ Ставки и графики не представлены. Сценарий пока не рассчитан.',
      'stress_note':'Стресс не рассчитан: нет подтверждённых процентных расходов.',
      'debt_title':'РАСКРЫТЫЙ ДОЛГ / 20.10.2026',
      'debt_main':'Полнота основного долга не установлена. [S2]',
      'debt_note':'Начисленные проценты не установлены. Неизвестные суммы не равны нулю.',
      'bank_title':'ЧТО ПОКАЗАЛИ ВЫПИСКИ',
      'bank_note':'01 Выписки не представлены.\n02 Остаток денежных средств не подтверждён.\n03 Периоды нужно согласовать.',
      'questions':['Общая ОСВ по всем счетам.','Графики долга и условия новой выдачи.','Выписки всех счетов.'],
      'sources':'S1: вымышленная отчётность; S2: сведения о составе пакета.',
      'limitations':'Учебный пример. Решение о финансировании принимает кредитор.'}

class ReportLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_one_page_and_reproducible(self):
        a,b=self.root/'a.pdf',self.root/'b.pdf'
        render(example(),a);render(example(),b)
        self.assertEqual(a.read_bytes(),b.read_bytes())
        p=PdfReader(a).pages
        self.assertEqual(len(p),1)
        self.assertAlmostEqual(float(p[0].mediabox.width),595.28,places=2)
        self.assertAlmostEqual(float(p[0].mediabox.height),841.89,places=2)
        self.assertIn('Нет данных',p[0].extract_text())
        self.assertIn('основного долга',p[0].extract_text())
    def test_fixed_heading_positions_and_fonts(self):
        dest=self.root/'a.pdf';render(example(),dest)
        seen={}
        def visit(text,cm,tm,font,size):
            if text.strip() in ['Кредитная оценка','МАСШТАБ И ЗАРАБОТОК']:
                seen[text.strip()]=(cm[4]+tm[4],cm[5]+tm[5],size)
        PdfReader(dest).pages[0].extract_text(visitor_text=visit)
        for txt,x,y,size in [('Кредитная оценка',36,841.89-113,27),
                             ('МАСШТАБ И ЗАРАБОТОК',36,841.89-321.2,8.2)]:
            self.assertIn(txt,seen)
            for actual,expected in zip(seen[txt],(x,y,size)):
                self.assertAlmostEqual(actual,expected,places=2)
    def test_overflow_preserves_existing_output(self):
        dest=self.root/'a.pdf';render(example(),dest);before=dest.read_bytes()
        d=example();d['bank_note']='Длинный текст. '*1000
        with self.assertRaises(LayoutError):render(d,dest)
        self.assertEqual(dest.read_bytes(),before)
        self.assertEqual(list(self.root.glob('*.pdf')),[dest])
    def test_invalid_rows_blank_or_style_override_rejected(self):
        for key,value in [('headlines',[]),('questions',['?']),('summary',''),('font_size',5)]:
            d=example();d[key]=value
            with self.subTest(key=key),self.assertRaises((LayoutError,ValueError)):
                render(d,self.root/'a.pdf')
            self.assertFalse((self.root/'a.pdf').exists())
    def test_terminology_rejected_before_release(self):
        d=example();d['summary']='те'+'ло долга'
        with self.assertRaises(ValueError):render(d,self.root/'a.pdf')
        self.assertFalse((self.root/'a.pdf').exists())

if __name__=='__main__':unittest.main()
