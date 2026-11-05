#!/usr/bin/env python3
"""Propose own-account transfer pairs and apply evidence-bound analyst decisions."""
from __future__ import annotations

import argparse
import copy
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import tempfile

from calculate_cashflow import calculate, validate
from check_cashflow import amount, audit, date_value, operation_fingerprint, transfer_evidence
from check_terminology import check_payload

VERSION = 'local-own-transfers-1.0'


def pair_matches(a, b, owners):
    return (a['account'] != b['account'] and owners[a['account']] == owners[b['account']]
            and a.get('counterparty_account') == b['account']
            and b.get('counterparty_account') == a['account']
            and ((amount(a['debit']) > 0 and amount(a['debit']) == amount(b['credit']))
                 or (amount(a['credit']) > 0 and amount(a['credit']) == amount(b['debit']))))


def match(data):
    if not isinstance(data,dict) or data.get('schema_version') != VERSION:
        raise ValueError('Нужна схема '+VERSION)
    cashflow = copy.deepcopy(data['cashflow'])
    validate(cashflow)
    rows = cashflow['operations']
    by_id = {r['id']:r for r in rows}
    owners = {a['account']:a['owner_id'] for a in cashflow['accounts']}
    external = data.get('external_accounts', [])
    if not isinstance(external,list):
        raise ValueError('external_accounts должен быть массивом подтверждённых счетов')
    external_owners = {}
    for account in external:
        if (not isinstance(account,dict) or any(not isinstance(account.get(k),str) or not account[k].strip()
                for k in ('account','owner_id')) or account['account'] in owners or account['account'] in external_owners):
            raise ValueError('Внешние счета требуют уникальные account/owner_id вне представленного охвата')
        external_owners[account['account']] = account['owner_id']
    outside = []
    for row in rows:
        target = row.get('counterparty_account')
        if target in external_owners and external_owners[target] == owners[row['account']]:
            row.update(category='own_out' if amount(row['debit']) else 'own_in',category_status='confirmed')
            outside.append({'operation_id':row['id'],'account':row['account'],'counterparty_account':target,
                            'owner_id':owners[row['account']],'date':row['date'],
                            'amount':str(amount(row['debit']) or amount(row['credit'])),
                            'source':row['source'],'page':row['page']})
    candidates = []
    for i, a in enumerate(rows):
        for b in rows[i+1:]:
            if pair_matches(a, b, owners):
                candidates.append({'operation_ids':sorted([a['id'],b['id']]),
                                   'dates':[a['date'],b['date']],
                                   'amount':str(amount(a['debit']) or amount(a['credit'])),
                                   'fingerprints':{r['id']:operation_fingerprint(r) for r in (a,b)}})
    confirmed = []
    issues = []
    used = set()
    resolved = set()
    saved_groups = defaultdict(list)
    for row in rows:
        if row.get('pair_id'):
            saved_groups[row['pair_id']].append(row)
    for pair_id, group in sorted(saved_groups.items()):
        if (len(group) == 2 and pair_matches(*group,owners)
                and all(row.get('category_status') == 'confirmed' for row in group)
                and audit(group)['ok']):
            confirmation = transfer_evidence(*group,pair_id)
            confirmed.append(confirmation)
            used.update(row['id'] for row in group)
            resolved.add(tuple(sorted(row['id'] for row in group)))
    decisions = data.get('decisions', [])
    if not isinstance(decisions,list) or any(not isinstance(d,dict) for d in decisions):
        raise ValueError('decisions должен быть массивом решений')
    confirmed_uses = Counter(uid for d in decisions if d.get('action') == 'confirm' for uid in d.get('operation_ids',[]))
    pair_uses = Counter(d.get('pair_id') for d in decisions)
    operation_pair_uses = Counter(tuple(sorted(d.get('operation_ids',[]))) for d in decisions)
    for decision in decisions:
        if not isinstance(decision.get('pair_id'),str) or not decision['pair_id'].strip():
            raise ValueError('Решение требует непустой pair_id')
        ids = sorted(decision['operation_ids'])
        if len(ids) != 2 or len(set(ids)) != 2 or any(uid not in by_id for uid in ids):
            raise ValueError('Решение требует две разные известные операции')
        if operation_pair_uses[tuple(ids)] > 1:
            issues.append({'code':'duplicate_operation_decision','operation_ids':ids})
            continue
        if pair_uses[decision['pair_id']] > 1:
            issues.append({'code':'duplicate_pair_decision','pair_id':decision['pair_id']})
            continue
        if decision.get('action') == 'confirm' and any(confirmed_uses[uid] > 1 for uid in ids):
            issues.append({'code':'operation_reused','operation_ids':ids})
            continue
        a, b = (by_id[uid] for uid in ids)
        if decision.get('fingerprints') != {uid:operation_fingerprint(by_id[uid]) for uid in ids}:
            issues.append({'code':'stale_decision','pair_id':decision.get('pair_id'),'operation_ids':ids})
            continue
        if not pair_matches(a,b,owners):
            issues.append({'code':'invalid_pair_decision','operation_ids':ids})
            continue
        if any(uid in used for uid in ids):
            issues.append({'code':'operation_reused','operation_ids':ids})
            continue
        if not isinstance(decision.get('reason'),str) or not decision['reason'].strip():
            raise ValueError('Решение требует основание')
        if decision['action'] == 'reject':
            resolved.add(tuple(ids))
            continue
        if decision['action'] != 'confirm':
            raise ValueError('Решение требует действие confirm или reject')
        confirmation = {'pair_id':decision['pair_id'],'operation_ids':ids,
                        'fingerprints':decision['fingerprints'],'owner_id':owners[a['account']],
                        'accounts':sorted([a['account'],b['account']]),'reason':decision['reason'],
                        'evidence':copy.deepcopy(decision['evidence'])}
        encoded = json.dumps(confirmation,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        temporary_pair = [dict(row, transfer_confirmation=encoded) for row in (a,b)]
        try:
            transfer_evidence(*temporary_pair,decision['pair_id'])
            if any(source not in {s['id'] for s in cashflow['sources']} for source in decision['evidence']['sources']):
                raise ValueError('Неизвестный источник подтверждения')
        except (ValueError,TypeError,KeyError) as exc:
            issues.append({'code':'invalid_transfer_confirmation','operation_ids':ids,'message':str(exc)})
            continue
        for row in (a,b):
            row.update(category='own_transfer_matched',category_status='confirmed',
                       pair_id=decision['pair_id'],transfer_confirmation=encoded)
        confirmed.append(confirmation)
        used.update(ids)
        resolved.add(tuple(ids))
    unresolved = [c for c in candidates if tuple(c['operation_ids']) not in resolved
                  and not any(uid in used for uid in c['operation_ids'])]
    covered = {uid for candidate in unresolved for uid in candidate['operation_ids']}
    for row in rows:
        target = row.get('counterparty_account')
        if (row['id'] not in used and row['id'] not in covered and target in owners
                and owners[target] == owners[row['account']]):
            unresolved.append({'code':'incomplete_pair','operation_ids':[row['id']],
                               'counterparty_account':target})
    calculation = calculate(cashflow)
    return {'schema_version':VERSION,'ok':not issues and not unresolved and calculation['ok'],
            'complete':not issues and not unresolved and calculation['complete'],
            'outside_transfers':outside,
            'candidates':candidates,'confirmed_pairs':confirmed,'unresolved':unresolved,
            'issues':issues,'decisions':copy.deepcopy(data.get('decisions',[])),
            'cashflow_input':cashflow,'calculation':calculation,
            'limits':['Пары подтверждает аналитик по оригиналам; совпадение суммы не доказывает связь.']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args(argv)
    if args.input.resolve()==args.output.resolve():
        parser.error('Исходник и результат должны быть разными файлами')
    try:
        data=json.loads(args.input.read_text(encoding='utf-8-sig'))
        check_payload(data)
        result=match(data)
        check_payload(result)
    except (OSError,ValueError,TypeError,KeyError) as exc:
        result={'schema_version':VERSION,'ok':False,'issues':[{'code':'invalid_input','message':str(exc)}]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=args.output.parent,delete=False) as f:
        json.dump(result,f,ensure_ascii=False,sort_keys=True,indent=2);f.write('\n');temporary=f.name
    os.replace(temporary,args.output)
    return 0 if result['ok'] else 2


if __name__=='__main__':
    raise SystemExit(main())
