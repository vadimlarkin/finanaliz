#!/usr/bin/env python3
"""Evidence-linked screening calculations. Standard library only; no network."""
from __future__ import annotations

import argparse
import calendar
import json
import math
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from check_terminology import check_payload

VERSION = '1.3.1'
FLOW = {
    'operating_profit', 'interest_expense', 'profit_before_tax',
    'one_off_net_income', 'operating_cashflow', 'cash_interest_paid',
    'interest_paid_in_cfo', 'principal_paid', 'portfolio_net_cashflow',
}
STOCK = {'interest_bearing_debt', 'equity', 'current_assets',
         'current_liabilities', 'unrestricted_cash'}
FORECAST = {'receipts_90d', 'operating_payments_90d', 'capex_90d',
            'debt_service_90d'}
LOAN = {'new_loan_amount', 'new_loan_rate_pct', 'new_loan_term_months',
        'new_loan_fees_12m', 'new_loan_interest_12m'}
SIGNED = {'operating_profit', 'profit_before_tax', 'one_off_net_income',
          'operating_cashflow', 'portfolio_net_cashflow', 'equity'}
ACCEPTED = {'confirmed', 'calculated'}


class InputError(ValueError):
    pass


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise InputError(f'{label}: требуется число, не строка/логическое значение')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise InputError(f'{label}: неверное число') from exc
    if not result.is_finite():
        raise InputError(f'{label}: NaN и бесконечности запрещены')
    return result


def day(value, label):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f'{label}: требуется календарная дата YYYY-MM-DD') from exc


