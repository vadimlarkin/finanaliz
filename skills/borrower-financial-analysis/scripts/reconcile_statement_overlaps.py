#!/usr/bin/env python3
"""Reconcile verified local statement appearances without changing source files."""
from __future__ import annotations

import argparse
import copy
from decimal import InvalidOperation
import hashlib
import json
from itertools import combinations
import os
import re
from pathlib import Path
import tempfile

from calculate_cashflow import ALIASES, calculate, validate
from check_terminology import check_payload
from check_cashflow import amount, date_value, normalized

VERSION = 'local-statement-overlaps-1.0'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def semantic(row):
    values={key:value for key,value in row.items() if key not in
            {'id','source','page','load_id','source_row','bank_operation_id'}}
    values['date']=date_value(row['date']).isoformat()
    for field in ('debit','credit'):
        values[field]=format(amount(row[field]),'.2f')
    values['purpose']=normalized(row['purpose'])
    values['category']=ALIASES.get(row['category'],row['category'])
    values['currency']=row.get('currency','RUB')
    return values


def validate_input(data):
    if not isinstance(data,dict) or data.get('schema_version')!=VERSION or set(data)-{'schema_version','cashflow','loads','decisions'}:
        raise ValueError('Требуется схема '+VERSION+' с cashflow, loads и decisions')
    cashflow=data['cashflow']
    checked=copy.deepcopy(cashflow)
    if isinstance(checked,dict) and isinstance(checked.get('operations'),list):
        for index,row in enumerate(checked['operations']):
            if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'].strip():
                raise ValueError('Нужен непустой ID каждой операции')
            row['id']=str(index)+':'+row['id']
    validate(checked)
    source_ids={source['id'] for source in cashflow['sources']}
    if not isinstance(data['loads'],list):
        raise ValueError('loads: требуется массив загрузок')
    loads={};source_versions={}
    for load in data['loads']:
        if not isinstance(load,dict) or set(load)!={'id','source','sha256'} or any(not isinstance(load[key],str) or not load[key].strip() for key in ('id','source','sha256')):
            raise ValueError('load: нужны id, source, sha256')
        if load['id'] in loads:
            raise ValueError('loads: повтор идентификатора загрузки')
        if load['source'] not in source_ids or not re.fullmatch('[0-9a-f]{64}',load['sha256']):
            raise ValueError('load: нужен объявленный source и SHA256 из 64 строчных hex-символов')
        if load['source'] in source_versions and source_versions[load['source']]!=load['sha256']:
            raise ValueError('Один source не может обозначать разные версии исходника')
        loads[load['id']]=load;source_versions[load['source']]=load['sha256']
    appearances={};known_bank_ids={}
    for row in cashflow['operations']:
        if not isinstance(row.get('load_id'),str) or row['load_id'] not in loads or loads[row['load_id']]['source']!=row['source']:
            raise ValueError('Операция требует свою объявленную загрузку с тем же source: '+row['id'])
        if not isinstance(row.get('source_row'),str) or not row['source_row'].strip():
            raise ValueError('Нужен точный source_row операции: '+row['id'])
        if 'bank_operation_id' in row and (not isinstance(row['bank_operation_id'],str) or not row['bank_operation_id'].strip()):
            raise ValueError('bank_operation_id должен быть непустой строкой: '+row['id'])
        if row.get('bank_operation_id'):
            previous_bank_id=known_bank_ids.setdefault(row['id'],row['bank_operation_id'])
            if previous_bank_id!=row['bank_operation_id']:
                raise ValueError('Все представления локального ID требуют согласованного bank_operation_id: '+row['id'])
        if row['id'] in appearances:
            previous=appearances[row['id']]
            if (loads[row['load_id']]['sha256'],row['source_row'],semantic(row)) != (loads[previous['load_id']]['sha256'],previous['source_row'],semantic(previous)):
                raise ValueError('Повтор ID допустим только для неизменной строки того же исходника: '+row['id'])
        appearances.setdefault(row['id'],row)
    if not isinstance(data.get('decisions',[]),list):
        raise ValueError('decisions: требуется массив')
    ids={row['id'] for row in cashflow['operations']};seen=set()
    for decision in data.get('decisions',[]):
        if not isinstance(decision,dict) or set(decision)-{'ids','action','keep_id','reason','fingerprints'}:
            raise ValueError('decision: неизвестные поля или формат')
        pair=decision.get('ids')
        if not isinstance(pair,list) or len(pair)!=2 or any(not isinstance(uid,str) or uid not in ids for uid in pair) or pair[0]==pair[1]:
            raise ValueError('decision.ids: нужны два разных известных ID операции')
        key=tuple(sorted(pair))
        if key in seen:
            raise ValueError('Повтор или противоречащие решения одной пары')
        seen.add(key)
        if decision.get('action') not in {'distinct','duplicate'} or not isinstance(decision.get('reason'),str) or not decision['reason'].strip():
            raise ValueError('decision: нужны action duplicate/distinct и непустое reason')
        if decision['action']=='duplicate' and decision.get('keep_id') not in pair:
            raise ValueError('decision.keep_id: нужна выбранная операция этой пары')
        if decision['action']=='distinct' and 'keep_id' in decision:
            raise ValueError('distinct не выбирает редакцию keep_id')
        snapshots=decision.get('fingerprints')
        if not isinstance(snapshots,dict) or set(snapshots)!=set(pair) or any(not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value) for value in snapshots.values()):
            raise ValueError('decision.fingerprints: нужны SHA256 обоих снимков')


