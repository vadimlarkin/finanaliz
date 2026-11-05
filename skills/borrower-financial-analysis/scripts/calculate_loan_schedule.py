#!/usr/bin/env python3
"""Calculate an explicit local loan schedule in RUB."""
from __future__ import annotations

import argparse
import calendar
import copy
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, DecimalException, ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_DOWN
import json
import os
from pathlib import Path
import tempfile

from check_terminology import check_payload

VERSION = 'local-loan-schedule-1.0'
ZERO = Decimal('0')
CENT = Decimal('0.01')


class InputError(ValueError):
    def __init__(self, field, message):
        self.field=field
        super().__init__(message)


def field(data, path):
    current=data
    for key in path.split('.'):
        if not isinstance(current,dict) or key not in current:
            raise InputError(path,'Не задано существенное условие '+path)
        current=current[key]
    return current


def decimal_value(value,path,*,positive=False,cents=False):
    if not isinstance(value,str) or not value.strip():
        raise InputError(path,'Нужно явное десятичное число строкой')
    try:
        number=Decimal(value)
        if not number.is_finite() or number<ZERO or positive and number<=ZERO:
            raise InputError(path,'Требуется конечное '+('положительное' if positive else 'неотрицательное')+' число')
        if cents and number!=number.quantize(CENT):
            raise InputError(path,'Сумма должна быть точной до копейки')
        return number
    except DecimalException as exc:
        raise InputError(path,'Неверное десятичное число') from exc


def day_value(value,path):
    try:
        parsed=date.fromisoformat(value)
        if value!=parsed.isoformat():
            raise ValueError('ISO')
        return parsed
    except (TypeError,ValueError) as exc:
        raise InputError(path,'Нужна календарная дата YYYY-MM-DD') from exc


def reject_unknown(value,allowed,path=''):
    if not isinstance(value,dict):
        raise InputError(path or 'input','Требуется объект')
    extra=sorted(set(value)-set(allowed))
    if extra:
        label=(path+'.' if path else '')+extra[0]
        raise InputError(label,'Неподдерживаемое поле договорных условий')


