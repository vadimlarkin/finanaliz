#!/usr/bin/env python3
"""Import explicitly described local CSV statements into the verified cashflow format."""
from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
from io import StringIO
import json
import os
import re
from pathlib import Path
import tempfile

from calculate_cashflow import ALIASES, KNOWN, calculate, validate
from check_cashflow import audit
from check_terminology import check_payload

VERSION='local-statement-csv-1.0'


class ProfileError(ValueError):
    pass


def validate_profile(profile):
    try:
        required={'schema_version','encoding','delimiter','date_format','amount_format','direction',
                  'columns','currency','account','period','coverage','source_id','service_rows'}
        if not isinstance(profile,dict) or required-set(profile) or set(profile)-required-{'category_map','bank_controls','control_sources'}:
            raise ValueError('Профиль содержит неизвестные или пропущенные поля')
        if profile['schema_version']!=VERSION:
            raise ValueError('Неподдерживаемая schema_version профиля')
        if not isinstance(profile['encoding'],str):raise ValueError('encoding требует имя кодировки')
        try:
            b'x'.decode(profile['encoding'])
        except UnicodeError:
            # A multibyte text encoding can reject the probe but decode its real source.
            pass
        except LookupError as exc:
            raise ValueError('encoding требует текстовую кодировку: '+profile['encoding']) from exc
        if not isinstance(profile['delimiter'],str) or len(profile['delimiter'])!=1 or profile['delimiter'] in '\r\n\x00"':
            raise ValueError('delimiter требует один допустимый символ')
        if profile['date_format'] not in {'%Y-%m-%d','%d.%m.%Y'}:
            raise ValueError('Неподдерживаемый date_format')
        money=profile['amount_format']
        if (not isinstance(money,dict) or set(money)!={'decimal_separator','group_separator','blank_is_zero'}
                or money['decimal_separator'] not in {'.',','}
                or money['group_separator'] not in {'',' ','.',',','\u00a0'}
                or money['group_separator']==money['decimal_separator'] or type(money['blank_is_zero']) is not bool):
            raise ValueError('amount_format требует явные различимые разделители и логический blank_is_zero')
        columns=profile['columns']
        extra={'counterparty_account','document_number','operation_date','bank_operation_id','creditor_kind','pair_id','transfer_confirmation'}
        if (not isinstance(columns,dict) or not {'date','purpose'}.issubset(columns)
                or set(columns)-{'date','purpose','currency','category'}-extra
                or any(not isinstance(v,str) or not v.strip() for v in columns.values())):
            raise ValueError('columns содержит неподдерживаемые поля или пустые имена')
        direction=profile['direction']
        if not isinstance(direction,dict):raise ValueError('direction требует объект')
        if direction.get('mode')=='separate':
            if (set(direction)!={'mode','debit_column','credit_column'}
                    or any(not isinstance(direction[k],str) or not direction[k].strip() for k in ('debit_column','credit_column'))
                    or direction['debit_column']==direction['credit_column']):
                raise ValueError('direction separate требует разные debit_column и credit_column')
        elif direction.get('mode')=='indicator':
            if (set(direction)!={'mode','amount_column','indicator_column','values'}
                    or any(not isinstance(direction[k],str) or not direction[k].strip() for k in ('amount_column','indicator_column'))
                    or direction['amount_column']==direction['indicator_column']
                    or not isinstance(direction['values'],dict) or not direction['values']
                    or any(not isinstance(k,str) or not k or v not in {'debit','credit'} for k,v in direction['values'].items())):
                raise ValueError('direction indicator требует явные колонки и отображение меток в debit/credit')
        else:
            raise ValueError('Неподдерживаемый mode направления, угадывание не выполняется')
        if profile['currency']!='RUB':raise ValueError('Поддерживается только явная валюта RUB')
        if not isinstance(profile['source_id'],str) or not profile['source_id'].strip():
            raise ValueError('source_id требует непустой идентификатор')
        category_map=profile.get('category_map',{})
        if not isinstance(category_map,dict):raise ValueError('category_map требует объект')
        for key,value in category_map.items():
            if (not isinstance(key,str) or not key or not isinstance(value,dict) or set(value)!={'category','status'}
                    or not isinstance(value['category'],str) or not value['category'].strip()
                    or value['status'] not in {'confirmed','unresolved'}
                    or value['status']=='confirmed' and ALIASES.get(value['category'],value['category']) not in KNOWN):
                raise ValueError('category_map требует явные поддерживаемые категории и статусы')
        if category_map and 'category' not in columns:
            raise ValueError('category_map требует явную колонку category')
        if not isinstance(profile['service_rows'],list):raise ValueError('service_rows требует массив')
        for rule in profile['service_rows']:
            if not isinstance(rule,dict) or not isinstance(rule.get('reason'),str) or not rule['reason'].strip():
                raise ValueError('Служебная строка требует reason')
            if set(rule)=={'blank','reason'} and rule['blank'] is True:
                continue
            if (set(rule)!={'column','equals','reason'} or not isinstance(rule['column'],str) or not rule['column'].strip()
                    or not isinstance(rule['equals'],str)):
                raise ValueError('Неподдерживаемое правило служебной строки')
        dummy={'schema_version':'local-cashflow-1.0','accounts':[profile['account']],
               'period':profile['period'],'coverage':profile['coverage'],
               'sources':[{'id':profile['source_id'],'file':'profile-validated.csv','locator':'CSV'}], 'operations':[]}
        if not isinstance(profile.get('control_sources',[]),list):
            raise ValueError('control_sources требует массив')
        dummy['sources']+=copy.deepcopy(profile.get('control_sources',[]))
        if 'bank_controls' in profile and not isinstance(profile['bank_controls'],list):
            raise ValueError('bank_controls требует массив независимых контролей')
        if profile.get('bank_controls'):
            dummy['bank_controls']=copy.deepcopy(profile['bank_controls'])
        validate(dummy)
    except (ValueError,KeyError,TypeError,LookupError,InvalidOperation) as exc:
        raise ProfileError(str(exc)) from exc


