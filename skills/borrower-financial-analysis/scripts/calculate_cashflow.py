#!/usr/bin/env python3
"""Calculate locally prepared cashflow amounts and numeric IP report fields."""
from __future__ import annotations

import argparse
import calendar
import copy
import json
import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from check_cashflow import amount, audit, date_value, money
from check_terminology import check_payload

VERSION = 'local-cashflow-1.0'
ZERO = Decimal('0')


# Canonical categories reuse the existing audit vocabulary.
ALIASES = {
    'проценты по долгу':'interest', 'пени по долгу':'penalty',
    'комиссии и банковское обслуживание':'fee', 'погашение основного долга':'principal',
    'собственный перевод между представленными счетами':'own_transfer_matched',
}
KNOWN = {'revenue','customer_refund','received_loan','own_in','own_out','other_in','other_out',
         'principal','interest','fee','penalty','payroll','suppliers','taxes','card',
         'debt_payment_unsplit','own_transfer_matched'}


def validate(data):
    if not isinstance(data,dict) or data.get('schema_version') != VERSION:
        raise ValueError('Требуется schema_version '+VERSION)
    period = data['period']
    start,end = (date.fromisoformat(period[k]) for k in ('start','end'))
    if any(period[k] != value.isoformat() for k,value in [('start',start),('end',end)]):
        raise ValueError('Границы периода требуют даты YYYY-MM-DD')
    if start > end:
        raise ValueError('Начало периода позже окончания')
    accounts = data['accounts']
    if not isinstance(accounts,list) or not accounts or any(not isinstance(a,dict) or
            not isinstance(a.get('account'),str) or not a['account'].strip() or
            not isinstance(a.get('owner_id'),str) or not a['owner_id'].strip() for a in accounts):
        raise ValueError('Нужны счета и подтверждённые владельцы')
    account_ids = [a['account'] for a in accounts]
    if len(set(account_ids)) != len(account_ids):
        raise ValueError('Повтор счёта в охвате')
    coverage = data['coverage']
    if not isinstance(coverage,dict) or type(coverage.get('complete')) is not bool:
        raise ValueError('coverage.complete должен быть логическим')
    missing = coverage.get('missing_months',[])
    if not isinstance(missing,list) or any(not isinstance(m,str) or len(m)!=7 or
            date.fromisoformat(m+'-01').strftime('%Y-%m') != m or
            not start.strftime('%Y-%m')<=m<=end.strftime('%Y-%m') for m in missing):
        raise ValueError('Неверные missing_months')
    if coverage['complete'] and missing:
        raise ValueError('Полный охват противоречит missing_months')
    sources = data['sources']
    if not isinstance(sources,list) or not sources or any(not isinstance(s,dict) or
            any(not isinstance(s.get(k),str) or not s[k].strip() for k in ('id','file','locator')) for s in sources):
        raise ValueError('Нужны источники id/file/locator')
    source_ids = [s['id'] for s in sources]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError('Повтор ID источника')
    rows = data['operations']
    if not isinstance(rows,list):
        raise ValueError('operations должен быть массивом')
    ids = set()
    for row in rows:
        if not isinstance(row,dict) or any(not isinstance(row.get(k),str) or not row[k].strip()
                for k in ('id','source','page','account','date','debit','credit','category','purpose')):
            raise ValueError('Операция требует строковые поля CSV, источник и строку/страницу')
        if row['id'] in ids:
            raise ValueError('Повтор ID операции: '+row['id'])
        ids.add(row['id'])
        if row['account'] not in account_ids or row['source'] not in source_ids:
            raise ValueError('Счёт или источник вне объявленного охвата: '+row['id'])
        if not start<=date_value(row['date'])<=end:
            raise ValueError('Дата операции вне периода: '+row['id'])
        if row.get('category_status') not in {'confirmed','unresolved'}:
            raise ValueError('Неизвестный category_status: '+row['id'])
        debit,credit = amount(row['debit']),amount(row['credit'])
        if (debit==ZERO)==(credit==ZERO) or row.get('currency','RUB')!='RUB':
            raise ValueError('Нужна одна ненулевая сторона в RUB: '+row['id'])
    controls = data.get('bank_controls')
    if controls is not None:
        if not isinstance(controls,list):
            raise ValueError('bank_controls должен быть массивом')
        for c in controls:
            if not isinstance(c,dict) or c.get('account') not in account_ids or c.get('source') not in source_ids:
                raise ValueError('Банковский контроль требует счёт в охвате и независимый source')
            if c.get('period',period) != period:
                raise ValueError('Период банковского контроля отличается от периода расчёта')
            for k in ('opening','closing','debit','credit'):
                amount(c[k])
    return start,end