def validate(data):
    reject_unknown(data,{'schema_version','currency','issue_date','amount','term_months','rate','accrual',
                         'repayments','interest_payment_dates','fees','rounding','first_12_months','sources','terms_sources'})
    for path,allowed in [('rate',{'fixed','meaning','value'}),
                         ('accrual',{'interest_type','day_count','issue_day','repayment_day'}),
                         ('rounding',{'mode','interest_stage'}),
                         ('first_12_months',{'end','include_issue_day','include_end_day','cash_include_issue_day','cash_include_end_day'})]:
        reject_unknown(field(data,path),allowed,path)
    enums={'schema_version':{VERSION},'currency':{'RUB'},
           'rate.meaning':{'borrower_nominal_annual_percent'},
           'accrual.interest_type':{'simple'},
           'accrual.day_count':{'actual_365_fixed','actual_366_fixed','actual_actual'},
           'accrual.issue_day':{'include','exclude'},'accrual.repayment_day':{'include','exclude'},
           'rounding.mode':{'half_up','half_even','down'},'rounding.interest_stage':{'payment','daily'}}
    for path,values in enums.items():
        value=field(data,path)
        if not isinstance(value,str) or value not in values:
            raise InputError(path,'Неподдерживаемое условие '+path+'; допустимо: '+', '.join(sorted(values)))
    if field(data,'rate.fixed') is not True:
        raise InputError('rate.fixed','Поддерживается только явно фиксированная ставка')
    if not isinstance(field(data,'fees'),list):
        raise InputError('fees','Нужен явный массив комиссий, включая пустой при их отсутствии')
    amount=decimal_value(field(data,'amount'),'amount',positive=True,cents=True)
    rate=decimal_value(field(data,'rate.value'),'rate.value')
    decimal_value(field(data,'term_months'),'term_months',positive=True)
    issue=day_value(field(data,'issue_date'),'issue_date')
    repayments=field(data,'repayments')
    if not isinstance(repayments,list) or not repayments:
        raise InputError('repayments','Нужен непустой график погашения основного долга')
    repayment_dates=set()
    total=ZERO
    for index,payment in enumerate(repayments):
        label='repayments.'+str(index)
        if not isinstance(payment,dict):
            raise InputError(label,'Погашение должно быть объектом')
        reject_unknown(payment,{'date','amount'},label)
        day=day_value(payment.get('date'),label+'.date')
        if day<issue:
            raise InputError(label+'.date','Погашение раньше выдачи')
        if day in repayment_dates:
            raise InputError('repayments','Повтор даты погашения; объедините явно по источнику')
        repayment_dates.add(day)
        total+=decimal_value(payment.get('amount'),label+'.amount',positive=True,cents=True)
    if total!=amount:
        raise InputError('repayments','Сумма погашений должна точно равняться основному долгу')
    interest_dates=field(data,'interest_payment_dates')
    if not isinstance(interest_dates,list):
        raise InputError('interest_payment_dates','Нужен явный массив дат уплаты процентов')
    interest_days=[day_value(value,'interest_payment_dates') for value in interest_dates]
    if len(set(interest_days))!=len(interest_days) or any(day<issue for day in interest_days):
        raise InputError('interest_payment_dates','Повтор даты либо дата раньше выдачи')
    if rate>ZERO and (not interest_days or max(interest_days)<max(repayment_dates)):
        raise InputError('interest_payment_dates','Последняя дата уплаты процентов должна покрывать погашение основного долга')
    window=field(data,'first_12_months')
    end=day_value(field(data,'first_12_months.end'),'first_12_months.end')
    anniversaries={date(issue.year+1,2,28),date(issue.year+1,3,1)} if issue.month==2 and issue.day==29 else {issue.replace(year=issue.year+1)}
    if end not in anniversaries:
        raise InputError('first_12_months.end','Нужна явная первая годовщина выдачи, для 29 февраля выберите 28 февраля либо 1 марта')
    for prefix in ('','cash_'):
        a,b=(field(data,'first_12_months.'+prefix+key) for key in ('include_issue_day','include_end_day'))
        if type(a) is not bool or type(b) is not bool or a==b:
            raise InputError('first_12_months','Ровно одна граничная дата входит в каждое окно первых 12 месяцев')
    fee_ids=set()
    for index,fee in enumerate(data['fees']):
        label='fees.'+str(index)
        if not isinstance(fee,dict) or not isinstance(fee.get('id'),str) or not fee['id'].strip() or fee['id'] in fee_ids:
            raise InputError(label+'.id','Нужен уникальный идентификатор комиссии')
        fee_ids.add(fee['id'])
        if day_value(fee.get('date'),label+'.date')<issue:
            raise InputError(label+'.date','Комиссия раньше выдачи не поддерживается')
        allowed={'id','date','calculation','amount'} if fee.get('calculation')=='fixed' else {'id','date','calculation','rate_pct','base'}
        if set(fee)-allowed:
            raise InputError(label,'Комиссия содержит неподдерживаемые либо противоречивые условия')
        if fee.get('calculation')=='fixed':
            decimal_value(fee.get('amount'),label+'.amount',cents=True)
        elif fee.get('calculation')=='percent':
            decimal_value(fee.get('rate_pct'),label+'.rate_pct')
            if fee.get('base') not in {'original_principal','outstanding_before_repayment','outstanding_after_repayment'}:
                raise InputError(label+'.base','Нужна явная поддерживаемая база комиссии')
        else:
            raise InputError(label+'.calculation','Поддерживаются fixed и percent комиссии')
    sources=field(data,'sources')
    if not isinstance(sources,list) or not sources:
        raise InputError('sources','Нужны источники условий')
    source_ids=set()
    for index,source in enumerate(sources):
        if not isinstance(source,dict) or any(not isinstance(source.get(key),str) or not source[key].strip() for key in ('id','file','locator')) or source['id'] in source_ids:
            raise InputError('sources.'+str(index),'Нужны уникальные id и происхождение file/locator')
        source_ids.add(source['id'])
    references=field(data,'terms_sources')
    if not isinstance(references,list) or not references or any(not isinstance(ref,str) or ref not in source_ids for ref in references) or len(set(references))!=len(references):
        raise InputError('terms_sources','Нужны уникальные ссылки на источники договорных условий')


def money(value):
    return format(value.quantize(CENT,rounding=ROUND_HALF_UP),'.2f')