def atomic_text(destination,text):
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',newline='',dir=destination.parent,delete=False) as stream:
        stream.write(text);name=stream.name
    os.replace(name,destination)


def parse_amount(raw,settings):
    if raw=='' and settings['blank_is_zero']:
        return '0.00'
    group=settings['group_separator'];decimal=settings['decimal_separator']
    integer=r'[0-9]+'
    if group:
        integer='(?:'+integer+'|[0-9]{1,3}(?:'+re.escape(group)+r'[0-9]{3})+)'
    pattern=integer+'(?:'+re.escape(decimal)+r'[0-9]{1,2})?'
    if not re.fullmatch(pattern,raw):
        raise ValueError('Сумма не соответствует явному формату или точности до копейки')
    raw=raw.replace(group,'') if group else raw
    try:
        value=Decimal(raw.replace(decimal,'.')).quantize(Decimal('.01'))
    except InvalidOperation as exc:
        raise ValueError('Неподдерживаемый диапазон денежной суммы') from exc
    return format(value,'.2f')


def parse_row(values,profile,sha,locator):
    columns=profile['columns'];direction=profile['direction'];money=profile['amount_format']
    raw_date=values[columns['date']]
    parsed=datetime.strptime(raw_date,profile['date_format'])
    if parsed.strftime(profile['date_format'])!=raw_date:
        raise ValueError('Дата не соответствует явному формату')
    day=parsed.date().isoformat()
    if not profile['period']['start']<=day<=profile['period']['end']:
        raise ValueError('Дата операции вне объявленного периода')
    if direction['mode']=='separate':
        debit=parse_amount(values[direction['debit_column']],money)
        credit=parse_amount(values[direction['credit_column']],money)
    else:
        label=values[direction['indicator_column']]
        if label not in direction['values']:
            raise ValueError('Неизвестная метка направления в '+direction['indicator_column']+': '+label)
        side=direction['values'][label]
        value=parse_amount(values[direction['amount_column']],money)
        debit=value if side=='debit' else '0.00'
        credit=value if side=='credit' else '0.00'
    if (Decimal(debit)==0)==(Decimal(credit)==0):
        raise ValueError('Операция требует ровно одну ненулевую сторону')
    currency=values[columns['currency']] if 'currency' in columns else profile['currency']
    if currency!=profile['currency'] or currency!='RUB':
        raise ValueError('Неподдерживаемая или противоречащая профилю валюта: '+currency)
    row={'id':profile['source_id']+':'+sha[:16]+':'+locator,'source':profile['source_id'],
         'page':locator,'source_row':locator,'load_id':profile['source_id']+':'+sha,'account':profile['account']['account'],
         'date':day,'purpose':values[columns['purpose']],
         'debit':debit,'credit':credit,'currency':currency}
    if not row['purpose'].strip():
        raise ValueError('Пустое назначение операции')
    if 'category' in columns:
        row['source_category']=values[columns['category']]
    category=profile.get('category_map',{}).get(values.get(columns.get('category','')))
    row.update(category=category['category'] if category else 'unresolved',
               category_status=category['status'] if category else 'unresolved')
    for key,column in columns.items():
        if key in {'date','purpose','currency','category'} or values[column]=='':continue
        value=values[column]
        if key=='operation_date':
            parsed=datetime.strptime(value,profile['date_format'])
            if parsed.strftime(profile['date_format'])!=value:
                raise ValueError('operation_date не соответствует явному формату')
            value=parsed.date().isoformat()
        row[key]=value
    return row