def reconcile(data):
    validate_input(data)
    cashflow=copy.deepcopy(data['cashflow'])
    loads={x['id']:x for x in data['loads']}
    rows=cashflow['operations']
    parents={row['id']:row['id'] for row in rows}
    def root(uid):
        while parents[uid]!=uid:
            uid=parents[uid]
        return uid
    edges=[];candidates=[];buckets={};weak_keys=[];meanings=[]
    for index,row in enumerate(rows):
        meaning=semantic(row);meanings.append(meaning)
        weak=(row['account'],meaning['date'],meaning['debit'],meaning['credit'],meaning['purpose'])
        weak_keys.append(weak)
        keys=[('source',loads[row['load_id']]['sha256'],row['source_row']),('weak',)+weak]
        if row.get('bank_operation_id'):
            keys.append(('bank',row['account'],row['bank_operation_id']))
        for key in keys:
            buckets.setdefault(key,[]).append(index)
    pairs={pair for indices in buckets.values() for pair in combinations(indices,2)}
    for i,j in sorted(pairs):
        left,right=rows[i],rows[j]
        same_row=(loads[left['load_id']]['sha256']==loads[right['load_id']]['sha256']
                  and left['source_row']==right['source_row'])
        same_bank=(left.get('bank_operation_id') and left.get('bank_operation_id')==right.get('bank_operation_id')
                   and left['account']==right['account'])
        if same_row or same_bank:
            a_values,b_values=meanings[i],meanings[j]
            different=sorted(key for key in set(a_values)|set(b_values) if a_values.get(key)!=b_values.get(key))
            if same_row and left.get('bank_operation_id') and right.get('bank_operation_id') and left['bank_operation_id']!=right['bank_operation_id']:
                different.append('bank_operation_id')
            if different:
                candidates.append({'ids':sorted([left['id'],right['id']]),'code':'identity_conflict','differing_fields':different})
                continue
            a,b=root(left['id']),root(right['id']);parents[max(a,b)]=min(a,b)
            edges.append({'ids':sorted([left['id'],right['id']]),'basis':'source_hash_row' if same_row else 'bank_operation_id'})
        elif left.get('bank_operation_id') and right.get('bank_operation_id') and left['bank_operation_id']!=right['bank_operation_id']:
            continue
        elif weak_keys[i]==weak_keys[j]:
            candidates.append({'ids':sorted([left['id'],right['id']]),'code':'possible_duplicate'})
    original_candidates=copy.deepcopy(candidates)
    sources={source['id']:source for source in cashflow['sources']}
    accounts={account['account']:account for account in cashflow['accounts']}
    snapshots={}
    for row in rows:
        value={'operation':row,'load':loads[row['load_id']], 'source':sources[row['source']],
               'account':accounts[row['account']]}
        snapshots.setdefault(row['id'],set()).add(fingerprint(value))
    fingerprints={uid:fingerprint(sorted(values)) for uid,values in snapshots.items()}
    decisions=copy.deepcopy(data.get('decisions',[]));applied=[];preferred=[];issues=[]
    for decision in decisions:
        pair=sorted(decision['ids'])
        expected={uid:fingerprints[uid] for uid in pair}
        if decision['fingerprints']!=expected:
            issues.append({'code':'stale_decision','ids':pair,'message':'Снимок операции или источника изменён; решение не применяется.'})
            continue
        match=next((candidate for candidate in candidates if candidate['ids']==pair),None)
        if match is None:
            issues.append({'code':'decision_not_applicable','ids':pair,'message':'Решение не относится к текущему неразрешённому совпадению.'})
            continue
        candidates.remove(match);applied.append(decision)
        if decision['action']=='duplicate':
            a,b=(root(uid) for uid in pair);parents[max(a,b)]=min(a,b)
            preferred.append(decision['keep_id'])
            edges.append({'ids':pair,'basis':'analyst_decision'})
    conflicts=[]
    for decision in applied:
        if decision['action']=='distinct' and root(decision['ids'][0])==root(decision['ids'][1]):
            conflicts.append(sorted(decision['ids']))
    selected_groups={}
    for uid in preferred:
        selected_groups.setdefault(root(uid),set()).add(uid)
    conflicts.extend(sorted(values) for values in selected_groups.values() if len(values)>1)
    identity_groups=sorted({tuple(sorted(row['id'] for row in rows if root(row['id'])==root(candidate['ids'][0])))
                            for candidate in candidates if candidate['code']=='identity_conflict'
                            and root(candidate['ids'][0])==root(candidate['ids'][1])})
    if conflicts or identity_groups:
        if conflicts:
            issues.append({'code':'conflicting_decisions','groups':conflicts,
                           'message':'Различение внутри группы повторов или разные keep_id требуют единого решения.'})
        if identity_groups:
            issues.append({'code':'inconsistent_identity_group','groups':identity_groups,
                           'message':'Автоматическая группа содержит несогласованные реквизиты; требуется проверка.'})
        parents={row['id']:row['id'] for row in rows}
        edges=[];applied=[];preferred=[];candidates=original_candidates
    else:
        separate={tuple(sorted(root(uid) for uid in decision['ids'])) for decision in applied if decision['action']=='distinct'}
        candidates=[candidate for candidate in candidates
                    if candidate['code']=='identity_conflict' or
                    (root(candidate['ids'][0])!=root(candidate['ids'][1])
                     and tuple(sorted(root(uid) for uid in candidate['ids'])) not in separate)]
    groups={}
    for row in rows:
        groups.setdefault(root(row['id']),[]).append(row)
    lineage=[];operations=[];duplicates=[]
    for uid,group in sorted(groups.items()):
        selected=[row for row in group if row['id'] in preferred]
        chosen=selected[0] if selected else min(group,key=lambda r:r['id'])
        operations.append(chosen)
        origins=[{'operation_id':r['id'],'source':r['source'],'page':r['page'],
                  'load_id':r['load_id'],'source_row':r['source_row'],
                  'source_sha256':loads[r['load_id']]['sha256']} for r in sorted(group,key=lambda r:r['id'])]
        lineage.append({'operation_id':chosen['id'],'appearances':origins})
        chosen['statement_appearances']=copy.deepcopy(origins)
        for row in sorted(group,key=lambda r:r['id']):
            if row is not chosen:
                edge=next((e for e in edges if e['basis']=='analyst_decision' and any(r['id'] in e['ids'] for r in group)),None) or next((e for e in edges if row['id'] in e['ids']),{'basis':'source_hash_row'})
                duplicates.append({'kept_id':chosen['id'],'duplicate_id':row['id'],'basis':edge['basis']})
    cashflow['operations']=operations
    summary=calculate(cashflow)
    provisional=None
    clean=not candidates and not issues
    if not clean:
        provisional=summary;summary=None
        provisional.update(ok=False,complete=False,pdf_compatible=False,pdf_patch=None)
        cashflow['coverage']['complete']=False
    return {'schema_version':VERSION,'ok':clean and summary['ok'],'clean':clean,'registry':cashflow,'summary':summary,
            'provisional_summary':provisional,'issues':issues,
            'confirmed_duplicates':duplicates,'candidates':candidates,'unresolved_matches':copy.deepcopy(candidates),
            'loads':copy.deepcopy(data['loads']),'lineage':lineage,'raw_operations':copy.deepcopy(data['cashflow']['operations']),
            'decisions':decisions,'applied_decisions':applied,'operation_fingerprints':fingerprints}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args(argv)
    if args.input.resolve()==args.output.resolve() or (args.output.exists() and args.input.exists() and args.output.samefile(args.input)):
        parser.error('Вход и результат должны быть разными файлами')
    try:
        data=json.loads(args.input.read_text(encoding='utf-8-sig'))
        if isinstance(data,dict) and isinstance(data.get('cashflow'),dict) and isinstance(data['cashflow'].get('sources'),list):
            for reference in data['cashflow']['sources']:
                if not isinstance(reference,dict) or not isinstance(reference.get('file'),str):
                    continue
                path=Path(reference['file'])
                for original in (path,args.input.parent/path):
                    if args.output.resolve()==original.resolve() or (args.output.exists() and original.exists() and args.output.samefile(original)):
                        parser.error('Результат не может заменять указанный исходный файл')
        check_payload(data);result=reconcile(data);check_payload(result)
    except (OSError,ValueError,KeyError,TypeError,InvalidOperation) as exc:
        result={'schema_version':VERSION,'ok':False,'clean':False,'issues':[{'code':'invalid_input','message':'Неподдерживаемый диапазон или точность денежной суммы' if isinstance(exc,InvalidOperation) else str(exc)}]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=args.output.parent,delete=False) as stream:
        json.dump(result,stream,ensure_ascii=False,sort_keys=True,indent=2);stream.write('\n');temporary=stream.name
    os.replace(temporary,args.output)
    return 0 if result['ok'] else 2


if __name__=='__main__':
    raise SystemExit(main())