def validate(data):
    if not isinstance(data, dict) or data.get('schema_version') != '1.0':
        raise InputError('schema_version должна быть 1.0')
    allowed = {'schema_version', 'company', 'assessment_date', 'balance_date',
               'period', 'currency', 'facts', 'sources', 'loan_repayment',
               'stress', 'context', 'loan_interest_basis', 'report'}
    if set(data) - allowed:
        raise InputError(f'Неизвестные поля: {sorted(set(data)-allowed)}')
    company = data.get('company', {})
    if not isinstance(company.get('name'), str) or not company['name'].strip():
        raise InputError('Не задано название компании')
    if company.get('business_model') not in {'operating', 'financial_intermediary'}:
        raise InputError('business_model: operating или financial_intermediary')
    if data.get('currency') != 'RUB':
        raise InputError('Первая версия принимает суммы в рублях: currency=RUB')
    assessment = day(data.get('assessment_date'), 'assessment_date')
    balance = day(data.get('balance_date'), 'balance_date')
    period = data.get('period', {})
    start, end = (day(period.get(k), 'period.'+k) for k in ('start', 'end'))
    if start > end or end > assessment or balance > assessment:
        raise InputError('Период/баланс не может быть позже даты проверки')
    if balance != end:
        raise InputError('Баланс и последний анализируемый период должны иметь одну дату; устаревшие остатки раскрыть отдельно')
    if (end-start).days > 366:
        raise InputError('Один период не может превышать 12 месяцев')
    sources = data.get('sources', [])
    if not isinstance(sources, list):
        raise InputError('sources должен быть массивом')
    ids = set()
    for source in sources:
        sid = source.get('id')
        if not isinstance(sid, str) or not sid or sid in ids:
            raise InputError('Идентификаторы источников должны быть непустыми и уникальными')
        ids.add(sid)
        for key in ('file', 'locator'):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise InputError(f'{sid}: не задан {key}')
    facts = data.get('facts', {})
    if not isinstance(facts, dict) or set(facts) - FLOW - STOCK - FORECAST - LOAN:
        raise InputError('facts содержит неизвестные показатели')
    for key, fact in facts.items():
        if not isinstance(fact, dict) or fact.get('status') not in ACCEPTED | {'unverified', 'conflicting', 'missing'}:
            raise InputError(f'{key}: неверный статус факта')
        refs = fact.get('sources', [])
        if not isinstance(refs, list) or any(s not in ids for s in refs):
            raise InputError(f'{key}: ссылка на неизвестный источник')
        if fact.get('value') is None:
            if fact['status'] in ACCEPTED:
                raise InputError(f'{key}: подтверждённый факт должен иметь значение')
            continue
        value = number(fact['value'], key)
        if key not in SIGNED and value < 0:
            raise InputError(f'{key}: расходы, остатки и выплаты вводятся положительными')
        if fact['status'] in ACCEPTED and not refs:
            raise InputError(f'{key}: подтверждение требует источника')
        if fact['status'] == 'calculated' and not fact.get('derivation'):
            raise InputError(f'{key}: расчётный факт требует derivation')
        if key in FLOW and (fact.get('period_start') != period['start'] or fact.get('period_end') != period['end']):
            raise InputError(f'{key}: несовпадающий период; полугодие не смешивать с годом')
        expected_stock_date = data['assessment_date'] if key == 'unrestricted_cash' else data['balance_date']
        if key in STOCK and fact.get('as_of') != expected_stock_date:
            raise InputError(f'{key}: несовпадающая дата остатка')
        if key in FORECAST and fact.get('forecast_as_of') != data['assessment_date']:
            raise InputError(f'{key}: прогноз должен быть на дату проверки')
    def val(k):
        f = facts.get(k, {})
        return number(f['value'], k) if f.get('value') is not None and f.get('status') in ACCEPTED else None
    paid, included = val('cash_interest_paid'), val('interest_paid_in_cfo')
    if paid is not None and included is not None and included > paid:
        raise InputError('В ОДДС не может быть учтено больше уплаченных процентов, чем всего уплачено')
    shock = number(data.get('stress', {}).get('earnings_drop_pct', 20), 'stress')
    if not 0 <= shock <= 100:
        raise InputError('Стресс: снижение прибыли должно быть от 0 до 100%')
    context = data.get('context', {})
    for key in ('observations', 'questions', 'limitations'):
        if not isinstance(context.get(key, []), list) or any(not isinstance(t, str) for t in context.get(key, [])):
            raise InputError(f'context.{key}: требуется массив строк')
    for flag in context.get('flags', []):
        if (not isinstance(flag, dict) or not isinstance(flag.get('text'), str)
                or flag.get('severity') not in {'high', 'attention'}
                or not flag.get('sources') or any(s not in ids for s in flag['sources'])):
            raise InputError('Флаг риска требует текста, severity и действующих источников')
    if data.get('loan_repayment', 'unknown') not in {'bullet', 'schedule', 'unknown'}:
        raise InputError('loan_repayment: bullet, schedule или unknown')
    if data.get('loan_interest_basis', 'unknown') not in {'simple_monthly', 'schedule', 'unknown'}:
        raise InputError('loan_interest_basis: simple_monthly, schedule или unknown')
    if (data.get('loan_repayment') == 'schedule' and val('new_loan_rate_pct') == 0
            and val('new_loan_interest_12m') is not None and val('new_loan_interest_12m') > 0):
        raise InputError('Нулевая ставка противоречит положительным процентам по графику нового займа')