def import_statement(source,profile):
    validate_profile(profile)
    source=Path(source).resolve();raw=source.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    try:
        text=raw.decode(profile['encoding'])
    except UnicodeError as exc:
        return {'schema_version':VERSION,'ok':False,'source':{'path':str(source),'sha256':sha,'bytes':len(raw)},
                'issues':[{'code':'invalid_encoding','message':'CSV не декодирован выбранной кодировкой: '+str(exc)}],
                'row_results':[],'cashflow':None,'csv_audit':None,'summary':None}
    reader=csv.reader(StringIO(text,newline=''),delimiter=profile['delimiter'],strict=True)
    rows=[];issues=[];ledger=[];finished=False
    def failure(code,message,start,end):
        issue={'code':code,'line_start':start,'line_end':end,'message':message}
        issues.append(issue);ledger.append({**issue,'kind':'error'})
    try:
        header=next(reader)
    except StopIteration:
        header=[];finished=True
        failure('invalid_header','Пустой CSV: заголовок отсутствует',1,1)
    except csv.Error as exc:
        header=[];finished=True
        failure('invalid_csv','Ошибка CSV в заголовке: '+str(exc),1,max(1,reader.line_num))
    required=set(profile['columns'].values())
    direction=profile['direction']
    required.update(direction[k] for k in ('debit_column','credit_column','amount_column','indicator_column') if k in direction)
    required.update(rule['column'] for rule in profile['service_rows'] if 'column' in rule)
    header_ok=bool(header) and all(name.strip() for name in header) and len(set(header))==len(header) and not required-set(header)
    if not finished:
        if header_ok:
            ledger.append({'line_start':1,'line_end':reader.line_num,'kind':'header'})
        else:
            failure('invalid_header','Пустые/повторные имена или отсутствующие настроенные колонки: '+', '.join(sorted(required-set(header))),1,reader.line_num)
    previous=reader.line_num
    while not finished:
        start=previous+1
        try:
            fields=next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            failure('invalid_csv','Ошибка синтаксиса CSV: '+str(exc),start,max(start,reader.line_num))
            break
        line=reader.line_num;previous=line
        if not header_ok:
            failure('invalid_header','Строка не импортирована из-за непригодного заголовка',start,line)
            continue
        locator=str(start) if start==line else str(start)+'-'+str(line)
        try:
            values=dict(zip(header,fields))
            services=[rule for rule in profile['service_rows'] if
                      (rule.get('blank') is True and not fields) or
                      (len(fields)==len(header) and 'column' in rule and values.get(rule['column'])==rule['equals'])]
            if services:
                if len(services)!=1:raise ValueError('Неоднозначное правило служебной строки')
                ledger.append({'line_start':start,'line_end':line,'kind':'service','reason':services[0]['reason']})
                continue
            if len(fields)!=len(header):
                raise ValueError('Неверное число полей: ожидалось '+str(len(header))+', получено '+str(len(fields)))
            row=parse_row(values,profile,sha,locator)
            rows.append(row)
            ledger.append({'line_start':start,'line_end':line,'kind':'operation','operation_id':row['id']})
        except (ValueError,KeyError,TypeError,InvalidOperation) as exc:
            issue={'code':'invalid_row','line_start':start,'line_end':line,'message':str(exc)}
            issues.append(issue);ledger.append({**issue,'kind':'error'})
    registry={'schema_version':'local-cashflow-1.0','period':copy.deepcopy(profile['period']),
              'accounts':[copy.deepcopy(profile['account'])],'coverage':copy.deepcopy(profile['coverage']),
              'sources':[{'id':profile['source_id'],'file':str(source),'locator':'CSV строки','sha256':sha}],
              'operations':rows}
    registry['sources']+=copy.deepcopy(profile.get('control_sources',[]))
    controls=copy.deepcopy(profile.get('bank_controls')) or None
    if controls is not None:
        registry['bank_controls']=controls
    if issues:
        registry['coverage']['complete']=False
    checked=None if issues else audit(rows,controls)
    summary=None if issues else calculate(registry)
    return {'schema_version':VERSION,'ok':not issues and checked['ok'] and summary['ok'],'issues':issues,
            'source':{'path':str(source),'sha256':sha,'bytes':len(raw)},'row_results':ledger,
            'cashflow':registry,'csv_audit':checked,'summary':summary,
            'loads':[{'id':profile['source_id']+':'+sha,'source':profile['source_id'],'sha256':sha}]}


