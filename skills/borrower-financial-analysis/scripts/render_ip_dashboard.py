#!/usr/bin/env python3
"""Fill the approved one-page form with explicitly scoped IP bank facts."""
from __future__ import annotations
import argparse
from pathlib import Path
from analyze import InputError, day, load_input
from report_data import FLOW_ROWS, STOCK_ROWS, CHART_ROWS, _fact, _object
from render_dashboard import _render_prepared, same_file, write_audit, LayoutError
from check_terminology import check_payload

LAYOUT_VERSION = 'investor-one-page-ip-1.0'


def prepare(data):
    _object(data, {'schema_version','borrower_name','fictional','assessment_date','sources',
                   'bank_scope','cash_history','chart','bank_debt','loan','conclusion'}, 'ip-pdf')
    if data.get('schema_version') != 'ip-pdf-1.0':
        raise InputError('Требуется schema_version ip-pdf-1.0')
    check_payload(data)
    assessment = day(data.get('assessment_date'), 'assessment_date')
    if not isinstance(data.get('borrower_name'), str) or not data['borrower_name'].strip():
        raise InputError('Не указан заёмщик')
    if not isinstance(data.get('fictional', False), bool):
        raise InputError('fictional: требуется логическое значение')
    if not isinstance(data.get('conclusion'), str) or not data['conclusion'].strip():
        raise InputError('Нужен краткий вывод с ограничением охвата')
    sources = data.get('sources', [])
    if not isinstance(sources, list) or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) or not x['id'].strip()
                                           or not isinstance(x.get('file'), str) or not x['file'].strip()
                                           or not isinstance(x.get('locator'), str) or not x['locator'].strip() for x in sources):
        raise InputError('Источники требуют id, file и locator')
    ids = {s['id'] for s in sources}
    if len(ids) != len(sources):
        raise InputError('Повтор идентификатора источника')
    scope = data.get('bank_scope', {})
    _object(scope, {'accounts','incomplete'}, 'bank_scope')
    accounts = scope.get('accounts', [])
    if not isinstance(accounts, list) or any(not isinstance(x, str) or not x.strip() for x in accounts) or len(set(accounts)) != len(accounts):
        raise InputError('bank_scope.accounts: нужны уникальные счета')
    if not isinstance(scope.get('incomplete'), bool):
        raise InputError('bank_scope.incomplete: требуется логическое значение')
    if not accounts and not scope['incomplete']:
        raise InputError('Без счетов нельзя подтвердить полный банковский охват')
    cash = {}
    if not isinstance(data.get('cash_history', []), list):
        raise InputError('cash_history: требуется массив')
    for entry in data.get('cash_history', []):
        _object(entry, {'as_of','fact'}, 'cash_history')
        stamp = day(entry.get('as_of'), 'cash_history.as_of')
        if stamp > assessment or stamp.year not in range(assessment.year-2, assessment.year+1) or stamp.year in cash:
            raise InputError('cash_history: неверная или повторная дата')
        if stamp.year < assessment.year and (stamp.month,stamp.day)!=(12,31):
            raise InputError('Для прошлых лет банковский остаток требуется на 31 декабря')
        fact = _fact(entry.get('fact'), 'cash', ids, as_of=entry['as_of'])
        fact['partial'] = fact['value'] is not None
        cash[stamp.year] = (entry['as_of'], fact)
    raw_chart = data.get('chart', {})
    _object(raw_chart, {'period','facts'}, 'chart')
    period = raw_chart.get('period')
    if period is not None:
        _object(period, {'start','end'}, 'chart.period')
        start,end = day(period.get('start'),'chart.start'),day(period.get('end'),'chart.end')
        if start.year != assessment.year or end.year != assessment.year or start>end or end>assessment:
            raise InputError('Диаграмма требует общий период текущего года не позже проверки')
    _object(raw_chart.get('facts', {}), {'net_credit_turnover','principal_paid','cash_interest_paid'}, 'chart.facts')
    chart_facts = {}
    for key,_ in CHART_ROWS:
        raw = raw_chart.get('facts', {}).get(key)
        if raw is not None and period is None:
            raise InputError('Для банковских итогов нужен период')
        chart_facts[key] = _fact(raw, key, ids, period=period)
        chart_facts[key]['partial'] = chart_facts[key]['value'] is not None
    if not accounts and (any(x[1]['value'] is not None for x in cash.values()) or any(x['value'] is not None for x in chart_facts.values())):
        raise InputError('Банковские суммы требуют указанного охвата счетов')
    history = []
    for year in range(assessment.year-2,assessment.year+1):
        stamp, cash_fact = cash.get(year,(f'{year}-12-31' if year<assessment.year else data['assessment_date'],_fact(None,'cash',ids)))
        facts = {k:_fact(None,k,ids) for k,_ in FLOW_ROWS+STOCK_ROWS}
        facts['cash'] = cash_fact
        history.append({'year':year,'balance_date':stamp,'period':{'start':f'{year}-01-01','end':stamp},'basis':'bank_partial','facts':facts})
    debt = data.get('bank_debt', {})
    _object(debt, {'period','principal'}, 'bank_debt')
    debt_period = debt.get('period')
    if debt_period is not None:
        _object(debt_period, {'start','end'}, 'bank_debt.period')
        a,b=day(debt_period.get('start'),'bank_debt.start'),day(debt_period.get('end'),'bank_debt.end')
        if a>b or b>assessment:
            raise InputError('Недопустимые даты записей ОКБ')
        caption='ОКБ '+a.strftime('%d.%m' if a.year==b.year else '%d.%m.%Y')+'–'+b.strftime('%d.%m.%Y')+' / основной долг, млн ₽'
    else:
        if debt.get('principal') is not None:
            raise InputError('Для банковского долга нужен диапазон дат записей')
        caption='ОКБ: даты не заданы / основной долг, млн ₽'
    principal = _fact(debt.get('principal'),'bank_principal',ids,period=debt_period)
    loan = data.get('loan', {})
    _object(loan, {'amount','rate_pct','term_months','repayment'}, 'loan')
    repayment = loan.get('repayment','unknown')
    if repayment not in {'unknown','bullet','schedule'}:
        raise InputError('Неизвестный порядок возврата основного долга')
    loan_facts = {key:_fact(loan.get(name),key,ids) for name,key in [('amount','new_loan_amount'),('rate_pct','new_loan_rate_pct'),('term_months','new_loan_term_months')]}
    term=loan_facts['new_loan_term_months']['value']
    if term is not None and (term<=0 or int(term)!=term):
        raise InputError('Срок требуется в целых положительных месяцах')
    render_data={'facts':loan_facts,'loan_repayment':repayment}
    def empty(reason):return {'value':None,'display':'Нет данных','reason':reason}
    result={'report_mode':'ip_bank','layout_version':LAYOUT_VERSION,
            'company':{'name':data['borrower_name'],'business_model':'operating','fictional':data.get('fictional',False)},
            'assessment_date':data['assessment_date'],'sources':sources,'conclusion':data['conclusion'],
            'bank_scope':scope,'report':{'financial_history':history,
             'chart':{'period':period,'bank_data_complete':False,'facts':chart_facts},
             'current_debt':{'as_of':None,'caption':caption,'source_period':debt_period,'facts':{'bank_principal':principal,
                 'unrelated_nonbank_principal':_fact(None,'nonbank_principal',ids),'related_party_principal':_fact(None,'related_party_principal',ids)}}},
            'loan_comparison':{'rows':[
             {'id':'interest','title':'Прибыль от продаж / проценты','period':None,'as_of':None,
              'before':empty('Нет бухгалтерской прибыли'),'after':empty('Нет бухгалтерской прибыли')},
             {'id':'leverage','title':'Процентный долг / капитал','period':None,'as_of':None,
              'before':empty('Нет данных о капитале'),'after':empty('Нет данных о капитале')}]},
            'loan_facts':loan_facts,
            'limitations':['Банковские суммы относятся только к представленным счетам.' + (' Охват неполный.' if scope['incomplete'] else ''),
                           'Банковский основной долг составлен по последним записям ОКБ на разные даты.',
                           'Прибыль, капитал и коэффициенты не восстановлены из банковских оборотов.']}
    return render_data,result


def render(data,destination,font_dir=None):
    if data.get('schema_version') in {'ip-investor-3.0','ip-investor-3.1'}:
        from ip_investor_report import render as render_investor
        return render_investor(data,destination,font_dir)
    if data.get('schema_version') in {'ip-pdf-2.0','ip-pdf-2.1'}:
        from ip_cashflow_report import render as render_cashflow
        return render_cashflow(data,destination,font_dir)
    view,result=prepare(data)
    return _render_prepared(view,result,destination,font_dir)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_json');parser.add_argument('output_pdf');parser.add_argument('--audit-json')
    args=parser.parse_args()
    try:
        paths=[Path(args.input_json),Path(args.output_pdf)]+([Path(args.audit_json)] if args.audit_json else [])
        if any(same_file(a,b) for i,a in enumerate(paths) for b in paths[i+1:]):
            raise InputError('Пути входа, PDF и аудита должны различаться')
        result=render(load_input(paths[0]),paths[1])
        if args.audit_json:write_audit(args.audit_json,result)
    except (InputError,LayoutError,ValueError,OSError,KeyError,TypeError) as exc:
        print('Ошибка: '+str(exc));return 2
    print(str(paths[1]));return 0


if __name__=='__main__':raise SystemExit(main())
