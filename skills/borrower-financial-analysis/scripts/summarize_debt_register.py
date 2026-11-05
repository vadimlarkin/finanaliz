#!/usr/bin/env python3
"""Summarize a locally verified debt register with dated evidence."""
from __future__ import annotations

import argparse
import copy
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from itertools import combinations
import os
from pathlib import Path
import tempfile

from check_terminology import check_payload

VERSION = 'local-debt-register-1.0'
AMOUNTS = ('principal','interest','total','overdue','limit')


def money(value):
    if value is None:
        return None
    result = Decimal(str(value))
    if isinstance(value,bool) or not result.is_finite() or result < 0 or result != result.quantize(Decimal('.01')):
        raise ValueError('Сумма должна быть неотрицательной с точностью до копейки')
    return format(result, '.2f')


def pdf_number(value):
    if value is None:
        return None
    converted=float(value)
    return converted if Decimal(str(converted))==Decimal(str(value)) else None


def stamp(value, name):
    if not isinstance(value,str) or len(value)!=10 or date.fromisoformat(value).isoformat()!=value:
        raise ValueError(name+': дата требуется в формате ГГГГ-ММ-ДД')
    return value


def identity(value, name):
    if not isinstance(value,str) or not value.strip():
        raise ValueError(name+': требуется непустая строка')


def object_fields(value, allowed, name):
    if not isinstance(value,dict) or set(value)-set(allowed):
        raise ValueError(name+': неверные поля или формат объекта')


def validate(data):
    object_fields(data,{'schema_version','kind','borrower_id','report_date','sources','records','decisions'},'input')
    if data.get('schema_version')!=VERSION:
        raise ValueError('schema_version: неподдерживаемая схема')
    if data.get('kind','verified_register') not in {'bki','verified_register'}:
        raise ValueError('kind: нужен bki или verified_register')
    identity(data.get('borrower_id'),'borrower_id')
    report=stamp(data['report_date'],'report_date')
    refs=set()
    if not isinstance(data['sources'],list):
        raise ValueError('sources: требуется массив')
    for source in data['sources']:
        object_fields(source,{'id','file','locator','kind'},'source')
        for key in ('id','file','locator'):
            identity(source.get(key),'sources.'+key)
        if source['id'] in refs:
            raise ValueError('sources: повтор идентификатора')
        refs.add(source['id'])
    def sources(values,name):
        if not isinstance(values,list) or not values or any(not isinstance(v,str) or v not in refs for v in values):
            raise ValueError(name+': нужны ссылки на объявленные источники')
    if not isinstance(data['records'],list):
        raise ValueError('records: требуется массив')
    ids=set();contracts=set()
    for row in data['records']:
        object_fields(row,{'id','contract_id','subject_id','role','creditor_id','status','as_of','sources','amounts','monthly_payment','payments','maturity'},'record')
        for key in ('id','contract_id','subject_id','creditor_id'):
            identity(row.get(key),'record.'+key)
        if row['id'] in ids:
            raise ValueError('record.id: повтор идентификатора')
        ids.add(row['id'])
        if row['role'] not in {'borrower','guarantor','other'} or row['status'] not in {'active','closed','unknown'}:
            raise ValueError('record: неизвестная роль или статус')
        if row['subject_id']==data['borrower_id'] and row['role']=='borrower':
            contracts.add(row['contract_id'])
        if row['as_of'] is not None and stamp(row['as_of'],'record.as_of')>report:
            raise ValueError('record.as_of: дата позже отчёта')
        sources(row['sources'],'record.sources')
        if not isinstance(row['amounts'],dict) or set(row['amounts'])-set(AMOUNTS):
            raise ValueError('record.amounts: неизвестные компоненты долга')
        for value in row['amounts'].values():
            money(value)
        monthly=row.get('monthly_payment')
        if monthly is not None:
            object_fields(monthly,{'amount','as_of','frequency','sources'},'monthly_payment')
            money(monthly['amount'])
            if monthly.get('as_of') is not None and stamp(monthly['as_of'],'monthly_payment.as_of')>report:
                raise ValueError('monthly_payment: дата позже отчёта')
            if monthly['frequency'] not in {'monthly','quarterly','annual','unknown'}:
                raise ValueError('monthly_payment.frequency: неподдерживаемая периодичность')
            sources(monthly['sources'],'monthly_payment.sources')
        maturity=row.get('maturity')
        if maturity is not None:
            object_fields(maturity,{'date','confirmed'},'maturity')
            stamp(maturity['date'],'maturity.date')
            if not isinstance(maturity['confirmed'],bool):
                raise ValueError('maturity.confirmed: требуется логический признак')
        payment_ids=set()
        if not isinstance(row.get('payments',[]),list):
            raise ValueError('payments: требуется массив')
        for payment in row.get('payments',[]):
            object_fields(payment,{'id','date','amount','treatment','sources'},'payment')
            identity(payment['id'],'payment.id')
            if payment['id'] in payment_ids:
                raise ValueError('payment.id: повтор')
            payment_ids.add(payment['id'])
            stamp(payment['date'],'payment.date');money(payment['amount'])
            if payment['treatment'] not in {'replace','additional','unknown'}:
                raise ValueError('payment.treatment: нужен replace, additional или unknown')
            sources(payment['sources'],'payment.sources')
    pairs=set()
    if not isinstance(data.get('decisions',[]),list):
        raise ValueError('decisions: требуется массив')
    for decision in data.get('decisions',[]):
        object_fields(decision,{'contracts','action','reason','sources'},'decision')
        members=decision['contracts']
        if not isinstance(members,list) or len(members)!=2 or len(set(members))!=2 or any(uid not in contracts for uid in members):
            raise ValueError('decision.contracts: нужны два разных известных прямых договора')
        pair=tuple(sorted(members))
        if pair in pairs:
            raise ValueError('decisions: повтор или противоречащие решения')
        pairs.add(pair)
        if decision['action'] not in {'merge','separate'}:
            raise ValueError('decision.action: нужен merge или separate')
        identity(decision.get('reason'),'decision.reason');sources(decision['sources'],'decision.sources')


