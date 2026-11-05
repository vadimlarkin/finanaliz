"""Evidence-linked monthly payment scenario for the proposed IP loan term."""
from decimal import Decimal, ROUND_HALF_UP
import re
from analyze import InputError, day
from report_data import _fact, _object

CENT = Decimal('.01')
GENITIVE = ('января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря')
ACCUSATIVE = ('январь','февраль','март','апрель','май','июнь','июль','август','сентябрь','октябрь','ноябрь','декабрь')


def month_index(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', value) or value[:4] == '0000':
        raise InputError('credit_load: месяц требуется в формате ГГГГ-ММ')
    return int(value[:4])*12 + int(value[5:])-1


def month_name(index):
    year, month = divmod(index, 12)
    if not 1 <= year <= 9999:
        raise InputError('credit_load: период выходит за допустимые даты')
    return f'{year:04d}-{month+1:02d}'


def rub(value):
    return None if value is None else str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def prepare_credit_load(raw, term, assessment_date, source_ids):
    """Return a scenario with monthly evidence, totals and display periods."""
    supplied = raw is not None
    raw = {} if raw is None else raw
    _object(raw, {'start_month','start_basis','start_sources','contracts'}, 'credit_load')
    assessment = day(assessment_date, 'assessment_date')
    start = raw.get('start_month', assessment_date[:7])
    first = month_index(start)
    basis = raw.get('start_basis', 'assessment_month')
    refs = raw.get('start_sources', [])
    if basis not in {'assessment_month','planned_disbursement','user_scenario'}:
        raise InputError('credit_load: неизвестное основание начала периода')
    if not isinstance(refs, list) or any(x not in source_ids for x in refs):
        raise InputError('credit_load: неизвестный источник периода')
    if basis != 'assessment_month' and not refs:
        raise InputError('credit_load: выбранное начало требует источника')
    if basis != 'assessment_month' and 'start_month' not in raw:
        raise InputError('credit_load: плановый или выбранный месяц должен быть указан явно')
    if basis == 'assessment_month' and start != assessment_date[:7]:
        raise InputError('credit_load: месяц проверки не совпадает с началом периода')
    if term is None:
        count = 0
    elif isinstance(term, bool) or Decimal(str(term)) != int(term) or not 1 <= int(term) <= 1200:
        raise InputError('credit_load: срок требуется в целых месяцах от 1 до 1200')
    else:
        count = int(term)
    months = [month_name(first+i) for i in range(count)]
    contracts = raw.get('contracts', [])
    if not isinstance(contracts, list):
        raise InputError('credit_load.contracts: требуется массив')
    ids, event_ids, prepared, contract_rows = set(), set(), [], []
    regular_values = []

    def fact(raw_fact, name):
        normalized = _fact(raw_fact, name, source_ids)
        if raw_fact and raw_fact.get('as_of'):
            if day(raw_fact['as_of'], name+'.as_of') > assessment:
                raise InputError('credit_load: дата исходного платежа позже проверки')
        value = None if normalized['value'] is None else Decimal(str(raw_fact['value']))
        if value is not None:
            value = value.quantize(CENT, rounding=ROUND_HALF_UP)
        return normalized, value

    for contract in contracts:
        _object(contract, {'id','monthly_payment','monthly_until','payments'}, 'credit_load.contract')
        uid = contract.get('id')
        if not isinstance(uid, str) or not uid.strip() or uid in ids:
            raise InputError('credit_load: нужны уникальные идентификаторы обязательств')
        ids.add(uid)
        monthly_fact, monthly = fact(contract.get('monthly_payment'), 'monthly_payment')
        until = month_index(contract['monthly_until']) if contract.get('monthly_until') else None
        if monthly is not None and (until is None or first <= until):
            regular_values.append(monthly)
        payments = contract.get('payments', [])
        if not isinstance(payments, list):
            raise InputError('credit_load.payments: требуется массив')
        by_month, events = {}, []
        for payment in payments:
            _object(payment, {'id','date','treatment','amount'}, 'credit_load.payment')
            pid = payment.get('id')
            if not isinstance(pid, str) or not pid.strip() or pid in event_ids:
                raise InputError('credit_load: повтор или отсутствие идентификатора платежа')
            event_ids.add(pid)
            stamp = day(payment.get('date'), 'credit_load.payment.date')
            treatment = payment.get('treatment')
            if treatment not in {'replace','additional'}:
                raise InputError('credit_load: платёж должен заменять месячную базу или добавляться к ней')
            amount_fact, value = fact(payment.get('amount'), 'dated_payment')
            event = {'id':pid, 'date':stamp.isoformat(), 'treatment':treatment, 'amount':amount_fact}
            events.append(event)
            by_month.setdefault(stamp.isoformat()[:7], []).append((treatment, value, pid))
        values = []
        for i, month in enumerate(months):
            recurring = monthly if monthly is None or until is None or first+i <= until else Decimal(0)
            included = recurring
            entries = by_month.get(month, [])
            replacements = [value for kind, value, _ in entries if kind == 'replace']
            additions = [value for kind, value, _ in entries if kind == 'additional']
            if replacements:
                included = sum((v for v in replacements if v is not None), Decimal(0)) if any(v is not None for v in replacements) else None
            if additions:
                known = [v for v in additions if v is not None]
                if known:
                    included = (included or Decimal(0)) + sum(known, Decimal(0))
            unknown = (included is None or any(v is None for _, v, _ in entries))
            values.append({'month':month, 'included_rub':rub(included), 'unknown':unknown,
                           'payment_ids':[pid for _, _, pid in entries]})
        contract_rows.append(values)
        prepared.append({'id':uid, 'monthly_payment':monthly_fact, 'monthly_until':contract.get('monthly_until'),
                         'payments':events, 'months':values})

    rows = []
    for i, month in enumerate(months):
        items = [contract[i] for contract in contract_rows]
        known = [Decimal(item['included_rub']) for item in items if item['included_rub'] is not None]
        rows.append({'month':month, 'included_rub':rub(sum(known, Decimal(0))) if known else None,
                     'unknown_contracts':[prepared[j]['id'] for j, item in enumerate(items) if item['unknown']]})
    known_rows = [Decimal(row['included_rub']) for row in rows if row['included_rub'] is not None]
    total = sum(known_rows, Decimal(0)) if known_rows else None
    bucket = 1 if count <= 12 else 3 if count <= 36 else 12*((count+143)//144)
    calendar = []
    for i in range(0, count, bucket):
        subset = rows[i:i+bucket]
        known = [Decimal(row['included_rub']) for row in subset if row['included_rub'] is not None]
        calendar.append({'start_month':subset[0]['month'], 'end_month':subset[-1]['month'],
                         'included_rub':rub(sum(known, Decimal(0))) if known else None})
    ending = months[-1] if months else None
    heading = 'Расчёт кредитной нагрузки'
    if ending:
        heading += ' до '+GENITIVE[int(ending[5:])-1]+' '+ending[:4]+' года'
    suffix = 'месяц' if count % 10 == 1 and count % 100 != 11 else 'месяца' if count % 10 in (2,3,4) and count % 100 not in (12,13,14) else 'месяцев'
    caption = ('Расчётный сценарий: '+str(count)+' '+suffix+' с '+GENITIVE[int(start[5:])-1]+' '+start[:4]+
               ' по '+ACCUSATIVE[int(ending[5:])-1]+' '+ending[:4]+'.') if ending else 'Период расчёта не установлен.'
    status = 'missing' if total is None else 'partial' if any(row['unknown_contracts'] for row in rows) else 'calculated'
    return {'status':status, 'period':{'start_month':start,'end_month':ending}, 'period_months':count,
            'start_basis':basis, 'start_sources':refs, 'supplied':supplied,
            'regular_monthly_rub':rub(sum(regular_values, Decimal(0))) if regular_values else None,
            'regular_contracts':len(regular_values), 'total_rub':rub(total), 'months':rows, 'contracts':prepared,
            'calendar':calendar, 'calendar_bucket_months':bucket, 'heading':heading, 'caption':caption}
