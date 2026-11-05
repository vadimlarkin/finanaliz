"""Evidence-linked cashflow form for an entrepreneur without accounting records."""
from __future__ import annotations
import calendar
import copy
import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.colors import HexColor
from analyze import InputError, day
from report_data import _object, _fact
from check_terminology import check_payload, check_file
from render_dashboard import (PAPER_W, PAPER_H, INK, PALE, BLUE, LayoutError,
                              register_fonts, draw_logo, rect, rule, label, text_box, date_label, amount)

LAYOUT_VERSION = 'ip-cashflow-one-page-2.0.1'
CURRENT_LAYOUT_VERSION = 'ip-cashflow-one-page-2.2'


def dec(value):
    return Decimal(str(value))


def prepare(data):
    modern = data.get('schema_version') == 'ip-pdf-2.1'
    allowed = {'schema_version','borrower_name','fictional','assessment_date','sources','bank_scope','period',
               'bank','inflows','outflows','flow_notes','monthly_client_receipts','debt','loan','banner',
               'findings','limitations'}
    if modern:allowed |= {'business','purpose','expense_breakdown'}
    _object(data, allowed, 'ip-pdf')
    if data.get('schema_version') not in {'ip-pdf-2.0','ip-pdf-2.1'}:
        raise InputError('Требуется schema_version ip-pdf-2.0 или ip-pdf-2.1')
    check_payload(data)
    assessment = day(data.get('assessment_date'), 'assessment_date')
    if not isinstance(data.get('borrower_name'), str) or not data['borrower_name'].strip():
        raise InputError('Не указан заёмщик')
    if not isinstance(data.get('fictional', False), bool):
        raise InputError('fictional: требуется логическое значение')
    sources = data.get('sources', [])
    if not isinstance(sources, list) or any(not isinstance(s, dict) or any(not isinstance(s.get(k), str) or not s[k].strip() for k in ('id','file','locator')) for s in sources):
        raise InputError('Источники требуют id, file, locator')
    ids = {s['id'] for s in sources}
    if len(ids) != len(sources):raise InputError('Повтор идентификатора источника')

    def period(raw, name):
        _object(raw, {'start','end'}, name)
        a,b=day(raw.get('start'),name+'.start'),day(raw.get('end'),name+'.end')
        if a>b or b>assessment:raise InputError(name+': будущие или обратные даты')
        return raw,a,b

    window,start,end = period(data.get('period'), 'period')
    scope=data.get('bank_scope', {})
    _object(scope, {'accounts','incomplete','operations_count'}, 'bank_scope')
    accounts=scope.get('accounts', [])
    if not isinstance(accounts,list) or any(not isinstance(x,str) or not x.strip() for x in accounts) or len(accounts)!=len(set(accounts)):
        raise InputError('Нужны уникальные счета')
    if not isinstance(scope.get('incomplete'),bool) or not accounts and scope['incomplete'] is False:
        raise InputError('Не установлен охват счетов')
    def count(value, name):
        if isinstance(value,bool) or not isinstance(value,int) or value<0:raise InputError(name+': требуется целое неотрицательное число')
        return value
    count(scope.get('operations_count'), 'operations_count')
    raw_bank=data.get('bank', {})
    _object(raw_bank, {'opening','closing','credits','debits','internal_transfers','transfer_pairs'}, 'bank')
    bank={k:_fact(raw_bank.get(k),k,ids,as_of=window['start'] if k=='opening' else window['end']) for k in ('opening','closing')}
    bank.update({k:_fact(raw_bank.get(k),k,ids,period=window) for k in ('credits','debits','internal_transfers')})
    pairs=count(raw_bank.get('transfer_pairs',0), 'transfer_pairs')
    calculations={}
    def value(f):return f.get('value')
    if all(value(bank[k]) is not None for k in ('opening','closing','credits','debits')):
        change=dec(value(bank['credits']))-dec(value(bank['debits']))
        if abs(dec(value(bank['opening']))+change-dec(value(bank['closing'])))>Decimal('.01'):
            raise InputError('Не сходятся банковские остатки и обороты')
        calculations['cash_change']=float(change)
    else:calculations['cash_change']=None
    internal=value(bank['internal_transfers'])
    for key,total_key in [('inflows','credits'),('outflows','debits')]:
        raw=data.get(key, [])
        if not isinstance(raw,list) or len(raw)>5:raise InputError(key+': не более пяти групп')
        rows=[];seen=set()
        for r in raw:
            _object(r,{'id','label','fact'},key)
            if not isinstance(r.get('id'),str) or not r['id'].strip() or r['id'] in seen:raise InputError('Нужны уникальные ID групп')
            if not isinstance(r.get('label'),str) or not r['label'].strip():raise InputError('Не указана группа потока')
            seen.add(r['id']);rows.append({'id':r['id'],'label':r['label'],'fact':_fact(r.get('fact'),key,ids,period=window)})
        calculations[key+'_total']=None
        if value(bank[total_key]) is not None and internal is not None:
            total=dec(value(bank[total_key]))-dec(internal)
            if total<0:raise InputError('Встречные переводы превышают банковский оборот')
            calculations[key+'_total']=float(total)
            if rows and all(value(r['fact']) is not None for r in rows) and abs(sum(dec(value(r['fact'])) for r in rows)-total)>Decimal('.01'):
                raise InputError(key+': группы не сходятся с оборотом без встречных переводов')
        calculations[key]=rows
    if not accounts and (scope['operations_count'] or any(value(f) is not None for f in bank.values()) or any(value(row['fact']) is not None for key in ('inflows','outflows') for row in calculations[key])):
        raise InputError('Суммы и операции требуют списка счетов')

    def notes(raw, name, limit):
        if not isinstance(raw,list) or len(raw)>limit:raise InputError(name+': превышено число пунктов')
        for row in raw:
            _object(row,{'text','sources'},name)
            if not isinstance(row.get('text'),str) or not row['text'].strip() or not isinstance(row.get('sources'),list) or not row['sources'] or any(s not in ids for s in row['sources']):
                raise InputError(name+': нужен текст со ссылками на источники')
        return copy.deepcopy(raw)
    flow_notes=notes(data.get('flow_notes',[]),'flow_notes',2)
    findings=notes(data.get('findings',[]),'findings',2)
    limitations=notes(data.get('limitations',[]),'limitations',2)
    if not findings or not limitations:raise InputError('Нужны вывод и ограничения')
    if not isinstance(data.get('banner'),str) or not data['banner'].strip():raise InputError('Нужен краткий вывод')
    monthly=data.get('monthly_client_receipts',[])
    if not isinstance(monthly,list) or len(monthly)>13:raise InputError('Не более 13 месячных значений')
    prepared_months=[]
    for m in monthly:
        _object(m,{'month','fact'},'monthly_client_receipts')
        month=m.get('month');a=day(str(month)+'-01','month')
        b=date(a.year,a.month,calendar.monthrange(a.year,a.month)[1])
        if a>end or b<start:raise InputError('Месяц вне окна выписок')
        if prepared_months:
            previous=prepared_months[-1]['month'];py,pm=map(int,previous.split('-'))
            if a != date(py+int(pm==12),1 if pm==12 else pm+1,1):raise InputError('Месяцы должны идти подряд')
        p={'start':max(a,start).isoformat(),'end':min(b,end).isoformat()}
        prepared_months.append({'month':month,'fact':_fact(m.get('fact'),'monthly',ids,period=p),'partial':p['start']!=a.isoformat() or p['end']!=b.isoformat()})
    customer=next((r['fact']['value'] for r in calculations['inflows'] if r['id']=='customer'),None)
    if prepared_months:
        if prepared_months[0]['month']!=start.strftime('%Y-%m') or prepared_months[-1]['month']!=end.strftime('%Y-%m'):raise InputError('Месячная динамика должна охватывать всё окно')
        if customer is not None and all(value(m['fact']) is not None for m in prepared_months) and abs(sum(dec(value(m['fact'])) for m in prepared_months)-dec(customer))>Decimal('.01'):
            raise InputError('Динамика не сходится с поступлениями клиентов')
    full_months = [m for m in prepared_months if not m['partial']]
    missing_months = [m['month'] for m in full_months if value(m['fact']) is None]
    average = None
    if full_months and not missing_months:
        average = float(sum(dec(value(m['fact'])) for m in full_months) / len(full_months))
    calculations['monthly_client_average'] = {
        'value': average,
        'status': 'calculated' if average is not None else 'missing',
        'month_count': len(full_months),
        'months': [m['month'] for m in full_months],
        'missing_months': missing_months,
        'excluded_partial_months': [m['month'] for m in prepared_months if m['partial']],
        'sources': sorted({s for m in full_months for s in m['fact'].get('sources', [])}),
        'derivation': 'Сумма клиентских поступлений за полные календарные месяцы / число полных календарных месяцев; подтверждённые нули включены.',
    }
    raw_debt=data.get('debt',{})
    _object(raw_debt,{'period','active_contracts','principal','total','overdue','payment_indicator','large_payment','notes'} | ({'payment_scope'} if modern else set()),'debt')
    debt_period,a,b=period(raw_debt.get('period'),'debt.period')
    debt={k:_fact(raw_debt.get(k),k,ids,period=debt_period) for k in ('active_contracts','principal','total','overdue','payment_indicator')}
    if debt['active_contracts']['value'] is not None and not float(debt['active_contracts']['value']).is_integer():raise InputError('Число договоров должно быть целым')
    debt['period']=debt_period;debt['notes']=notes(raw_debt.get('notes',[]),'debt.notes',1)
    large=raw_debt.get('large_payment')
    if large is not None:
        _object(large,{'date','total','principal','interest'},'large_payment');due=day(large.get('date'),'large_payment.date')
        debt['large_payment']={'date':due.isoformat(),**{k:_fact(large.get(k),k,ids,as_of=due.isoformat()) for k in ('total','principal','interest')}}
        if all(value(debt['large_payment'][k]) is not None for k in ('total','principal','interest')) and abs(dec(value(debt['large_payment']['principal']))+dec(value(debt['large_payment']['interest']))-dec(value(debt['large_payment']['total'])))>Decimal('.01'):
            raise InputError('Не сходится состав крупного платежа')
    else:debt['large_payment']=None
    loan=data.get('loan',{})
    _object(loan,{'amount','rate_pct','term_months'},'loan')
    loan={k:_fact(loan.get(k),k,ids) for k in ('amount','rate_pct','term_months')}
    term=loan['term_months']['value']
    if term is not None and (term<=0 or not float(term).is_integer()):raise InputError('Срок требуется в положительных целых месяцах')
    result = {'layout_version':CURRENT_LAYOUT_VERSION if modern else LAYOUT_VERSION,'schema_version':data['schema_version'],'borrower_name':data['borrower_name'],
            'fictional':data.get('fictional',False),'assessment_date':data['assessment_date'],'sources':copy.deepcopy(sources),
            'period':copy.deepcopy(window),'bank_scope':copy.deepcopy(scope),'bank':bank,'transfer_pairs':pairs,
            'calculations':calculations,'flow_notes':flow_notes,'monthly_client_receipts':prepared_months,
            'debt':debt,'loan':loan,'banner':data['banner'],'findings':findings,'limitations':limitations}
    if modern:
        result['business'] = notes([data.get('business')], 'business', 1)[0]
        result['purpose'] = notes([data.get('purpose')], 'purpose', 1)[0]
        rows = data.get('expense_breakdown')
        if not isinstance(rows,list) or len(rows)>6:raise InputError('expense_breakdown: не более шести групп')
        expenses=[];seen=set()
        for row in rows:
            _object(row,{'id','label','fact'},'expense_breakdown')
            if not isinstance(row.get('id'),str) or not row['id'].strip() or row['id'] in seen:raise InputError('Нужны уникальные ID расходов')
            if not isinstance(row.get('label'),str) or not row['label'].strip():raise InputError('Не указана группа расходов')
            seen.add(row['id']);expenses.append({'id':row['id'],'label':row['label'],'fact':_fact(row.get('fact'),'expense',ids,period=window)})
        other = next((row['fact']['value'] for row in calculations['outflows'] if row['id']=='other'),None)
        if other is not None and all(row['fact']['value'] is not None for row in expenses) and abs(sum(dec(row['fact']['value']) for row in expenses)-dec(other))>Decimal('.01'):
            raise InputError('Расшифровка не сходится с прочими списаниями')
        result['expense_breakdown'] = expenses
        pay=raw_debt.get('payment_scope')
        _object(pay,{'contracts','frequency','monthly_contracts','large_payment_included','sources'},'payment_scope')
        count(pay.get('contracts'),'payment_scope.contracts')
        if debt['active_contracts']['value'] is not None and pay['contracts']>debt['active_contracts']['value']:raise InputError('Охват платежей превышает число договоров')
        if pay.get('frequency') not in {'monthly','mixed','unknown'}:raise InputError('Не установлена периодичность')
        if 'monthly_contracts' in pay:
            count(pay['monthly_contracts'],'payment_scope.monthly_contracts')
            if pay['monthly_contracts']>pay['contracts'] or pay['frequency']=='monthly' and pay['monthly_contracts']!=pay['contracts']:raise InputError('Не сходится число месячных договоров')
        included=pay.get('large_payment_included')
        if included is not None and not isinstance(included,bool):raise InputError('Нужно явно указать включение крупного платежа')
        refs=pay.get('sources')
        if not isinstance(refs,list) or not refs or any(not isinstance(ref,str) or ref not in ids for ref in refs):raise InputError('Охват платежей требует источников')
        if included and debt['large_payment'] and value(debt['large_payment']['total']) is not None and value(debt['payment_indicator']) is not None and value(debt['payment_indicator'])<value(debt['large_payment']['total']):raise InputError('Ориентир меньше якобы включённого крупного платежа')
        result['debt']['payment_scope'] = copy.deepcopy(pay)
    return result