def analyze(data):
    validate(data)
    facts = data['facts']
    model = data['company']['business_model']
    metrics = []
    limitations = list(data.get('context', {}).get('limitations', []))

    def value(k):
        f = facts.get(k, {})
        return number(f['value'], k) if f.get('value') is not None and f.get('status') in ACCEPTED else None

    def refs(keys):
        return sorted({r for k in keys for r in facts.get(k, {}).get('sources', [])})

    def has(keys):
        return all(value(k) is not None for k in keys)

    def metric(mid, title, keys, formula, compute, low, high, maximum,
               note, reverse=False, contextual=False):
        missing = [k for k in keys if value(k) is None]
        val = None if missing else compute()
        if val is not None and (not val.is_finite() or not math.isfinite(float(val))):
            raise InputError(f'{mid}: нечисловой результат')
        state = 'unknown'
        if val is not None:
            if contextual:
                state = 'context'
            elif reverse:
                state = 'green' if val <= low else 'yellow' if val <= high else 'red'
            else:
                state = 'red' if val < low else 'yellow' if val < high else 'green'
        item = {'id': mid, 'title': title, 'value': float(val) if val is not None else None,
                'status': state, 'sources': refs(keys), 'formula': formula,
                'thresholds': [float(low), float(high)], 'scale_max': float(maximum),
                'reverse': reverse, 'note': note, 'missing': missing}
        if val is None:
            item['display'] = 'Нет данных' if missing else 'Не применимо'
        else:
            item['display'] = f'{val:.2f}'.replace('.', ',')+'×'
        metrics.append(item)
        return val

    if model == 'operating':
        earnings_keys = ['operating_profit']
        earnings = value('operating_profit')
        earnings_formula = 'Прибыль от продаж'
    else:
        earnings_keys = ['profit_before_tax', 'interest_expense', 'one_off_net_income']
        earnings = (value('profit_before_tax')+value('interest_expense')-value('one_off_net_income')) if has(earnings_keys) else None
        earnings_formula = 'Прибыль до налога + расходы фондирования − чистый разовый доход'
        limitations.append('Для посредника проверить стоимость и собираемость портфеля, сроки активов и фондирования.')

    metric('interest_coverage', 'Покрытие процентов', earnings_keys+['interest_expense'],
           earnings_formula+' / проценты',
           lambda: earnings/value('interest_expense') if value('interest_expense') > 0 else None,
           Decimal('1'), Decimal('3'), Decimal('5'),
           'Заработок до процентов на 1 ₽ начисленных процентов.')
    metric('leverage', 'Долговая нагрузка', ['interest_bearing_debt', 'equity'],
           'Процентный долг / собственный капитал',
           lambda: value('interest_bearing_debt')/value('equity') if value('equity') > 0 else None,
           Decimal('1'), Decimal('3'), Decimal('5'),
           'Для посредника шкала нейтральна: нужен отраслевой ориентир.' if model != 'operating' else 'Только процентный долг, без обычных авансов и кредиторки.',
           reverse=True, contextual=model != 'operating')
    liquidity_keys = ['unrestricted_cash', 'receipts_90d', 'operating_payments_90d', 'capex_90d', 'debt_service_90d']
    if has(liquidity_keys):
        metric('liquidity', 'Деньги на 90 дней', liquidity_keys,
               '(Свободные деньги + поступления − операционные выплаты − капзатраты) / выплаты по долгу',
               lambda: (value('unrestricted_cash')+value('receipts_90d')-value('operating_payments_90d')-value('capex_90d'))/value('debt_service_90d') if value('debt_service_90d') > 0 else None,
               Decimal('1'), Decimal('1.3'), Decimal('3'), 'По платёжному плану; поступления могут задержаться.')
    elif model == 'operating':
        metric('liquidity', 'Ликвидность баланса', ['current_assets', 'current_liabilities'],
               'Оборотные активы / краткосрочные обязательства',
               lambda: value('current_assets')/value('current_liabilities') if value('current_liabilities') > 0 else None,
               Decimal('1'), Decimal('1.5'), Decimal('3'),
               'Приближение: активы не равны доступным деньгам.', contextual=True)
        limitations.append('Нет полного плана на 90 дней; балансовая ликвидность не подтверждает деньги на выплаты.')
    else:
        metric('liquidity', 'Деньги на 90 дней', liquidity_keys, 'Плановое покрытие выплат',
               lambda: None, Decimal('1'), Decimal('1.3'), Decimal('3'), 'Нужны сроки сбора портфеля и обязательных выплат.')

    cash_keys = ['operating_cashflow', 'cash_interest_paid', 'interest_paid_in_cfo', 'principal_paid']
    if model != 'operating':
        cash_keys.append('portfolio_net_cashflow')
    def cash_cover():
        denominator = value('cash_interest_paid')+value('principal_paid')
        numerator = value('operating_cashflow')+value('interest_paid_in_cfo')
        if model != 'operating':
            numerator += value('portfolio_net_cashflow')
        return numerator/denominator if denominator > 0 else None
    metric('cash_coverage', 'Денежное покрытие', cash_keys,
           '(ОДДС + проценты в ОДДС + поправка портфеля) / (уплаченные проценты + основной долг)',
           cash_cover, Decimal('1'), Decimal('1.3'), Decimal('3'),
           'Исторический поток до капзатрат; это не прогноз и не DSCR.')

    start, end = (day(data['period'][k], k) for k in ('start', 'end'))
    annual = start == date(start.year, 1, 1) and end == date(start.year, 12, 31)
    newkeys = earnings_keys+['interest_expense', 'new_loan_amount', 'new_loan_term_months', 'new_loan_fees_12m']
    if data.get('loan_repayment') == 'bullet':
        newkeys += ['new_loan_rate_pct']
    else:
        newkeys += ['new_loan_interest_12m']
    new_interest = None
    known_loan = (data.get('loan_repayment') == 'bullet' and data.get('loan_interest_basis') == 'simple_monthly') or (data.get('loan_repayment') == 'schedule' and data.get('loan_interest_basis') == 'schedule')
    if annual and known_loan and has(newkeys):
        if value('new_loan_amount') <= 0 or value('new_loan_term_months') <= 0:
            raise InputError('Сумма и срок нового займа должны быть больше нуля')
        if data.get('loan_repayment') == 'bullet':
            new_interest = value('new_loan_amount')*value('new_loan_rate_pct')/100*min(value('new_loan_term_months'), Decimal('12'))/12
        else:
            new_interest = value('new_loan_interest_12m')
        new_interest += value('new_loan_fees_12m')
    total_interest = value('interest_expense')+new_interest if new_interest is not None else None
    metric('new_loan_coverage', 'После нового займа', newkeys,
           'Прошлогодний заработок / (прошлогодние проценты + новые проценты и комиссии за 12 мес.)',
           lambda: earnings/total_interest if total_interest is not None and total_interest > 0 else None,
           Decimal('1'), Decimal('3'), Decimal('5'),
           'Условный сценарий без роста прибыли. Возврат основного долга проверяется отдельно.' if annual else 'Для сравнения нужен полный год или сопоставимые 12 месяцев.')
    if not annual:
        limitations.append('Промежуточный период не пересчитывается автоматически в год: датчик нового займа недоступен.')
    limitations.append('Покрытие процентов не подтверждает источник возврата основного долга.')

    shock = number(data.get('stress', {}).get('earnings_drop_pct', 20), 'stress')
    stress_value = earnings*(1-shock/100)/total_interest if earnings is not None and total_interest is not None and total_interest > 0 else None
    stress = {'earnings_drop_pct': float(shock), 'interest_coverage': float(stress_value) if stress_value is not None else None,
              'status': 'unknown' if stress_value is None else 'red' if stress_value < 1 else 'yellow' if stress_value < 3 else 'green',
              'note': 'Чувствительность прибыли; не прогноз. Основной долг не включён.'}
    conflict = any(f.get('status') == 'conflicting' for f in facts.values())
    stale_days = (day(data['assessment_date'], 'assessment')-end).days
    if stale_days > 180:
        limitations.append(f'После отчётной даты прошло {stale_days} дней; нужны свежие остатки и обороты.')
    if conflict:
        limitations.append('Есть противоречивые источники: спорные факты исключены из расчётов.')
    if value('equity') is not None and value('equity') <= 0:
        limitations.append('Собственный капитал неположительный; отношение долг/капитал неинтерпретируемо.')
    red = [m['title'] for m in metrics if m['status'] == 'red']
    if value('equity') is not None and value('equity') <= 0:
        red.append('неположительный собственный капитал')
    available = sum(m['value'] is not None for m in metrics)
    flags = data.get('context', {}).get('flags', [])
    alert = bool(red) or any(f['severity'] == 'high' for f in flags)
    caution = bool(flags) or conflict or stale_days > 180 or bool(data.get('context', {}).get('limitations')) or stress['status'] in {'red', 'yellow'} or any(m['status'] in {'yellow', 'context', 'unknown'} for m in metrics)
    risk = 'high' if alert else 'attention' if caution else 'not_seen_in_metrics'
    if risk == 'high':
        conclusion = 'Выявлены существенные сигналы финансового риска.'
    elif risk == 'attention':
        conclusion = 'Есть ограничения или показатели, требующие уточнения.'
    else:
        conclusion = 'По рассчитанным показателям тревожных сигналов не выявлено.'
    data_status = 'limited' if available < 5 or conflict or stale_days > 180 or data.get('context', {}).get('limitations') or any(m['status'] == 'context' for m in metrics) else 'screening_only'
    observations = list(data.get('context', {}).get('observations', []))
    observations = [f['text']+' ['+', '.join(f['sources'])+']' for f in flags]+observations
    if conflict:
        observations.insert(0, 'Противоречивые сведения исключены из расчётов; нужны подтверждения.')
    if stale_days > 180:
        observations.insert(0, f'Отчётность старше 180 дней ({stale_days}); текущие остатки не подтверждены.')
    if value('equity') is not None and value('equity') <= 0:
        observations.insert(0, 'Собственный капитал неположительный — отдельный сигнал риска.')
    if not observations:
        observations = ['Сигналы: '+', '.join(red)+'.'] if red else ['Цвета относятся к отдельным показателям, а не к вероятности возврата займа.']
    questions = list(data.get('context', {}).get('questions', []))
    if not questions:
        questions = ['Чем подтверждён возврат основного долга?', 'Есть ли свежий платёжный календарь и расшифровка долгов?']
    result = {'schema_version': '1.0', 'engine_version': VERSION,
            'company': data['company'], 'assessment_date': data['assessment_date'],
            'balance_date': data['balance_date'], 'period': data['period'],
            'metrics': metrics, 'stress': stress, 'risk': risk,
            'conclusion': conclusion, 'data_status': data_status,
            'calculation_coverage': f'{available}/5', 'stale_days': stale_days,
            'observations': observations, 'questions': questions,
            'limitations': list(dict.fromkeys(limitations)), 'sources': data['sources'],
            'new_interest_12m': float(new_interest) if new_interest is not None else None,
            'scale_notice': 'Цветовые зоны — авторские ориентиры предварительной проверки, не нормативы и не рейтинг.'}
    from report_data import prepare_report
    result['report'] = prepare_report(data)
    def comparison_cell(val, keys, reason, facts=None):
        source_facts = facts if facts is not None else data['facts']
        def accepted(key):
            fact = source_facts.get(key, {})
            return fact.get('status') in ACCEPTED and fact.get('value') is not None
        missing = [key for key in keys if not accepted(key)]
        conflicting = [key for key in keys if source_facts.get(key, {}).get('status') == 'conflicting']
        if val is not None and (not val.is_finite() or not math.isfinite(float(val))):
            raise InputError('Сравнение до и после займа: число вне диапазона')
        return {'value': float(val) if val is not None else None,
                'display': f'{val:.2f}'.replace('.', ',') if val is not None else 'Противоречие' if conflicting else 'Нет данных' if missing else 'Не применимо',
                'status': 'available' if val is not None else 'conflicting' if conflicting else 'missing' if missing else 'not_applicable',
                'missing': missing, 'conflicting': conflicting,
                'sources': sorted({s for key in keys if accepted(key) for s in source_facts[key].get('sources', [])}),
                'reason': '' if val is not None else 'Противоречивые исходные данные' if conflicting else 'Нет исходных данных' if missing else reason}

    interest_keys = earnings_keys+['interest_expense']
    ltm = result['report']['interest_ltm']
    interest_facts = dict(ltm['facts'])
    def interest_value(key):
        fact = interest_facts.get(key, {})
        return number(fact['value'], key) if fact.get('status') in ACCEPTED and fact.get('value') is not None else None
    interest_ready = all(interest_value(key) is not None for key in interest_keys)
    ltm_earnings = (interest_value('operating_profit') if model=='operating' else
                    interest_value('profit_before_tax')+interest_value('interest_expense')-interest_value('one_off_net_income')
                    if interest_ready else None)
    old_interest = interest_value('interest_expense')
    before_interest = ltm_earnings/old_interest if interest_ready and old_interest > 0 else None
    added_keys = ['new_loan_amount','new_loan_term_months']+(['new_loan_rate_pct'] if data.get('loan_repayment') == 'bullet' else ['new_loan_interest_12m'])
    added_interest = None
    for key in added_keys:
        interest_facts[key] = data['facts'].get(key, {})
    if known_loan and has(added_keys):
        if value('new_loan_amount') > 0 and value('new_loan_term_months') > 0:
            added_interest = (value('new_loan_amount')*value('new_loan_rate_pct')/100*min(value('new_loan_term_months'),Decimal('12'))/12
                              if data['loan_repayment']=='bullet' else value('new_loan_interest_12m'))
    after_interest = None
    if interest_ready and added_interest is not None and old_interest+added_interest > 0:
        after_interest = ltm_earnings/(old_interest+added_interest)
    after_reason = ('Не задан расчёт процентов' if added_interest is None else 'Проценты равны нулю')
    leverage_keys = ['interest_bearing_debt','equity']
    current_debt = result['report']['current_debt']
    leverage_facts = dict(current_debt['facts'] if current_debt['provided'] else data['facts'])
    new_debt_keys = ['new_loan_amount']+(['new_loan_rate_pct'] if value('new_loan_rate_pct') is not None else ['new_loan_interest_12m'])
    for key in new_debt_keys:
        leverage_facts[key] = data['facts'].get(key, {})
    def leverage_value(key):
        fact = leverage_facts.get(key, {})
        return number(fact['value'], key) if fact.get('status') in ACCEPTED and fact.get('value') is not None else None
    debt, capital = (leverage_value(key) for key in leverage_keys)
    before_leverage = debt/capital if debt is not None and capital is not None and capital>0 else None
    # A zero contractual rate is explicit evidence that the proposed loan is interest free.
    bears_interest = (value('new_loan_rate_pct') > 0 if value('new_loan_rate_pct') is not None
                      else True if value('new_loan_interest_12m') is not None and value('new_loan_interest_12m')>0 else None)
    after_leverage = ((debt+(value('new_loan_amount') if bears_interest else 0))/capital
                      if before_leverage is not None and value('new_loan_amount') is not None and bears_interest is not None else None)
    result['loan_comparison'] = {
        'assumptions': ['Новый заём выдан полностью; прочий долг не изменился.',
                        'Прибыль и капитал сохраняются на исходном уровне.',
                        'Новые проценты за первые 12 месяцев; комиссии учитываются только в служебных расчётах.'],
        'new_interest_only_12m': float(added_interest) if added_interest is not None else None,
        'rows': [
            {'id':'interest_coverage', 'title':'Прибыль от продаж / проценты' if model=='operating' else 'Заработок до фондирования / проценты',
             'period':ltm['period'],
             'before':comparison_cell(before_interest,interest_keys,'Проценты равны нулю',interest_facts),
             'after':comparison_cell(after_interest,interest_keys+added_keys,after_reason,interest_facts)},
            {'id':'leverage', 'title':'Процентный долг / капитал','as_of':current_debt['as_of'] if current_debt['provided'] else data['balance_date'],
             'before':comparison_cell(before_leverage,leverage_keys,'Капитал ≤ 0',leverage_facts),
             'after':comparison_cell(after_leverage,leverage_keys+new_debt_keys,
                                      'Не подтверждена процентность нового займа' if bears_interest is None else 'Капитал ≤ 0',leverage_facts)},
        ]}
    check_payload(result)
    return result


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f'Повторяющийся ключ JSON: {key}')
        result[key] = value
    return result


def load_input(path):
    return json.loads(Path(path).read_text(encoding='utf-8'), parse_float=Decimal,
                      object_pairs_hook=unique_pairs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_json')
    parser.add_argument('output_json')
    args = parser.parse_args()
    source, output = Path(args.input_json).resolve(), Path(args.output_json).resolve()
    try:
        if source == output:
            raise InputError('Исходный JSON нельзя перезаписывать')
        result = analyze(load_input(source))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    except (InputError, ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Ошибка: {exc}', file=sys.stderr)
        return 2
    print(str(output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
