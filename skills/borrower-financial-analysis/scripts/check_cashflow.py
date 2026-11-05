#!/usr/bin/env python3
"""Audit prepared cashflow CSV; never classify or modify source documents."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ZERO = Decimal('0')
REQUIRED = {'id', 'source', 'page', 'account', 'date', 'debit', 'credit', 'purpose', 'category'}
INTEREST = {'interest', 'проценты', 'проценты по долгу'}
PENALTY = {'penalty', 'пени', 'штрафы', 'пени по долгу', 'пени и штрафы'}
FEE = {'fee', 'комиссии', 'комиссии и банковское обслуживание'}
PRINCIPAL = {'principal', 'основной долг', 'погашение основного долга'}
MATCHED_TRANSFER = {'own_transfer_matched', 'собственный перевод между представленными счетами'}
PENALTY_PATTERN = re.compile(
    r'(?:(?:погашение|уплата|оплата|выплата|перечисление|списание|возврат)\s+'
    r'(?:(?:начисленных|просроченных|текущих)\s+)?(?:пен[яией]+|штраф\w*|неусто\w*)\b'
    r'|^(?:пени|штраф\w*|неусто\w*)\s+(?:за|по)\b)')
FEE_PATTERN = re.compile(r'(?:(?:выплата|перечисление|погашение|уплата|оплата|списание|возврат)\s+комисси\w*\b|^комиссия\s+за\b)')
PRINCIPAL_PATTERN = re.compile(r'(?:(?:погашение|возврат|уплата)\s+основного\s+долга\b|погашение\s+(?:(?:срочной|просроченной)\s+)?ссуды\b)')
INTEREST_PATTERN = re.compile(
    r'(?:(?:выплата|перечисление|погашение|уплата|оплата|списание|возврат)\s+'
    r'(?:(?:просроченных|начисленных|текущих|неуплаченных)\s+)?процент\w*\b|^проценты\s+(?:по|за)\b)')
UNSPLIT_RETURN = re.compile(r'^возврат\s+(?:денежных\s+средств\s+|средств\s+)?(?:по\s+договору\s+)?(?:займа|кредита)\b')


def normalized(value):
    text = unicodedata.normalize('NFKC', str(value))
    return ' '.join(''.join(c for c in text if unicodedata.category(c) != 'Cf').lower().split())


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Денежное поле не является числом') from exc
    if not result.is_finite() or result < 0 or result != result.quantize(Decimal('.01')):
        raise ValueError('Требуется неотрицательная сумма с точностью до копейки')
    return result


def date_value(value):
    for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError('Неверная календарная дата')


def money(value):
    return format(value, '.2f')


def audit(rows, controls=None):
    issues = []
    accounts = defaultdict(lambda: {'debit': ZERO, 'credit': ZERO, 'count': 0})
    categories = defaultdict(lambda: {'debit': ZERO, 'credit': ZERO})
    prepared = []
    ids = set()

    def fail(code, message, row=None):
        item = {'code': code, 'message': message}
        if row:
            item.update(operation_id=row.get('id'), source=row.get('source'), page=row.get('page'))
        issues.append(item)

    for row in rows:
        try:
            if None in row or not REQUIRED.issubset(row) or any(row[k] is None or not str(row[k]).strip() for k in REQUIRED):
                raise ValueError('Отсутствуют обязательные поля операции')
            if row['id'] in ids:
                raise ValueError('Повтор ID операции')
            ids.add(row['id'])
            debit, credit = amount(row['debit']), amount(row['credit'])
            if (debit == ZERO) == (credit == ZERO):
                raise ValueError('Операция должна иметь одну ненулевую сторону')
            if row.get('currency', 'RUB') != 'RUB':
                raise ValueError('Проверка рассчитана на рубли; валюты нельзя складывать')
            date_value(row['date'])
        except (ValueError, InvalidOperation) as exc:
            fail('invalid_operation', str(exc), row)
            continue
        purpose = normalized(row['purpose'])
        category = normalized(row['category'])
        matched = False
        for pattern, allowed, code, label in (
            (PENALTY_PATTERN, PENALTY, 'penalty_misclassified', 'Пени или штраф'),
            (FEE_PATTERN, FEE, 'fee_misclassified', 'Комиссия'),
            (PRINCIPAL_PATTERN, PRINCIPAL, 'principal_misclassified', 'Погашение основного долга'),
            (INTEREST_PATTERN, INTEREST, 'interest_misclassified', 'Явно процентный платёж'),
        ):
            if pattern.search(purpose):
                matched = True
                if category not in allowed:
                    fail(code, label + ': категория не соответствует явному назначению', row)
                break
        if not matched and UNSPLIT_RETURN.search(purpose) and category in INTEREST:
            fail('loan_return_is_not_interest', 'Название договора с процентами не определяет состав возврата', row)
        prepared.append((row, debit, credit))
        for field, value in (('debit', debit), ('credit', credit)):
            accounts[row['account']][field] += value
            categories[row['category']][field] += value
        accounts[row['account']]['count'] += 1

    pair_groups = defaultdict(list)
    for row, debit, credit in prepared:
        pair_id = row.get('pair_id', '').strip()
        matched_category = normalized(row['category']) in MATCHED_TRANSFER
        if matched_category and not pair_id:
            fail('missing_transfer_pair', 'Для встречного собственного перевода нужен ID пары', row)
        if pair_id:
            if not matched_category:
                fail('unexpected_pair_category', 'Пара отмечена у операции другой категории', row)
            pair_groups[pair_id].append((row, debit, credit))
    paired_amount, matched_pairs = ZERO, 0
    for pair_id, group in pair_groups.items():
        outgoing = [x for x in group if x[1] > ZERO]
        incoming = [x for x in group if x[2] > ZERO]
        valid = len(group) == 2 and len(outgoing) == len(incoming) == 1
        if valid:
            a, debit, _ = outgoing[0]
            b, _, credit = incoming[0]
            valid = (debit == credit and a['account'] != b['account']
                     and a['account'] == b.get('counterparty_account')
                     and b['account'] == a.get('counterparty_account')
                     and date_value(a['date']) == date_value(b['date'])
                     and all(normalized(x[0]['category']) in MATCHED_TRANSFER for x in group))
        if not valid:
            fail('invalid_transfer_pair', 'Не сходятся стороны собственного перевода: ' + pair_id)
        else:
            paired_amount += debit
            matched_pairs += 1

    bank_accounts = 0
    if controls is not None:
        if not isinstance(controls, list):
            fail('invalid_controls', 'Банковские контроли должны быть массивом счетов')
            controls = []
        seen = set()
        for control in controls:
            try:
                account = control['account']
                if account in seen:
                    raise ValueError('Повтор счёта в банковских контролях')
                seen.add(account)
                opening, debit, credit, closing = (amount(control[k]) for k in ('opening', 'debit', 'credit', 'closing'))
                if control.get('currency', 'RUB') != 'RUB':
                    raise ValueError('Банковские контроли содержат другую валюту')
                if opening + credit - debit != closing:
                    fail('bank_balance_mismatch', 'Банковские остатки и обороты не сходятся: ' + account)
                actual = accounts[account]
                if actual['debit'] != debit or actual['credit'] != credit:
                    fail('bank_turnover_mismatch', 'Строки не совпадают с банковскими оборотами: ' + account)
                if 'bank_count' in control:
                    count = control['bank_count']
                    if type(count) is not int or count < 0:
                        raise ValueError('Число операций должно быть целым неотрицательным')
                    if count != actual['count']:
                        fail('bank_count_mismatch', 'Число операций не совпадает: ' + account)
                bank_accounts += 1
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                fail('invalid_controls', str(exc))
        for account in set(accounts) - seen:
            fail('missing_bank_control', 'Нет банковского контроля для счёта: ' + account)

    return {
        'schema_version': '1.0', 'ok': not issues,
        'checks': {'operation_count': len(rows), 'valid_operation_count': len(prepared),
                   'bank_accounts': bank_accounts, 'bank_controls_supplied': controls is not None,
                   'matched_pairs': matched_pairs},
        'limits': ['Проверка сверяет подготовленные данные и явные назначения; полноту бизнеса и достоверность PDF она не подтверждает.'],
        'totals': {'debit': money(sum((v['debit'] for v in accounts.values()), ZERO)),
                   'credit': money(sum((v['credit'] for v in accounts.values()), ZERO)),
                   'paired_transfer_each_side': money(paired_amount),
                   'after_matched_transfers': {
                       'debit': money(sum((v['debit'] for v in accounts.values()), ZERO) - paired_amount),
                       'credit': money(sum((v['credit'] for v in accounts.values()), ZERO) - paired_amount)},
                   'by_category': {k: {f: money(v) for f, v in values.items()} for k, values in sorted(categories.items())}},
        'issues': issues,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('transactions', help='Проверенный CSV операций')
    parser.add_argument('--bank-controls', help='JSON с независимыми банковскими итогами')
    args = parser.parse_args(argv)
    try:
        with Path(args.transactions).open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None or not REQUIRED.issubset(reader.fieldnames) or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError('Нет обязательных столбцов или есть повтор заголовка')
            rows = list(reader)
        controls = json.loads(Path(args.bank_controls).read_text(encoding='utf-8-sig')) if args.bank_controls else None
        result = audit(rows, controls)
    except (OSError, ValueError, TypeError, InvalidOperation) as exc:
        result = {'schema_version': '1.0', 'ok': False, 'issues': [{'code': 'invalid_input', 'message': str(exc)}]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