def fmt(value, divisor=1000000):
    if value is None:return 'Нет данных'
    return f'{value/divisor:,.3f}'.replace(',',' ').replace('.',',')


def chart_amount(value):
    if value is None:
        return 'н/д'
    scaled = dec(value) / 1000000
    if not scaled:
        return '0'
    if scaled < Decimal('.05'):
        places = max(3, 1 - scaled.adjusted())
        return format(scaled, f'.{places}f').rstrip('0').rstrip('.').replace('.', ',')
    return format(scaled, '.1f').replace('.', ',')


def draw_monthly_receipts(c, r, top, height, title='Клиентские поступления по месяцам'):
    """Render the same month and average semantics in both cashflow forms."""
    monthly = r['monthly_client_receipts']
    label(c, 36, top, title, 11, True)
    label(c, 559, top, 'млн ₽', 8, right=True)
    average = r['calculations']['monthly_client_average']
    caption = 'Среднее за полный месяц: ' + chart_amount(average['value'])
    if average['value'] is not None:
        caption += ' млн ₽'
    elif not average['month_count']:
        caption += ', нет полных месяцев'
    else:
        caption += ', есть месяцы без данных'
    if average['value'] is not None:
        c.saveState()
        c.setStrokeColor(HexColor(INK))
        c.setLineWidth(.7)
        c.setDash(3, 2)
        c.line(36, PAPER_H-top-11, 49, PAPER_H-top-11)
        c.restoreState()
    label(c, 54 if average['value'] is not None else 36, top+14, caption, 7.3, width=340)
    label(c, 559, top+14, 'По представленным счетам', 7.1, right=True)
    if not monthly:
        label(c, 36, top+44, 'Месячная динамика не установлена', 9)
        return
    values = [m['fact']['value'] for m in monthly if m['fact']['value'] is not None]
    maximum = max(values+[1])
    baseline = top+31+height
    step = 523/len(monthly)
    rule(c, 36, baseline, 559)
    for i, month in enumerate(monthly):
        value = month['fact']['value']
        bar_height = 0 if value is None else value/maximum*height
        if bar_height:
            rect(c, 36+i*step+6, baseline-bar_height, step-12, bar_height,
                 PALE if month['partial'] else BLUE)
    if average['value'] is not None:
        line_top = baseline-average['value']/maximum*height
        c.saveState()
        c.setStrokeColor(HexColor(INK))
        c.setLineWidth(.7)
        c.setDash(3, 2)
        c.line(36, PAPER_H-line_top, 559, PAPER_H-line_top)
        c.restoreState()
    for i, month in enumerate(monthly):
        center = 36+(i+.5)*step
        value = month['fact']['value']
        bar_height = 0 if value is None else value/maximum*height
        value_top = baseline-bar_height-4
        display = chart_amount(value)
        width = stringWidth(display, 'Body', 7.1)
        # A white backing keeps a nearby average line from crossing the number.
        rect(c, center-width/2-1, value_top-7, width+2, 9, '#FFFFFF')
        label(c, center-width/2, value_top, display, 7.1, width=step-4)
        display = month['month'][5:]+'.'+month['month'][2:4]
        width = stringWidth(display, 'Body', 7.1)
        label(c, center-width/2, baseline+13, display, 7.1, width=step-4)
        if month['partial']:
            start = max(date.fromisoformat(r['period']['start']), date.fromisoformat(month['month']+'-01'))
            last = calendar.monthrange(start.year, start.month)[1]
            end = min(date.fromisoformat(r['period']['end']), date(start.year, start.month, last))
            display = str(start.day)+'-'+str(end.day)
            width = stringWidth(display, 'Body', 6.8)
            label(c, center-width/2, baseline+24, display, 6.8, width=step-4)