def prepared_csv(rows):
    names=['id','source','page','account','date','debit','credit','purpose','category','category_status','currency']
    names+=sorted({k for row in rows for k in row}-set(names))
    stream=StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=names)
    writer.writeheader();writer.writerows(rows)
    return stream.getvalue()


def same_target(left,right):
    return left.resolve()==right.resolve() or (left.exists() and right.exists() and left.samefile(right))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('profile',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--csv-output',type=Path);args=parser.parse_args(argv)
    destinations=[args.output]+([args.csv_output] if args.csv_output else [])
    if any(same_target(destination,source) for destination in destinations for source in (args.input,args.profile)):
        parser.error('Выходные файлы не могут заменять исходный CSV или профиль')
    if args.csv_output and same_target(args.output,args.csv_output):
        parser.error('Выходные JSON и CSV должны быть разными файлами')
    try:
        profile=json.loads(args.profile.read_text(encoding='utf-8-sig'))
        if isinstance(profile,dict) and isinstance(profile.get('control_sources'),list):
            for reference in profile['control_sources']:
                if not isinstance(reference,dict) or not isinstance(reference.get('file'),str):continue
                for original in (Path(reference['file']),args.profile.parent/reference['file']):
                    if any(same_target(destination,original) for destination in destinations):
                        parser.error('Выходной файл не может заменять источник независимого контроля')
        check_payload(profile);result=import_statement(args.input,profile);check_payload(result)
    except ProfileError as exc:
        result={'schema_version':VERSION,'ok':False,'issues':[{'code':'invalid_profile','message':str(exc)}]}
    except (OSError,ValueError,KeyError,TypeError,InvalidOperation) as exc:
        result={'schema_version':VERSION,'ok':False,'issues':[{'code':'invalid_input','message':str(exc)}]}
    if result['ok'] and args.csv_output:
        atomic_text(args.csv_output,prepared_csv(result['cashflow']['operations']))
    atomic_text(args.output,json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    return 0 if result['ok'] else 2


if __name__=='__main__':
    raise SystemExit(main())
