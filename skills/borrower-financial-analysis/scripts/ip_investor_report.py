"""Approved cash-basis investor form for an IP without accounting records."""
from __future__ import annotations
import copy
import os
import tempfile
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas
from analyze import InputError, day
from report_data import _fact, _object
from ip_credit_load import prepare_credit_load
from check_terminology import check_payload, check_file
from ip_cashflow_report import prepare as prepare_controls, dec, fmt, draw_monthly_receipts
from render_dashboard import (PAPER_W, PAPER_H, PALE, BLUE, register_fonts, draw_logo,
                              rect, rule, label, text_box, date_label, amount)

LAYOUT_VERSION='ip-investor-one-page-3.2'
FLOW_KEYS=('revenue','received_loans','own_in','other_in','debt_payments','payroll','suppliers','taxes','own_out','other_out')


def prepare(data):
    _object(data,{'schema_version','borrower_name','inn','fictional','assessment_date','sources','period','bank_scope',
                  'bank','flows','customer_refunds','misc_breakdown','monthly_revenue','bki','debt_payments','loan','project','conclusions','credit_load'},'ip-investor')
    if data.get('schema_version') not in {'ip-investor-3.0','ip-investor-3.1'}:raise InputError('Требуется schema_version ip-investor-3.1')
    check_payload(data)
    inn=data.get('inn')
    if not isinstance(inn,str) or len(inn)!=12 or not inn.isascii() or not inn.isdigit():raise InputError('ИНН ИП должен содержать 12 цифр')
    sources=data.get('sources',[])
    if not isinstance(sources,list) or any(not isinstance(s,dict) or not isinstance(s.get('id'),str) for s in sources):raise InputError('Не указаны источники')
    ids={s['id'] for s in sources};window=data.get('period')
    raw=data.get('flows',{});_object(raw,FLOW_KEYS,'flows')
    flows={k:_fact(raw.get(k),k,ids,period=window) for k in FLOW_KEYS}
    bki=data.get('bki',{});_object(bki,{'report_date','record_period','principal','interest','total','overdue','active_contracts'},'bki')
    numeric_bki=any(isinstance(bki.get(k),dict) and bki[k].get('value') is not None for k in ('principal','interest','total','overdue','active_contracts'))
    if numeric_bki and (not bki.get('report_date') or not bki.get('record_period')):raise InputError('Суммы БКИ требуют дат отчёта и записей')
    control_debt_period=bki.get('record_period') or {'start':data.get('assessment_date'),'end':data.get('assessment_date')}
    loan=data.get('loan',{});_object(loan,{'principal','net_proceeds','commission','investor_rate_pct','effective_investor_rate_pct','term_months','repayment'},'loan')
    # Existing bank, period, provenance and monthly controls are reused without accounting calculations.
    control=prepare_controls({'schema_version':'ip-pdf-2.0','borrower_name':data.get('borrower_name'),'fictional':data.get('fictional',False),
        'assessment_date':data.get('assessment_date'),'sources':sources,'period':window,'bank_scope':data.get('bank_scope'),
        'bank':data.get('bank'),'inflows':[],'outflows':[],
        'monthly_client_receipts':data.get('monthly_revenue',[]),'flow_notes':[],
        'debt':{'period':control_debt_period,**{k:bki.get(k) for k in ('principal','total','overdue','active_contracts')}},
        'loan':{'amount':loan.get('principal'),'term_months':loan.get('term_months')},'banner':'Проверка исходных сумм',
        'findings':[{'text':'Банковские данные','sources':list(ids)[:1]}],'limitations':[{'text':'Охват определяется представленными источниками','sources':list(ids)[:1]}]})
    # Reuse the customer-series control with the new cash-revenue label.
    monthly_total=sum(dec(m['fact']['value']) for m in control['monthly_client_receipts'] if m['fact']['value'] is not None)
    if control['monthly_client_receipts'] and all(m['fact']['value'] is not None for m in control['monthly_client_receipts']) and flows['revenue']['value'] is not None and abs(monthly_total-dec(flows['revenue']['value']))>dec('.01'):
        raise InputError('Выручка по месяцам не сходится с итогом')
    refunds=_fact(data.get('customer_refunds'),'customer_refunds',ids,period=window)
    refund_adjustment=dec(refunds['value']) if refunds['value'] is not None else dec(0)
    for side,keys in [('inflows_total',('revenue','received_loans','own_in','other_in')),('outflows_total',('debt_payments','payroll','suppliers','taxes','own_out','other_out'))]:
        total=control['calculations'][side]
        if total is not None:
            expected=dec(total)-refund_adjustment
            if expected<0:raise InputError('Возвраты заказчикам превышают поддерживаемый очищенный оборот')
            if all(flows[k]['value'] is not None for k in keys) and abs(sum(dec(flows[k]['value']) for k in keys)-expected)>dec('.01'):raise InputError('Группы потока не сходятся с очищенным банковским оборотом')
            control['calculations'][side]=float(expected)
    assessment=day(data['assessment_date'],'assessment_date');report_date=day(bki['report_date'],'bki.report_date') if bki.get('report_date') else None
    if report_date and (report_date>assessment or bki.get('record_period') and day(bki['record_period']['end'],'bki.record_period.end')>report_date):raise InputError('Даты БКИ позже отчёта или даты анализа')
    bki_facts={k:_fact(bki.get(k),k,ids,period=bki.get('record_period')) for k in ('principal','interest','total','overdue','active_contracts')}
    def check_sum(parts,target,message):
        if all(f['value'] is not None for f in parts+[target]) and abs(sum(dec(f['value']) for f in parts)-dec(target['value']))>dec('.01'):raise InputError(message)
    check_sum([bki_facts['principal'],bki_facts['interest']],bki_facts['total'],'Компоненты долга БКИ не сходятся')
    misc=data.get('misc_breakdown',{});_object(misc,{'fees_services','card','penalties'},'misc_breakdown')
    misc={k:_fact(misc.get(k),k,ids,period=window) for k in ('fees_services','card','penalties')}
    check_sum(list(misc.values()),flows['other_out'],'Прочие списания не сходятся с расшифровкой')
    pay=data.get('debt_payments',{});_object(pay,{'bank','nonbank','own_platform','platform_contracts','platform_name'},'debt_payments')
    payments={k:_fact(pay.get(k),k,ids,period=window) for k in ('bank','nonbank','own_platform','platform_contracts')}
    check_sum([payments['bank'],payments['nonbank']],flows['debt_payments'],'Выплаты банкам и небанковским кредиторам не сходятся')
    own=payments['own_platform']['value'];nonbank=payments['nonbank']['value'];contracts=payments['platform_contracts']['value']
    if own is not None and nonbank is not None and own>nonbank:raise InputError('Выплаты платформе превышают небанковские выплаты')
    if contracts is not None and not float(contracts).is_integer():raise InputError('Число договоров должно быть целым')
    if own is not None and own>0 and (contracts is None or contracts<=0):raise InputError('Для истории выплат нужны подтверждённые договоры')
    if not isinstance(pay.get('platform_name'),str) or not pay['platform_name'].strip():raise InputError('Не указано название платформы')
    loan_facts={k:_fact(loan.get(k),k,ids) for k in ('principal','net_proceeds','commission','investor_rate_pct','effective_investor_rate_pct','term_months')}
    check_sum([loan_facts['net_proceeds'],loan_facts['commission']],loan_facts['principal'],'Основной долг не равен полученным средствам и комиссии')
    if loan.get('repayment') not in {'end','schedule','unknown'}:raise InputError('Не установлен порядок погашения')
    def note(raw,name):
        _object(raw,{'text','sources'},name)
        if not isinstance(raw.get('text'),str) or not raw['text'].strip() or not isinstance(raw.get('sources'),list) or not raw['sources'] or any(not isinstance(s,str) or s not in ids for s in raw['sources']):raise InputError(name+': нужны текст и источники')
        return copy.deepcopy(raw)
    project=data.get('project',{});_object(project,{'description','escrow','escrow_bank','security'},'project')
    if not isinstance(project.get('escrow_bank'),str) or not project['escrow_bank'].strip():raise InputError('Не указан банк эскроу')
    project={'description':note(project.get('description'),'project.description'),'escrow':_fact(project.get('escrow'),'escrow',ids),
             'escrow_bank':project['escrow_bank'],'security':note(project.get('security'),'project.security')}
    conclusions=data.get('conclusions')
    if not isinstance(conclusions,list) or not 1<=len(conclusions)<=3:raise InputError('Нужны от одного до трёх пунктов вывода')
    conclusions=[note(n,'conclusions') for n in conclusions]
    calculations={k:control['calculations'][k] for k in ('cash_change','inflows_total','outflows_total','monthly_client_average')}
    calculations['monthly_client_average']['derivation']='Сумма денежной выручки за полные календарные месяцы / число этих месяцев; подтверждённые нули включены.'
    credit_load=prepare_credit_load(data.get('credit_load'),loan_facts['term_months']['value'],data['assessment_date'],ids)
    return {'schema_version':data['schema_version'],'layout_version':LAYOUT_VERSION,'borrower_name':data['borrower_name'],'inn':inn,
            'fictional':data.get('fictional',False),'assessment_date':data['assessment_date'],'sources':copy.deepcopy(sources),
            'period':copy.deepcopy(window),'bank_scope':copy.deepcopy(data['bank_scope']),'bank':control['bank'],
            'flows':flows,'customer_refunds':refunds,'misc_breakdown':misc,'monthly_client_receipts':control['monthly_client_receipts'],'calculations':calculations,
            'bki':{'report_date':bki.get('report_date'),'record_period':copy.deepcopy(bki.get('record_period')),**bki_facts},
            'debt_payments':{**payments,'platform_name':pay['platform_name']},'loan':{**loan_facts,'repayment':loan['repayment']},
            'project':project,'conclusions':conclusions,'credit_load':credit_load}


def render(data,destination,font_dir=None):
    from ip_investor_layout import render_prepared
    return render_prepared(prepare(data),destination,font_dir)