def render(data, destination, font_dir=None):
    r=prepare(data)
    if r['schema_version'] == 'ip-pdf-2.1':return render21(r,destination,font_dir)
    register_fonts(font_dir)
    target=Path(destination);target.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(suffix='.pdf',dir=str(target.parent));os.close(fd)
    def box(text,x,top,width,height,size=9,bold=False):
        text_box(c,text,x,PAPER_H-top-height,width,height,size,bold=bold)
    try:
        c=Canvas(temporary,pagesize=(PAPER_W,PAPER_H),invariant=1,pageCompression=1)
        c.setTitle('Финансовый отчёт ИП / '+r['borrower_name']);c.setAuthor('ВБетон');c.setSubject(LAYOUT_VERSION)
        rect(c,0,0,PAPER_W,PAPER_H,'#FFFFFF');draw_logo(c)
        label(c,559,33,'ОБРАЗЕЦ / УСЛОВНЫЕ ДАННЫЕ' if r['fictional'] else 'ФИНАНСОВЫЙ АНАЛИЗ',8,True,True)
        rule(c,36,56,559);label(c,36,82,'Финансовый отчёт ИП',22,True)
        label(c,36,103,r['borrower_name'],11,True,width=350)
        label(c,559,103,date_label(r['assessment_date']),9.5,right=True)
        rect(c,36,114,523,44,PALE);rect(c,36,114,3,44,BLUE)
        box(r['banner'],48,121,499,30,10.2,True)
        loan=r['loan'];terms=[]
        if loan['rate_pct']['value'] is not None:terms.append(str(loan['rate_pct']['value']).replace('.',',')+'% годовых')
        if loan['term_months']['value'] is not None:terms.append(str(int(loan['term_months']['value']))+' мес.')
        unknown=[]
        if loan['rate_pct']['value'] is None:unknown.append('ставка')
        if loan['term_months']['value'] is None:unknown.append('срок')
        if unknown:terms.append(' и '.join(unknown)+' не установлены' if len(unknown)>1 else unknown[0]+' не установлен'+('а' if unknown[0]=='ставка' else ''))
        box('Новый заём '+amount(loan['amount']['value'])+', '+', '.join(terms)+'.',36,171,523,23,10,True)
        scope=r['bank_scope'];p=r['period'];n=len(scope['accounts'])
        label(c,36,205,date_label(p['start'])+' - '+date_label(p['end'])+' / счетов: '+str(n)+' / операций: '+str(scope['operations_count']),8.7,width=523)
        for i,(title,value) in enumerate([('Остаток в начале',r['bank']['opening']['value']),('Изменение остатка',r['calculations']['cash_change']),('Остаток в конце',r['bank']['closing']['value'])]):
            x=36+177*i;rect(c,x,216,169,57,PALE)
            label(c,x+10,233,title,9,True,width=150);label(c,x+10,257,fmt(value),18,True,width=150)
        label(c,559,285,'млн ₽, по представленным счетам',7.8,right=True)
        label(c,36,303,'Денежные потоки',12.5,True)
        label(c,559,303,'млн ₽',8,right=True)
        for x,key,title in [(36,'inflows','Зачисления'),(310,'outflows','Списания')]:
            rect(c,x,312,249,19,PALE);label(c,x+8,325,title,9,True)
            if not r['calculations'][key]:label(c,x+8,346,'Разбивка не установлена',8.7)
            for i,row in enumerate(r['calculations'][key]):
                top=346+18*i
                label(c,x+8,top,row['label'],8.7,width=173)
                label(c,x+241,top,fmt(row['fact']['value']),9.3,True,True,width=62)
                rule(c,x,top+5,x+249)
            label(c,x+8,441,'Итого без встречных переводов',8.2,True,width=180)
            label(c,x+241,441,fmt(r['calculations'][key+'_total']),10,True,True,width=62)
        box('\n'.join(x['text'] for x in r['flow_notes']),36,451,523,24,8.2)
        draw_monthly_receipts(c,r,490,66)
        label(c,36,632,'Долговая нагрузка',12.5,True)
        d=r['debt'];label(c,36,646,'ОКБ: последние записи '+date_label(d['period']['start'])+' - '+date_label(d['period']['end']),7.8,width=523)
        label(c,315,646,'млн ₽',7.8,right=True)
        for i,(key,title) in enumerate([('active_contracts','Прямых договоров'),('principal','Известный основной долг'),('total','Общая задолженность'),('overdue','Просрочка в активной сводке')]):
            top=658+15*i
            label(c,44,top,title,8.7,width=186)
            value=d[key]['value'];display=str(int(value)) if key=='active_contracts' and value is not None else fmt(value)
            label(c,315,top,display,10.5,True,True,width=75);rule(c,36,top+5,323)
        label(c,342,656,'Платёжный ориентир, тыс. ₽',8.5,True,width=210)
        label(c,550,676,fmt(d['payment_indicator']['value'],1000),15,True,True,width=203)
        label(c,342,692,'Последние значения; не точный график',7.3,width=210)
        large=d['large_payment']
        if large:
            label(c,342,711,'Крупный платёж '+date_label(large['date']),8.5,True,width=210)
            label(c,550,729,fmt(large['total']['value'])+' млн ₽',14,True,True,width=203)
        box('\n'.join(x['text'] for x in d['notes']),36,736,523,22,8)
        label(c,36,770,'Вывод по имеющимся данным',12.5,True)
        box('\n'.join(x['text'] for x in r['findings']+r['limitations']),36,778,523,43,9)
        label(c,36,832,'Охват счетов неполный. Остаток на счетах не равен свободным деньгам. Суммы округлены.' if scope['incomplete'] else 'Банковский поток не подтверждает прибыль и полный свободный поток бизнеса.',7.4,width=523)
        c.showPage();c.save();check_file(temporary);os.replace(temporary,target)
    except Exception:
        Path(temporary).unlink(missing_ok=True);raise
    return r


