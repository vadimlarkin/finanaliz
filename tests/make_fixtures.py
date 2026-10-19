"""Rebuild exclusively fictional demonstration cases."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERIOD = {'start': '2025-01-01', 'end': '2025-12-31'}
FLOW = {'operating_profit': 30000000, 'interest_expense': 8000000,
        'operating_cashflow': 18000000, 'cash_interest_paid': 8000000,
        'interest_paid_in_cfo': 8000000, 'principal_paid': 10000000}
STOCK = {'interest_bearing_debt': 60000000, 'equity': 60000000,
         'current_assets': 90000000, 'current_liabilities': 45000000,
         'unrestricted_cash': 15000000}
FORECAST = {'receipts_90d': 40000000, 'operating_payments_90d': 30000000,
            'capex_90d': 5000000, 'debt_service_90d': 8000000}
LOAN = {'new_loan_amount': 10000000, 'new_loan_rate_pct': 24,
        'new_loan_term_months': 12, 'new_loan_fees_12m': 0}


def main():
    folder = ROOT / 'examples'
    folder.mkdir(exist_ok=True)
    facts = {}
    for group, values, sid in [('flow', FLOW, 'S1'), ('stock', STOCK, 'S1'),
                               ('forecast', FORECAST, 'S3'), ('loan', LOAN, 'S2')]:
        for key, value in values.items():
            f = {'value': value, 'status': 'confirmed', 'sources': [sid]}
            if group == 'flow':
                f.update(period_start=PERIOD['start'], period_end=PERIOD['end'])
            if group == 'stock':
                f['as_of'] = '2026-01-15' if key == 'unrestricted_cash' else PERIOD['end']
                if key == 'unrestricted_cash':
                    f['sources'] = ['S3']
            if group == 'forecast':
                f['forecast_as_of'] = '2026-01-15'
            facts[key] = f
    base = {'schema_version': '1.0',
            'company': {'name': 'ООО «Пример»', 'business_model': 'operating', 'fictional': True},
            'assessment_date': '2026-01-15', 'balance_date': PERIOD['end'], 'period': PERIOD,
            'currency': 'RUB', 'facts': facts,
            'sources': [{'id': 'S1', 'file': 'synthetic-statements.md', 'locator': 'Колонка обычного бизнеса, 2025 год'},
                        {'id': 'S2', 'file': 'synthetic-loan.md', 'locator': 'Условия учебного займа'},
                        {'id': 'S3', 'file': 'synthetic-calendar.md', 'locator': 'План и свободные деньги на 15.01.2026'}],
            'loan_repayment': 'bullet', 'loan_interest_basis': 'simple_monthly', 'stress': {'earnings_drop_pct': 20},
            'context': {'observations': ['Покрытие после нового займа — условный сценарий без роста прибыли.',
                                          'Источник возврата 10 млн ₽ тела займа требует подтверждения.'],
                        'questions': ['Чем подтверждён источник возврата основной суммы займа?',
                                      'Как изменятся поступления при задержке оплаты покупателями?'],
                        'limitations': []}}
    weak = copy.deepcopy(base)
    weak['company']['name'] = 'ООО «Нехватка»'
    for key, value in {'operating_profit': 5000000, 'equity': 10000000,
                       'operating_cashflow': -12000000, 'receipts_90d': 12000000}.items():
        weak['facts'][key]['value'] = value
    weak['sources'][0]['locator'] = 'Колонка «Риск», 2025 год'
    weak['context']['observations'] = ['Прибыль от продаж не покрывает начисленные проценты.',
                                      'В плане не хватает денег на операционные и долговые выплаты.']
    missing = copy.deepcopy(base)
    missing['company']['name'] = 'ООО «Неполные данные»'
    for key in ['operating_cashflow', 'cash_interest_paid', 'interest_paid_in_cfo',
                'principal_paid', 'receipts_90d', 'operating_payments_90d', 'capex_90d',
                'debt_service_90d', 'new_loan_rate_pct', 'new_loan_fees_12m']:
        missing['facts'].pop(key)
    missing['sources'][0]['locator'] = 'Колонка «Неполные данные», 2025 год'
    missing['context']['observations'] = ['Баланс не подтверждает источник выплаты процентов и тела займа.']
    missing['context']['questions'] = ['Предоставьте ОДДС или полные банковские выписки.',
                                      'Нужны графики долгов и платёжный план на 90 дней.',
                                      'Уточните ставку и комиссии предлагаемого займа.']
    financial = copy.deepcopy(base)
    financial['company'].update(name='ООО «Учебный портфель»', business_model='financial_intermediary')
    financial['facts'].pop('operating_profit')
    for key, value in {'profit_before_tax': 4000000, 'one_off_net_income': 0,
                       'portfolio_net_cashflow': -6000000}.items():
        financial['facts'][key] = {'value': value, 'status': 'confirmed', 'sources': ['S4'],
                                   'period_start': PERIOD['start'], 'period_end': PERIOD['end']}
    financial['sources'].append({'id': 'S4', 'file': 'synthetic-intermediary.md',
                                 'locator': 'Результат и поправка портфеля'})
    financial['context']['observations'] = ['Доходность портфеля не равна денежным сборам; проверьте качество требований.',
                                           'Структура фондирования требует отраслевого сопоставления.']
    for name, data in [('operating', base), ('risk', weak), ('incomplete', missing), ('intermediary', financial)]:
        (folder / (name+'.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    table = '# Вымышленные исходные данные\n\nВсе компании и суммы учебные. Единица: рубль. Год: 01.01–31.12.2025. Баланс: 31.12.2025.\n\n'
    table += '| Показатель | Обычный бизнес | Риск | Неполные данные |\n|---|---:|---:|---:|\n'
    for key, value in {**FLOW, **{k: v for k, v in STOCK.items() if k != 'unrestricted_cash'}}.items():
        table += '| '+key+' | '+str(value)+' | '+str(weak['facts'].get(key, {}).get('value', 'нет'))+' | '+str(missing['facts'].get(key, {}).get('value', 'нет'))+' |\n'
    table += '\nКолонки — самостоятельные компании. Отсутствующая строка не означает ноль.\n'
    (folder / 'synthetic-statements.md').write_text(table, encoding='utf-8')
    (folder / 'synthetic-loan.md').write_text('# Учебный заём\n\nНа 15.01.2026 предложено 10 000 000 ₽ под номинальные 24% годовых на 12 месяцев. Проценты ежемесячно, тело в конце срока. Комиссии первых 12 месяцев: 0 ₽. В примере «Неполные данные» ставка и комиссии не представлены. Линейный расчёт процентов — условность примера; для реального договора нужен точный график и day-count.\n', encoding='utf-8')
    (folder / 'synthetic-calendar.md').write_text('# Учебный план на 90 дней\n\nНа 15.01.2026 свободные деньги: 15 000 000 ₽. Поступления без новых займов: 40 000 000 ₽; операционные выплаты: 30 000 000 ₽; капзатраты: 5 000 000 ₽; выплаты по существующему долгу: 8 000 000 ₽. Для «Нехватка» поступления — 12 000 000 ₽. Для «Неполные данные» план отсутствует. Целевой новый заём не включён в свободные деньги. Возврат нового займа наступает за пределами 90 дней.\n', encoding='utf-8')
    (folder / 'synthetic-intermediary.md').write_text('# Учебный финансовый посредник\n\nЗа 2025 год прибыль до налога после признанных потерь по портфелю: 4 000 000 ₽. Расходы фондирования: 8 000 000 ₽. Чистый разовый доход: 0 ₽, раскрыт отдельной расшифровкой. Результат до фондирования: 12 000 000 ₽. Чистый отток на портфель вне ОДДС: 6 000 000 ₽ (portfolio_net_cashflow = -6 000 000). Прочие суммы из колонки обычного бизнеса и S2–S3.\n', encoding='utf-8')


if __name__ == '__main__':
    main()