def calculate(data):
    validate(data)
    rounding={'half_up':ROUND_HALF_UP,'half_even':ROUND_HALF_EVEN,'down':ROUND_DOWN}[data['rounding']['mode']]
    amount=Decimal(data['amount'])
    rate=Decimal(data['rate']['value'])/100
    issue=date.fromisoformat(data['issue_date'])
    repayments={date.fromisoformat(p['date']):Decimal(p['amount']) for p in data['repayments']}
    interest_dates={date.fromisoformat(d) for d in data['interest_payment_dates']}
    fees=defaultdict(lambda:ZERO)
    fee_details=[]
    for fee in data['fees']:
        charged=date.fromisoformat(fee['date'])
        base=None
        if fee['calculation']=='fixed':
            value=Decimal(fee['amount'])
        else:
            base=amount
            if fee['base']!='original_principal':
                base-=sum((v for d,v in repayments.items() if d<charged),ZERO)
                if fee['base']=='outstanding_after_repayment':
                    base-=repayments.get(charged,ZERO)
            value=base*Decimal(fee['rate_pct'])/100
        value=value.quantize(CENT,rounding=rounding)
        fees[charged]+=value
        fee_details.append({'id':fee['id'],'date':fee['date'],'amount':money(value),
                            'base_amount':money(base) if base is not None else None,
                            'base':fee.get('base'),'calculation':fee['calculation']})
    dates=sorted(set(repayments)|interest_dates|set(fees))
    balance=amount
    accrued=ZERO
    accrued_by_date={}
    payments={}
    current=issue
    while current<=max(dates):
        if data['accrual']['repayment_day']=='exclude':
            balance-=repayments.get(current,ZERO)
        if current>issue or data['accrual']['issue_day']=='include':
            basis=data['accrual']['day_count']
            denominator=366 if basis=='actual_366_fixed' or (basis=='actual_actual' and calendar.isleap(current.year)) else 365
            daily=balance*rate/denominator
            if data['rounding']['interest_stage']=='daily':
                daily=daily.quantize(CENT,rounding=rounding)
            accrued+=daily
            accrued_by_date[current]=daily
        if current in interest_dates:
            payments[current]=accrued.quantize(CENT,rounding=rounding)
            accrued_by_date[current]=accrued_by_date.get(current,ZERO)+payments[current]-accrued
            accrued=ZERO
        if data['accrual']['repayment_day']=='include':
            balance-=repayments.get(current,ZERO)
        current+=timedelta(days=1)
    schedule=[]
    balance=amount
    totals={k:ZERO for k in ('principal','interest','fees','payment')}
    for day in dates:
        principal=repayments.get(day,ZERO); interest=payments.get(day,ZERO)
        fee=fees[day].quantize(CENT,rounding=rounding)
        balance-=principal
        row={'date':day.isoformat(),'principal':money(principal),'interest':money(interest),
             'fees':money(fee),'payment':money(principal+interest+fee),'principal_remaining':money(balance)}
        schedule.append(row)
        for key in totals:
            totals[key]+=Decimal(row[key])
    window=data['first_12_months']
    window_end=date.fromisoformat(window['end'])
    def inside(day,*,cash=False):
        prefix='cash_' if cash else ''
        return (day>issue or day==issue and window[prefix+'include_issue_day']) and (day<window_end or day==window_end and window[prefix+'include_end_day'])
    first={'start':issue.isoformat(),**window,
           'interest_accrued':money(sum((v for d,v in accrued_by_date.items() if inside(d)),ZERO).quantize(CENT,rounding=rounding))}
    for out,key in [('interest_paid','interest'),('fees','fees'),('principal_paid','principal'),('payments','payment')]:
        first[out]=money(sum((Decimal(row[key]) for row in schedule if inside(date.fromisoformat(row['date']),cash=True)),ZERO))
    references=data['terms_sources']
    def fact(value,calculated=False):
        numeric=float(value)
        if Decimal(str(numeric))!=Decimal(value):
            raise InputError('corporate_patch','Числовой формат корпоративной формы теряет точность; уменьшите масштаб суммы или уточните поддержку формата')
        result={'value':numeric,'status':'calculated' if calculated else 'confirmed','sources':list(references)}
        if calculated:
            result.update(derivation='График с явной дневной базой, границами и округлением, окно '+issue.isoformat()+' по '+window_end.isoformat(),
                          period_start=issue.isoformat(),period_end=window_end.isoformat())
        return result
    patch={'loan_repayment':'schedule','loan_interest_basis':'schedule',
           'facts':{'new_loan_amount':fact(amount),'new_loan_rate_pct':fact(data['rate']['value']),
                    'new_loan_term_months':fact(data['term_months']),
                    'new_loan_interest_12m':fact(first['interest_accrued'],True),
                    'new_loan_fees_12m':fact(first['fees'],True)}}
    return {'schema_version':VERSION,'ok':True,'schedule':schedule,'totals':{k:money(v) for k,v in totals.items()},
            'fees':fee_details,'first_12_months':first,'corporate_patch':patch,
            'sources':copy.deepcopy(data['sources']),'terms':copy.deepcopy(data),
            'limits':['Расходы рассчитаны по переданным условиям; документы и источник возврата основного долга проверяет аналитик.',
                      'Решение о финансировании принимает кредитор.']}



def unique_fields(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise InputError('json','Повторяющееся поле JSON: '+key)
        result[key]=value
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args(argv)
    if args.input.resolve()==args.output.resolve():
        parser.error('Исходник и результат должны быть разными файлами')
    try:
        data=json.loads(args.input.read_text(encoding='utf-8-sig'),object_pairs_hook=unique_fields)
        check_payload(data)
        result=calculate(data)
        check_payload(result)
    except (ValueError,KeyError,TypeError,OSError,DecimalException,OverflowError) as exc:
        result={'schema_version':VERSION,'ok':False,'errors':[{'field':getattr(exc,'field','input'),'message':str(exc)}]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',dir=args.output.parent,encoding='utf-8',delete=False) as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2,sort_keys=True);stream.write('\n')
        temporary=stream.name
    os.replace(temporary,args.output)
    return 0 if result['ok'] else 2


if __name__=='__main__':
    raise SystemExit(main())
