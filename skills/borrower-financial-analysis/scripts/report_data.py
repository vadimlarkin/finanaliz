"""Prepare evidence-linked display data; never reconstruct raw accounting records."""
from __future__ import annotations
import math
import calendar
from datetime import date, timedelta

from analyze import ACCEPTED, InputError, day, number

FLOW_ROWS = (
    ('revenue', 'Выручка'),
    ('operating_profit', 'Прибыль от продаж'),
    ('net_profit', 'Чистая прибыль'),
)
STOCK_ROWS = (
    ('total_assets', 'Активы всего'),
    ('fixed_assets', 'Основные средства'),
    ('inventory', 'Запасы'),
    ('receivables', 'Дебиторская задолженность'),
    ('cash', 'Деньги'),
    ('long_term_investments', 'Долгосрочные финансовые вложения'),
    ('short_term_investments', 'Краткосрочные финансовые вложения'),
    ('equity', 'Собственный капитал'),
    ('long_term_debt', 'Долгосрочные кредиты и займы'),
    ('short_term_debt', 'Краткосрочные кредиты и займы'),
    ('accounts_payable', 'Кредиторская задолженность'),
)
CHART_ROWS = (
    ('revenue', 'Выручка'),
    ('net_credit_turnover', 'ЧКО'),
    ('principal_paid', 'Основной долг'),
    ('cash_interest_paid', 'Проценты'),
)


def _object(raw, allowed, label):
    if not isinstance(raw, dict) or set(raw)-set(allowed):
        raise InputError(f'{label}: неверные поля или формат')


def _period(raw, assessment, year, full_year=False):
    _object(raw, {'start', 'end'}, 'report.period')
    start, end = (day(raw.get(k), 'report.period.'+k) for k in ('start', 'end'))
    if start > end or start.year != year or end.year != year or end > assessment:
        raise InputError('report: неверный период или будущая дата')
    if full_year and (start.isoformat() != f'{year}-01-01' or end.isoformat() != f'{year}-12-31'):
        raise InputError('report: для предыдущих лет нужен полный календарный год')
    return start, end


def _fact(raw, key, sources, period=None, as_of=None, reconstructed=False):
    if raw is None:
        return {'value': None, 'status': 'missing', 'sources': [], 'reconstructed': False}
    _object(raw, {'value', 'status', 'sources', 'derivation', 'period_start', 'period_end', 'as_of'}, 'report.'+key)
    if raw.get('status') not in ACCEPTED | {'unverified', 'conflicting', 'missing'}:
        raise InputError(f'report.{key}: неверный статус')
    refs = raw.get('sources', [])
    if not isinstance(refs, list) or any(not isinstance(s, str) or s not in sources for s in refs):
        raise InputError(f'report.{key}: неизвестный источник')
    value = number(raw['value'], 'report.'+key) if raw.get('value') is not None else None
    if value is not None and not math.isfinite(float(value)):
        raise InputError(f'report.{key}: число выходит за поддерживаемый диапазон')
    signed = {'revenue', 'operating_profit', 'net_profit', 'equity', 'net_credit_turnover', 'profit_before_tax', 'one_off_net_income'}
    if value is not None and key not in signed and value < 0:
        raise InputError(f'report.{key}: остатки и выплаты должны быть неотрицательными')
    if raw.get('status') in ACCEPTED:
        if value is None or not refs:
            raise InputError(f'report.{key}: подтверждённый факт требует значения и источника')
        if raw['status'] == 'calculated' and (not isinstance(raw.get('derivation'), str) or not raw['derivation'].strip()):
            raise InputError(f'report.{key}: нужен расчёт derivation')
        if reconstructed and raw['status'] != 'calculated':
            raise InputError(f'report.{key}: восстановление из ОСВ требует расчётного факта')
    if value is not None:
        if period and (raw.get('period_start') != period['start'] or raw.get('period_end') != period['end']):
            raise InputError(f'report.{key}: несовпадающий период')
        if as_of and raw.get('as_of') != as_of:
            raise InputError(f'report.{key}: несовпадающая дата остатка')
    accepted = raw.get('status') in ACCEPTED and raw.get('value') is not None
    result = dict(raw)
    result['value'] = float(number(raw['value'], 'report')) if accepted else None
    if value is not None and not accepted:
        result['reported_value'] = float(value)
    result['reconstructed'] = reconstructed and accepted
    return result


