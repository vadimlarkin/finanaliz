import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'skills/borrower-financial-analysis/scripts'))
import render_ip_dashboard


class AgreedInvestorForm(unittest.TestCase):
    def case(self):return json.loads((ROOT/'examples/ip-investor-v3.json').read_text())

    def test_agreed_form_has_correct_debt_cards_cashflows_and_aligned_payment_values(self):
        data=self.case();original=copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'report.pdf'
            audit=render_ip_dashboard.render(data,output)
            pdf=PdfReader(output);self.assertEqual(len(pdf.pages),1)
            self.assertAlmostEqual(float(pdf.pages[0].mediabox.width),595.28,places=1)
            text=pdf.pages[0].extract_text()
            self.assertIn('Выручка: денежные оплаты, включая авансы.',text)
            self.assertNotIn('подтверждённых возвратов заказчикам',text)
            for phrase in ['ИНН 000000000000','3,18 млн ₽','28% годовых','29,46%', '8 месяцев',
                           'Проанализировано 100 операций по 2 представленным счетам',
                           'Выручка от основного вида деятельности','Подтверждённый долг по БКИ',
                           'Просроченный долг по БКИ','6,100 млн ₽','0 млн ₽','Полученные кредиты и займы',
                           'Заработная плата','Поставщики','Налоги','Штрафы и пени',
                           'Банковские кредиторы','Небанковские кредиторы','4,800','1,000','0,400',
                           'История работы с ВБетон','Бюро кредитных историй','27.10.2026']:
                self.assertIn(phrase,text)
            for phrase in ['Деньги на','На этих счетах','Последние суммы платежей','Крупный платёж',
                           'Полученное финансирование','Клиентские поступления','Исключены 3 пары']:
                self.assertNotIn(phrase,text)
            positions={}
            def observe(txt,cm,tm,font,size):
                if txt.strip() in {'Банковские кредиторы','4,800 млн ₽'}:positions[txt.strip()]=tm[5]
            pdf.pages[0].extract_text(visitor_text=observe)
            self.assertAlmostEqual(positions['Банковские кредиторы'],positions['4,800 млн ₽'],places=1)
            self.assertEqual(audit['layout_version'],'ip-investor-one-page-3.2')
        self.assertEqual(data,original)

    def test_missing_bki_is_unknown_and_does_not_become_zero_or_block_cashflow_report(self):
        data=self.case();data['bki']={}
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'report.pdf'
            audit=render_ip_dashboard.render(data,output)
            text=PdfReader(output).pages[0].extract_text()
            self.assertIsNone(audit['bki']['total']['value'])
            self.assertIsNone(audit['bki']['overdue']['value'])
            self.assertIn('Отчёт не предоставлен',text)
            self.assertIn('12,000',text)

    def test_inconsistent_financial_groups_preserve_previous_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'report.pdf'
            render_ip_dashboard.render(self.case(),output)
            previous=output.read_bytes()
            for change in ('commission','bki','payments','platform_subset','misc','monthly','source','period'):
                with self.subTest(change=change):
                    data=self.case()
                    if change=='commission':data['loan']['commission']['value'] += 1
                    elif change=='bki':data['bki']['total']['value'] += 1
                    elif change=='payments':data['debt_payments']['bank']['value'] += 1
                    elif change=='platform_subset':data['debt_payments']['own_platform']['value']=1000001
                    elif change=='misc':data['misc_breakdown']['card']['value'] += 1
                    elif change=='monthly':data['monthly_revenue'][0]['fact']['value'] += 1
                    elif change=='source':data['project']['description']['sources']=['UNKNOWN']
                    else:data['debt_payments']['bank']['period_end']='2026-10-26'
                    with self.assertRaises(render_ip_dashboard.InputError):
                        render_ip_dashboard.render(data,output)
                    self.assertEqual(previous,output.read_bytes())

    def test_overflow_and_impossible_loan_term_preserve_previous_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'report.pdf'
            render_ip_dashboard.render(self.case(),output);previous=output.read_bytes()
            for change in ('overflow','term'):
                data=self.case()
                if change=='overflow':data['project']['security']['text']='Длинное описание. '*100
                else:data['loan']['term_months']['value']=8.5
                with self.assertRaises((render_ip_dashboard.InputError,render_ip_dashboard.LayoutError)):
                    render_ip_dashboard.render(data,output)
                self.assertEqual(previous,output.read_bytes())

    def test_customer_refund_reduces_cash_revenue_without_being_counted_twice(self):
        data=self.case();data['bank_scope']['operations_count']=101
        data['customer_refunds']={'value':100000,'status':'confirmed','sources':['B'],
            'period_start':data['period']['start'],'period_end':data['period']['end']}
        data['bank']['debits']['value']=19800000
        data['bank']['closing']['value']=700000
        data['flows']['revenue']['value']=11900000
        data['monthly_revenue'][-1]['fact']['value']=4900000
        with tempfile.TemporaryDirectory() as tmp:
            audit=render_ip_dashboard.render(data,Path(tmp)/'report.pdf')
            self.assertEqual(audit['calculations']['inflows_total'],14900000)
            self.assertEqual(audit['calculations']['outflows_total'],14700000)
            self.assertEqual(audit['bank']['debits']['value'],19800000)
            self.assertEqual(audit['calculations']['cash_change'],200000)
            text=PdfReader(Path(tmp)/'report.pdf').pages[0].extract_text()
            self.assertIn('подтверждённых возвратов заказчикам (100 000,00 ₽)',text)