def calculate(data):
    """Return a deterministic result without changing prepared input."""
    start,end = validate(data)
    period = data['period']
    accounts = [a['account'] for a in data['accounts']]
    rows = copy.deepcopy(data['operations'])
    for row in rows:
        row['date'] = date_value(row['date']).isoformat()
        row['category'] = ALIASES.get(row['category'],row['category'])
    controls = data.get('bank_controls')
    incoming = {'revenue','received_loan','own_in','other_in'}
    outgoing = {'customer_refund','own_out','other_out','payroll','suppliers','taxes','debt_payment_unsplit'}
    verified = [r for r in rows if r.get('category_status') == 'confirmed' and r['category'] in KNOWN
                and not (r['category'] in incoming and amount(r['debit'])>ZERO)
                and not (r['category'] in outgoing and amount(r['credit'])>ZERO)]
    unresolved = [r for r in rows if r not in verified]
    audit_result = audit(rows, controls)
    if controls is not None:
        seen_controls = {c['account'] for c in controls}
        operated_accounts = {r['account'] for r in rows}
        for account in sorted(set(accounts)-seen_controls-operated_accounts):
            audit_result['issues'].append({'code':'missing_bank_control','account':account,
                                          'message':'Нет банковского контроля для представленного нулевого счёта'})
    owners = {a['account']:a['owner_id'] for a in data['accounts']}
    for row in rows:
        if row.get('pair_id') and (row not in verified or
                owners.get(row.get('counterparty_account')) != owners[row['account']]):
            audit_result['issues'].append({'code':'unconfirmed_transfer_pair',
                'operation_id':row['id'],'message':'Пара требует подтверждённые категории и одного владельца счетов'})
    audit_result['issues'].sort(key=lambda issue:json.dumps(issue,sort_keys=True,ensure_ascii=False))
    audit_result['ok'] = not audit_result['issues']
    complete = data['coverage']['complete'] and not unresolved and audit_result['ok']


    def fact(selected, side, window=period):
        return {'value': money(sum((amount(r[side]) for r in selected), ZERO)),
                'operation_ids': sorted(r['id'] for r in selected if amount(r[side]) != ZERO),
                'period': copy.deepcopy(window), 'accounts': accounts,
                'terms':[{'operation_id':r['id'],'field':side,'sign':1} for r in sorted(selected,key=lambda r:r['id']) if amount(r[side])!=ZERO]}

    totals = {k: fact(rows, k) for k in ('debit', 'credit')}
    totals['cash_change'] = {'value': money(Decimal(totals['credit']['value'])-Decimal(totals['debit']['value'])),
                             'operation_ids': sorted(r['id'] for r in rows), 'period': copy.deepcopy(period), 'accounts': accounts,
                             'terms':[{'operation_id':r['id'],'field':'credit' if amount(r['credit']) else 'debit',
                                       'sign':1 if amount(r['credit']) else -1} for r in sorted(rows,key=lambda r:r['id'])]}
    months = []
    cursor = start.replace(day=1)
    while cursor <= end:
        last = cursor.replace(day=calendar.monthrange(cursor.year,cursor.month)[1])
        window = {'start': max(start,cursor).isoformat(), 'end':min(end,last).isoformat()}
        selected = [r for r in rows if r['date'][:7] == cursor.isoformat()[:7]]
        months.append({'month':cursor.isoformat()[:7], 'period':window,
                       'partial':start>cursor or end<last,
                       'revenue':fact([r for r in selected if r['category']=='revenue' and r in verified],'credit',window)})
        cursor = date(cursor.year+1,1,1) if cursor.month==12 else date(cursor.year,cursor.month+1,1)
    categories = {category:{side:fact([r for r in rows if r['category']==category],side)
                            for side in ('debit','credit')} for category in sorted({r['category'] for r in rows})}
    for month in months:
        selected = [r for r in rows if r['date'][:7]==month['month']]
        month['totals'] = {side:fact(selected,side,month['period']) for side in ('debit','credit')}
        month['categories'] = {c:{side:fact([r for r in selected if r['category']==c],side,month['period'])
                                  for side in ('debit','credit')} for c in sorted({r['category'] for r in selected})}
    components = {}
    for key,category,side in [('principal_paid','principal','debit'),('interest_paid','interest','debit'),
                              ('interest_returned','interest','credit'),('fees_paid','fee','debit'),
                              ('fees_returned','fee','credit')]:
        components[key] = fact([r for r in rows if r['category']==category and r in verified],side)
    components['operating_outflows'] = fact([r for r in rows if r in verified and r['category'] in
        {'payroll','suppliers','taxes','fee','card','other_out'}],'debit')
    sources = data.get('sources',[])
    def pdf_number(value):
        if value is None:
            return None
        numeric = float(value)
        if Decimal(str(numeric)) != Decimal(value):
            raise ValueError('Сумма не поддерживается числовым входом PDF без потери копеек')
        return numeric
    def pdf_fact(raw, window=period):
        return {'value':pdf_number(raw['value']), 'status':'calculated' if raw['value'] is not None else 'missing',
                'sources':sorted({r['source'] for r in rows if r['id'] in raw['operation_ids']}) or sorted({r['source'] for r in rows}) or [s['id'] for s in sources],
                'derivation':raw.get('derivation') or ('Сумма проверенных операций: '+', '.join(raw['operation_ids']) if raw['operation_ids'] else 'Нет операций в представленном охвате.'),
                'period_start':window['start'],'period_end':window['end']}
    pdf_months = []
    for month in months:
        raw = copy.deepcopy(month['revenue'])
        if (not data['coverage']['complete'] and 'missing_months' not in data['coverage']
                or month['month'] in data['coverage'].get('missing_months',[])
                or any(r['date'][:7]==month['month'] for r in unresolved)):
            raw['value'] = None
        pdf_months.append({'month':month['month'],'fact':pdf_fact(raw,month['period'])})
    balance_sources = sorted({c.get('source') for c in controls or [] if c.get('source')})
    bank_issue_codes = sorted({issue['code'] for issue in audit_result['issues']
                               if issue['code'].startswith('bank_') or
                               issue['code'] in {'invalid_controls','missing_bank_control'}})
    balances = {}
    for key in ('opening','closing'):
        known = (controls is not None and not bank_issue_codes
                 and {c['account'] for c in controls} == set(accounts))
        balances[key] = {'value':money(sum((amount(c[key]) for c in controls),ZERO)) if known else None,
                         'operation_ids':[], 'control_accounts':accounts if known else [],
                         'sources':balance_sources,'period':copy.deepcopy(period),'accounts':accounts,
                         'issue_codes':bank_issue_codes}
    transfer_rows = [r for r in verified if r['category']=='own_transfer_matched']
    transfer_valid = not any('transfer' in i['code'] or 'pair' in i['code'] for i in audit_result['issues'])
    def after_transfers(selected, window=period):
        if not transfer_valid:
            return None
        return {side:fact([r for r in selected if r not in transfer_rows],side,window) for side in ('debit','credit')}
    cleaned = after_transfers(rows)
    for month in months:
        month['after_matched_transfers'] = after_transfers([r for r in rows if r['date'][:7]==month['month']],month['period'])
    transfer_fact = fact(transfer_rows,'debit')
    transfer_fact['operation_ids'] = sorted(r['id'] for r in transfer_rows)
    if not transfer_valid:
        transfer_fact['value'] = None
    bank = {key:pdf_fact(totals[side]) for key,side in [('credits','credit'),('debits','debit')]}
    for key in ('opening','closing'):
        bank[key] = {'value':pdf_number(balances[key]['value']), 'status':'confirmed' if balances[key]['value'] is not None else 'missing',
                     'sources':balance_sources,'as_of':period['start' if key=='opening' else 'end']}
    bank['internal_transfers'] = pdf_fact(transfer_fact)
    bank['transfer_pairs'] = audit_result['checks']['matched_pairs']
    def group(categories, side, window=period, selected=verified):
        return fact([r for r in selected if r['category'] in categories],side,window)
    revenue = group({'revenue'},'credit')
    refunds = group({'customer_refund'},'debit')
    revenue['value'] = money(Decimal(revenue['value'])-Decimal(refunds['value']))
    revenue['operation_ids'] = sorted(revenue['operation_ids']+refunds['operation_ids'])
    revenue['terms'] += [dict(term,sign=-1) for term in refunds['terms']]
    revenue['derivation'] = 'Поступления по категории revenue минус списания customer_refund.'
    flow_raw = {'revenue':revenue}
    for key,selected_categories,side in [
        ('received_loans',{'received_loan'},'credit'),('own_in',{'own_in'},'credit'),
        ('other_in',{'other_in','interest','fee','principal','penalty','card'},'credit'),
        ('debt_payments',{'principal','interest','debt_payment_unsplit'},'debit'),
        ('payroll',{'payroll'},'debit'),('suppliers',{'suppliers'},'debit'),('taxes',{'taxes'},'debit'),
        ('own_out',{'own_out'},'debit'),('other_out',{'fee','penalty','card','other_out'},'debit')]:
        flow_raw[key] = group(selected_categories,side)
    flows = {key:pdf_fact(raw) for key,raw in flow_raw.items()}
    if unresolved or not audit_result['ok']:
        for value in flows.values():
            value.update(value=None,status='missing')
    for month,pdf_month in zip(months,pdf_months):
        selected = [r for r in verified if r['date'][:7]==month['month']]
        a,b = group({'revenue'},'credit',month['period'],selected),group({'customer_refund'},'debit',month['period'],selected)
        a['value'] = money(Decimal(a['value'])-Decimal(b['value']))
        a['operation_ids'] = sorted(a['operation_ids']+b['operation_ids'])
        a['terms'] += [dict(term,sign=-1) for term in b['terms']]
        a['derivation'] = 'Поступления revenue минус списания customer_refund за месяц.'
        month['revenue'] = a
        if pdf_month['fact']['value'] is not None:
            pdf_month['fact'] = pdf_fact(a,month['period'])
    pdf_limits = []
    if len(months)>13:
        pdf_limits.append('more_than_13_months')
    for month,pdf_month in zip(months,pdf_months):
        if Decimal(month['revenue']['value'])<ZERO:
            if 'negative_monthly_revenue' not in pdf_limits:
                pdf_limits.append('negative_monthly_revenue')
            pdf_month['fact'].update(value=None,status='missing')
    misc = {key:pdf_fact(group(categories,'debit')) for key,categories in
            [('fees_services',{'fee'}),('card',{'card'}),('penalties',{'penalty'})]}
    if any(r['category']=='other_out' and amount(r['debit'])>ZERO for r in verified) or unresolved:
        # The current form only has these three detailed rows; unknown allocation stays unknown.
        misc = {key:{'value':None,'status':'missing','sources':[]} for key in misc}
    debt_rows = [r for r in verified if r['category'] in {'principal','interest','debt_payment_unsplit'} and amount(r['debit'])>ZERO]
    payments = {}
    for kind in ('bank','nonbank'):
        raw = fact([r for r in debt_rows if r.get('creditor_kind')==kind],'debit')
        if any(r.get('creditor_kind') not in {'bank','nonbank'} for r in debt_rows) or unresolved:
            raw['value'] = None
        payments[kind] = pdf_fact(raw)
    payments.update(own_platform={'value':None,'status':'missing','sources':[]},
                    platform_contracts={'value':None,'status':'missing','sources':[]})
    patch = {'period':copy.deepcopy(period),'bank':bank,'flows':flows,'misc_breakdown':misc,
             'customer_refunds':pdf_fact(refunds),'debt_payments':payments,
             'bank_scope':{'accounts':accounts,'incomplete':not complete,'operations_count':len(rows)},
             'monthly_revenue':pdf_months}
    return {'schema_version': VERSION, 'ok':audit_result['ok'], 'totals':totals, 'months':months,
            'categories':categories,'components':components,'complete':complete,
            'unresolved':{k:fact(unresolved,k) for k in ('debit','credit')},
            'audit':audit_result,'bank_controls':copy.deepcopy(controls), 'balances':balances,
            'free_cashflow':None, 'sources':copy.deepcopy(sources),'pdf_patch':patch,
            'operations':copy.deepcopy(data['operations']),
            'pdf_compatible':not pdf_limits and audit_result['ok'], 'pdf_limits':pdf_limits,
            'after_matched_transfers':cleaned,'transfers':transfer_fact,
            'limits':['Полный свободный денежный поток не определяется арифметикой выписок.',
                      'Итоги относятся только к указанному периоду и представленным счетам.',
                      *([] if controls is not None else ['Независимые банковские итоги не предоставлены.'])]}



def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input'); parser.add_argument('output')
    args = parser.parse_args(argv)
    source, destination = Path(args.input), Path(args.output)
    if source.resolve() == destination.resolve():
        parser.error('Вход и результат должны быть разными файлами')
    try:
        data = json.loads(source.read_text(encoding='utf-8-sig'))
        check_payload(data)
        result = calculate(data)
        check_payload(result)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {'schema_version':VERSION, 'ok':False,
                  'issues':[{'code':'invalid_input','message':str(exc)}]}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=destination.parent,delete=False) as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2,sort_keys=True);stream.write('\n')
        temporary = stream.name
    os.replace(temporary,destination)
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