def payment_data(contracts, blocked):
    eligible=[];missing=[];dated=[];patch=[];limitations=[]
    for contract in contracts:
        row=contract['accepted'];uid=contract['id']
        monthly=row.get('monthly_payment') if row else None
        events=row.get('payments',[]) if row else []
        valid=bool(uid not in blocked and monthly and monthly['frequency']=='monthly' and monthly.get('as_of') and monthly['amount'] is not None)
        unresolved=any(p['treatment']=='unknown' for p in events)
        fact=None
        if valid:
            eligible.append({'contract_id':uid,'record_id':row['id'],**monthly})
            fact={'value':pdf_number(monthly['amount']),'status':'confirmed' if pdf_number(monthly['amount']) is not None else 'missing','sources':monthly['sources'],'as_of':monthly['as_of']}
        else:
            missing.append(uid)
        rendered=[]
        usable_base=bool(fact and fact['value'] is not None)
        replacement_months={event['date'][:7] for event in events if event['treatment']=='replace'}
        for event in sorted(events,key=lambda p:(p['date'],p['id'])):
            dated.append({'contract_id':uid,'record_id':row['id'],**event,
                          'replaces_monthly_base':True if event['treatment']=='replace' else False if event['treatment']=='additional' else None,
                          'monthly_base_known':valid,'base_frequency':monthly['frequency'] if monthly else None,
                          'base_as_of':monthly.get('as_of') if monthly else None})
            reason=None
            if uid in blocked:
                reason='Договор требует решения; зависимый платёж не передан в числовую вставку.'
            elif unresolved:
                reason='Неизвестно правило отдельного платежа договора; зависимые платежи не переданы в числовую вставку.'
            elif event['treatment']=='additional' and not usable_base and event['date'][:7] not in replacement_months:
                reason='Месячная база неизвестна; дополнительный платёж не передан как полный итог месяца.'
            if reason:
                limitations.append({'contract_id':uid,'payment_id':event['id'],'reason':reason})
            else:
                rendered.append({'id':uid+':'+event['id'],'date':event['date'],'treatment':event['treatment'],
                                 'amount':{'value':pdf_number(event['amount']),'status':'confirmed' if pdf_number(event['amount']) is not None else 'missing','sources':event['sources']}})
        patch.append({'id':uid,'monthly_payment':fact if not unresolved else None,'payments':rendered})
    subtotal=money(sum((Decimal(str(p['amount'])) for p in eligible),Decimal(0))) if eligible else None
    guide={'regular_monthly':{'value':subtotal if contracts and not missing and not blocked else None,
                              'known_subtotal':subtotal,'frequency':'monthly','includes_dated_payments':False,
                              'known_contracts':[p['contract_id'] for p in eligible],'missing_contracts':missing,
                              'entries':eligible},
           'dated_payments':dated,'patch_limitations':limitations,
           'exact_future_schedule':False}
    return guide,{'contracts':patch}