def prepare_report(data):
    assessment = day(data['assessment_date'], 'assessment_date')
    current_year = assessment.year
    raw = data.get('report', {})
    _object(raw, {'financial_history', 'chart', 'current_debt', 'interest_ltm'}, 'report')
    raw_history = raw.get('financial_history', [])
    if not isinstance(raw_history, list):
        raise InputError('report.financial_history: требуется массив')
    sources = {s['id'] for s in data['sources']}
    entries = {}
    for entry in raw_history:
        _object(entry, {'year', 'balance_date', 'period', 'basis', 'facts'}, 'financial_history')
        year = entry.get('year')
        if isinstance(year, bool) or not isinstance(year, int) or year not in range(current_year-2, current_year+1) or year in entries:
            raise InputError('report: нужны уникальные годы, два предыдущих и текущий')
        start, end = _period(entry.get('period'), assessment, year, year < current_year)
        if year == current_year and start.isoformat() != f'{year}-01-01':
            raise InputError('report: текущие результаты должны идти с начала года')
        if entry.get('balance_date') != end.isoformat():
            raise InputError('report: дата баланса должна совпадать с концом периода')
        if entry.get('basis') != ('osv' if year == current_year else 'statements'):
            raise InputError('report: предыдущие годы берутся из отчётности, текущий из ОСВ')
        _object(entry.get('facts', {}), {key for key, _ in FLOW_ROWS+STOCK_ROWS}, 'financial_history.facts')
        entries[year] = entry
    history = []
    for year in range(current_year-2, current_year+1):
        entry = entries.get(year, {})
        facts = entry.get('facts', {})
        history.append({
            'year': year,
            'balance_date': entry.get('balance_date', f'{year}-12-31' if year < current_year else None),
            'period': entry.get('period'),
            'basis': entry.get('basis'),
            'facts': {key: _fact(facts.get(key), key, sources,
                                 period=entry.get('period') if (key, _) in FLOW_ROWS else None,
                                 as_of=entry.get('balance_date') if (key, _) in STOCK_ROWS else None,
                                 reconstructed=entry.get('basis') == 'osv')
                      for key, _ in FLOW_ROWS+STOCK_ROWS},
        })
    chart = raw.get('chart', {})
    _object(chart, {'period', 'bank_data_complete', 'facts'}, 'report.chart')
    if not isinstance(chart.get('bank_data_complete', False), bool):
        raise InputError('report.chart.bank_data_complete: требуется логическое значение')
    if chart.get('period') is not None:
        _period(chart['period'], assessment, current_year)
    elif chart.get('facts'):
        raise InputError('report.chart: для значений нужен общий период')
    _object(chart.get('facts', {}), {key for key, _ in CHART_ROWS}, 'report.chart.facts')
    result = {'financial_history': history, 'chart': {
        'period': chart.get('period'),
        'bank_data_complete': chart.get('bank_data_complete', False),
        'facts': {key: _fact(chart.get('facts', {}).get(key), key, sources, period=chart.get('period')) for key, _ in CHART_ROWS},
    }}
    ltm = raw.get('interest_ltm')
    ltm_keys = {'operating_profit', 'interest_expense', 'profit_before_tax', 'one_off_net_income'}
    period = None
    if ltm is not None:
        _object(ltm, {'period', 'facts'}, 'report.interest_ltm')
        period = ltm.get('period')
        _object(period, {'start', 'end'}, 'report.interest_ltm.period')
        start, end = (day(period.get(key), 'report.interest_ltm.'+key) for key in ('start', 'end'))
        if end.year < 2 or end > assessment:
            raise InputError('report.interest_ltm: неверная или будущая дата')
        # At month end use twelve complete calendar months, including leap February.
        expected_start = (date(end.year-1+(end.month==12), end.month%12+1, 1)
                          if end.day == calendar.monthrange(end.year, end.month)[1]
                          else end.replace(year=end.year-1)+timedelta(days=1))
        if start != expected_start:
            raise InputError('report.interest_ltm: нужен период ровно последних 12 месяцев')
        latest = max([day(data['period']['end'], 'period.end')]+[
            day(entry['period']['end'], 'report.period.end') for entry in history
            if entry['period'] and any(entry['facts'][key]['value'] is not None for key, _ in FLOW_ROWS)])
        if end < latest:
            raise InputError('report.interest_ltm: есть финансовые данные на более позднюю дату')
        _object(ltm.get('facts', {}), ltm_keys, 'report.interest_ltm.facts')
    ltm_facts = {key: _fact((ltm or {}).get('facts', {}).get(key), key, sources, period=period)
                 for key in sorted(ltm_keys)}
    for key in ltm_keys:
        candidates = [data['facts'].get(key, {})] if period == data['period'] else []
        if key == 'operating_profit':
            candidates += [entry['facts'][key] for entry in history if period and entry['period'] == period]
        for other in candidates:
            a, b = ltm_facts[key]['value'], other.get('value')
            if a is not None and b is not None and other.get('status') in ACCEPTED and number(a, key) != number(b, key):
                raise InputError(f'report.interest_ltm.{key}: противоречие за один период')
    result['interest_ltm'] = {'period': period, 'facts': ltm_facts}
    debt = raw.get('current_debt')
    debt_keys = {'bank_principal', 'nonbank_principal', 'related_party_principal', 'interest_bearing_debt', 'equity'}
    if debt is not None:
        _object(debt, {'as_of', 'facts'}, 'report.current_debt')
        as_of = day(debt.get('as_of'), 'report.current_debt.as_of')
        if as_of > assessment:
            raise InputError('report.current_debt: будущая дата')
        _object(debt.get('facts', {}), debt_keys, 'report.current_debt.facts')
    as_of = debt['as_of'] if debt is not None else None
    debt_facts = {key: _fact((debt or {}).get('facts', {}).get(key), key, sources, as_of=as_of)
                  for key in sorted(debt_keys)}
    gross, related = (debt_facts[key]['value'] for key in ('nonbank_principal', 'related_party_principal'))
    if gross is not None and related is not None and related > gross:
        raise InputError('report.current_debt: связанные займы превышают небанковский основной долг')
    debt_facts['unrelated_nonbank_principal'] = {
        'value': gross-related if gross is not None and related is not None else None,
        'status': 'calculated' if gross is not None and related is not None else 'missing',
        'sources': sorted(set(debt_facts['nonbank_principal'].get('sources', [])+debt_facts['related_party_principal'].get('sources', []))),
        'derivation': 'Небанковский основной долг минус основной долг по связанным займам.',
        'as_of': as_of, 'reconstructed': False,
    }
    result['current_debt'] = {'as_of': as_of, 'provided': debt is not None, 'facts': debt_facts}
    if debt is not None:
        for key in ('interest_bearing_debt', 'equity'):
            candidates = [data['facts'].get(key, {})] if as_of == data['balance_date'] else []
            if key == 'equity':
                candidates += [entry['facts'][key] for entry in history if entry['balance_date'] == as_of]
            for other in candidates:
                a, b = debt_facts[key]['value'], other.get('value')
                if a is not None and b is not None and other.get('status') in ACCEPTED and number(a, key) != number(b, key):
                    raise InputError(f'report.current_debt.{key}: противоречие остатков на одну дату')
    for entry in history:
        for key in ('operating_profit', 'equity'):
            shown = entry['facts'][key]
            base = data['facts'].get(key, {})
            comparable = (entry['period'] == data['period'] if key == 'operating_profit'
                          else entry['balance_date'] == data['balance_date'])
            if comparable and shown['value'] is not None and base.get('status') in ACCEPTED and base.get('value') is not None:
                if number(shown['value'], key) != number(base['value'], key):
                    raise InputError(f'report.{key}: противоречие с расчётными фактами')
        if entry['period'] == result['chart']['period']:
            a, b = entry['facts']['revenue']['value'], result['chart']['facts']['revenue']['value']
            if a is not None and b is not None and a != b:
                raise InputError('report.revenue: выручка таблицы и диаграммы противоречит друг другу')
    return result