def render21(r, destination, font_dir=None):
    """Investor form with activity, expense detail and explained payment scope."""
    register_fonts(font_dir)
    target=Path(destination);target.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(suffix='.pdf',dir=str(target.parent));os.close(fd)
    def box(text,x,top,width,height,size=9,bold=False):
        text_box(c,text,x,PAPER_H-top-height,width,height,size,bold=bold)
    try:
        c=Canvas(temporary,pagesize=(PAPER_W,PAPER_H),invariant=1,pageCompression=1)
        c.setTitle('Финансовый отчёт ИП / '+r['borrower_name']);c.setAuthor('ВБетон');c.setSubject(r['layout_version'])
        rect(c,0,0,PAPER_W,PAPER_H,'#FFFFFF');draw_logo(c)
        label(c,559,33,'ОБРАЗЕЦ / УСЛОВНЫЕ ДАННЫЕ' if r['fictional'] else 'ФИНАНСОВЫЙ АНАЛИЗ',8,True,True)
        rule(c,36,56,559);label(c,36,82,'Финансовый отчёт ИП',22,True)
        label(c,36,103,r['borrower_name'],11,True,width=350);label(c,559,103,date_label(r['assessment_date']),9.5,right=True)
        rect(c,36,114,523,31,PALE);rect(c,36,114,3,31,BLUE);box(r['banner'],48,118,499,27,10.2,True)
        box('Бизнес: '+r['business']['text']+'. Цель займа: '+r['purpose']['text']+'.',36,153,523,23,8.8)
        loan=r['loan'];terms=[]
        if loan['rate_pct']['value'] is not None:terms.append(str(loan['rate_pct']['value']).replace('.',',')+'% годовых')
        if loan['term_months']['value'] is not None:terms.append(str(int(loan['term_months']['value']))+' мес.')
        if loan['rate_pct']['value'] is None:terms.append('ставка не установлена')
        if loan['term_months']['value'] is None:terms.append('срок не установлен')
        box('Новый заём '+amount(loan['amount']['value'])+', '+', '.join(terms)+'.',36,181,523,23,9.8,True)
        scope=r['bank_scope'];p=r['period'];d=r['debt'];n=len(scope['accounts'])
        label(c,36,212,date_label(p['start'])+' - '+date_label(p['end'])+' / счетов: '+str(n)+' / операций: '+str(scope['operations_count']),8.3,width=523)
        customer=next((row['fact']['value'] for row in r['calculations']['inflows'] if row['id']=='customer'),None)
        for i,(title,value) in enumerate([('Клиентские поступления',customer),('Основной долг по ОКБ',d['principal']['value']),('Деньги на '+date_label(p['end']),r['bank']['closing']['value'])]):
            x=36+177*i;rect(c,x,221,169,54,PALE)
            label(c,x+10,237,title,8.6,True,width=150);label(c,x+10,260,fmt(value),17,True,width=150)
        label(c,559,285,'млн ₽; деньги по представленным счетам, долг на даты ОКБ',7.5,right=True,width=523)
        control='На этих счетах: начало '+fmt(r['bank']['opening']['value'])+'; изменение '+fmt(r['calculations']['cash_change'])+' млн ₽. '
        control+=('Охват неполный. ' if scope['incomplete'] else '')+'Это не оценка прибыли.'
        box(control,36,292,523,20,7.7)
        label(c,36,324,'Денежные потоки',12.5,True);label(c,559,324,'млн ₽',8,right=True)
        for x,key,title in [(36,'inflows','Зачисления'),(310,'outflows','Списания')]:
            rect(c,x,334,249,18,PALE);label(c,x+8,347,title,9,True)
            if not r['calculations'][key]:label(c,x+8,367,'Разбивка не установлена',8.5)
            for i,row in enumerate(r['calculations'][key]):
                top=362+15*i;label(c,x+8,top,row['label'],8.5,width=173)
                label(c,x+241,top,fmt(row['fact']['value']),9.3,True,True,width=62);rule(c,x,top+4,x+249)
            label(c,x+8,438,'Итого без встречных переводов',8.2,True,width=180)
            label(c,x+241,438,fmt(r['calculations'][key+'_total']),10,True,True,width=62)
        expense='Из прочих списаний: '+('; '.join(row['label']+' '+fmt(row['fact']['value']) for row in r['expense_breakdown']) if r['expense_breakdown'] else 'разбивка не установлена')+'.'
        box(expense,36,446,523,22,8)
        box('\n'.join(row['text'] for row in r['flow_notes']),36,471,523,22,7.8)
        draw_monthly_receipts(c,r,505,54)
        label(c,36,632,'Долговая нагрузка',12.5,True)
        label(c,36,646,'ОКБ: последние записи '+date_label(d['period']['start'])+' - '+date_label(d['period']['end']),7.6,width=523);label(c,315,646,'млн ₽',7.6,right=True)
        for i,(key,title) in enumerate([('active_contracts','Прямых договоров'),('principal','Известный основной долг'),('total','Общая задолженность'),('overdue','Просрочка в активной сводке')]):
            top=658+15*i;label(c,44,top,title,8.5,width=186)
            value=d[key]['value'];display=str(int(value)) if key=='active_contracts' and value is not None else fmt(value)
            label(c,315,top,display,10.5,True,True,width=75);rule(c,36,top+4,323)
        label(c,342,647,'Последние суммы платежей, тыс. ₽',8.5,True,width=210)
        label(c,550,667,fmt(d['payment_indicator']['value'],1000),15,True,True,width=203)
        ps=d['payment_scope'];freq={'monthly':'месячные ориентиры','mixed':'разная периодичность','unknown':'периодичность не установлена'}[ps['frequency']]
        if ps['frequency']=='mixed' and 'monthly_contracts' in ps:freq=str(ps['monthly_contracts'])+' месячных, '+str(ps['contracts']-ps['monthly_contracts'])+' «иное»'
        pay_note='По '+str(ps['contracts'])+' договорам: '+freq+'.'
        if d['large_payment']:
            inc=ps.get('large_payment_included')
            pay_note+='\n'+('Крупный платёж отдельно; не точный график.' if inc is False else 'Крупный платёж включён; не точный график.' if inc is True else 'Включение крупного платежа не установлено.')
        pay_note+='\nНе расчёт месячной нагрузки.'
        box(pay_note,342,673,210,29,7.3)
        if d['large_payment']:
            large=d['large_payment'];label(c,342,711,'Крупный платёж '+date_label(large['date']),8.5,True,width=210)
            label(c,550,729,fmt(large['total']['value'])+' млн ₽',13.5,True,True,width=203)
        box('\n'.join(row['text'] for row in d['notes']),36,736,523,20,7.7)
        label(c,36,770,'Вывод по имеющимся данным',12,True)
        box('\n'.join(row['text'] for row in r['findings']+r['limitations']),36,778,523,40,8.8)
        label(c,36,832,'Остаток на счетах не равен свободным деньгам. Суммы округлены.' if scope['incomplete'] else 'Банковский поток не подтверждает полный свободный поток бизнеса. Суммы округлены.',7,width=523)
        c.showPage();c.save();check_file(temporary);os.replace(temporary,target)
    except Exception:
        Path(temporary).unlink(missing_ok=True);raise
    return r