def summarize(data):
    validate(data)
    rows = copy.deepcopy(data['records'])
    direct = [r for r in rows if r['subject_id']==data['borrower_id'] and r['role']=='borrower']
    groups = {}
    for row in direct:
        groups.setdefault(row['contract_id'],[]).append(row)
    decisions=copy.deepcopy(data.get('decisions',[]))
    parents={uid:uid for uid in groups}
    def root(uid):
        while parents[uid]!=uid:
            uid=parents[uid]
        return uid
    for decision in decisions:
        if decision['action']=='merge':
            left,right=sorted(root(uid) for uid in decision['contracts'])
            parents[right]=left
    merged={}
    for uid,entries in groups.items():
        merged.setdefault(root(uid),[]).extend(entries)
    groups=merged
    separate=set()
    for decision in decisions:
        if decision['action']=='separate':
            pair=tuple(sorted(root(uid) for uid in decision['contracts']))
            if pair[0]==pair[1]:
                raise ValueError('decisions: объединённые договоры одновременно объявлены разными')
            separate.add(pair)
    contracts=[]
    issues=[]
    for uid, entries in sorted(groups.items()):
        ignored=[r for r in entries if not r['as_of'] and all(v is None or Decimal(str(v))==0 for v in r['amounts'].values()) and not r.get('monthly_payment') and not r.get('payments')]
        dated=[r for r in entries if r['as_of']]
        selected=max(dated,key=lambda r:(r['as_of'],r['id'])) if dated else None
        problems=[]
        undated=[r['id'] for r in entries if r['as_of'] is None and r not in ignored]
        if undated:
            problems.append({'code':'undated_record','record_ids':sorted(undated)})
        if selected:
            latest=[r for r in dated if r['as_of']==selected['as_of']]
            def signature(row):
                return json.dumps({k:v for k,v in row.items() if k not in {'id','sources','contract_id'}},sort_keys=True)
            if len({signature(r) for r in latest})>1:
                problems.append({'code':'conflicting_latest','record_ids':sorted(r['id'] for r in latest)})
            if selected['status']=='unknown':
                problems.append({'code':'unknown_status','record_ids':[selected['id']]})
            values={k:Decimal(str(v)) for k,v in selected['amounts'].items() if v is not None}
            if ('total' in values and any(values[k]>values['total'] for k in ('principal','interest','overdue') if k in values)
                    or 'total' in values and 'principal' in values and 'interest' in values and values['principal']+values['interest']>values['total']
                    or selected['status']=='closed' and any(values.get(k,0)>0 for k in ('principal','interest','total','overdue'))):
                problems.append({'code':'conflicting_amounts','record_ids':[selected['id']]})
        else:
            problems.append({'code':'no_dated_record','record_ids':sorted(r['id'] for r in entries)})
        issues.extend(dict(problem,contract_id=uid) for problem in problems)
        maturity=selected.get('maturity') if selected else None
        contracts.append({'id':uid,'accepted':selected,'record_ids':sorted(r['id'] for r in entries),
                          'ignored_record_ids':sorted(r['id'] for r in ignored),
                          'issues':problems,'confirmed_maturity':maturity['date'] if maturity and maturity['confirmed'] and maturity['date']!='9999-12-31' else None})
    active=[c for c in contracts if c['accepted'] and c['accepted']['status']!='closed']
    unresolved=[]
    for left,right in combinations(active,2):
        ids=(left['id'],right['id'])
        if left['accepted']['creditor_id']==right['accepted']['creditor_id'] and ids not in separate:
            unresolved.append({'contracts':list(ids),'reason':'Совпадает кредитор; требуется решение аналитика.'})
    blocked={uid for candidate in unresolved for uid in candidate['contracts']} | {i['contract_id'] for i in issues}
    dates=[c['accepted']['as_of'] for c in active if c['accepted']['as_of']]
    totals={}
    for field in AMOUNTS:
        known=[c for c in active if c['id'] not in blocked and c['accepted']['amounts'].get(field) is not None]
        total=sum((Decimal(str(c['accepted']['amounts'][field])) for c in known),Decimal(0))
        totals[field]={'value':money(total) if active and not blocked and len(known)==len(active) else None,
                       'known_subtotal':money(total) if known else None,
                       'contract_ids':[c['id'] for c in known],
                       'record_ids':[c['accepted']['id'] for c in known],
                       'entries':[{'contract_id':c['id'],'record_id':c['accepted']['id'],
                                   'as_of':c['accepted']['as_of'],'sources':c['accepted']['sources'],
                                   'value':money(c['accepted']['amounts'][field])} for c in known]}
    record_period={'start':min(dates),'end':max(dates)} if dates else None
    guide,load_patch=payment_data(active,blocked)
    def pdf_fact(raw):
        return {'value':pdf_number(raw['value']),'status':'calculated' if pdf_number(raw['value']) is not None else 'missing',
                'sources':sorted({ref for c in active if c['id'] in raw['contract_ids'] for ref in c['accepted']['sources']}),
                'derivation':'Сумма последних принятых записей договоров: '+', '.join(raw['record_ids']),
                **({'period_start':record_period['start'],'period_end':record_period['end']} if record_period else {})}
    bki={}
    if data.get('kind')=='bki' and record_period:
        bki={'report_date':data['report_date'],'record_period':record_period,
             **{field:pdf_fact(totals[field]) for field in ('principal','interest','total','overdue')}}
        # The form checks this optional decomposition. Other costs remain in total.
        if all(totals[k]['value'] is not None for k in ('principal','interest','total')) and Decimal(totals['principal']['value'])+Decimal(totals['interest']['value'])!=Decimal(totals['total']['value']):
            bki.pop('interest')
    source_dates=[r['as_of'] for r in rows if r['as_of']]
    return {'schema_version':VERSION,'payment_guide':guide,
            'source_record_period':{'start':min(source_dates),'end':max(source_dates)} if source_dates else None,
            'pdf_patch':{'bki':bki,'credit_load':load_patch,'sources':copy.deepcopy(data['sources'])},
            'basis':'sum_of_latest_records','common_balance_date':None,
            'limits':['Свод относится к последним записям с разными датами, общий остаток на одну дату не установлен.',
                      'Платёжные ориентиры не являются точным будущим графиком.'],
            'ok':not unresolved and not issues,'issues':issues,'decisions':decisions,'unresolved_matches':unresolved,'totals':totals,'contracts':contracts,
            'record_period':record_period,
            'other_roles':[r for r in rows if r not in direct]}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args(argv)
    if args.input.resolve()==args.output.resolve() or (args.input.exists() and args.output.exists() and args.input.samefile(args.output)):
        parser.error('Результат не может заменять исходник')
    try:
        data=json.loads(args.input.read_text(encoding='utf-8-sig'))
        check_payload(data)
        result=summarize(data)
        check_payload(result)
    except (OSError,ValueError,KeyError,TypeError,InvalidOperation) as exc:
        result={'schema_version':VERSION,'ok':False,'issues':[{'code':'invalid_input','message':str(exc)}]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=args.output.parent,delete=False) as handle:
        json.dump(result,handle,ensure_ascii=False,indent=2,sort_keys=True);handle.write('\n')
        temporary=handle.name
    os.replace(temporary,args.output)
    return 0 if result['ok'] else 2


if __name__=='__main__':
    raise SystemExit(main())
