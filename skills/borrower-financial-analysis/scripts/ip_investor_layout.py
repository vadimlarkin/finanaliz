"""Approved compact IP layout, including a loan-specific payment scenario."""
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from render_dashboard import (PAPER_W,PAPER_H,PALE,BLUE,register_fonts,draw_logo,
                              rect,rule,label,text_box,date_label,amount)
from ip_cashflow_report import fmt,draw_monthly_receipts
from check_terminology import check_file


def render_prepared(r, destination, font_dir=None):
    register_fonts(font_dir)
    target=Path(destination);target.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(suffix='.pdf',dir=str(target.parent));os.close(fd)
    def box(text,x,top,width,height,size=9,bold=False):
        text_box(c,text,x,PAPER_H-top-height,width,height,size,bold=bold)
    def val(fact):return fact['value']
    def money(value):return ('0' if value==0 else fmt(value))+' млн ₽' if value is not None else 'Нет данных'
    def percent(value):return 'н/д' if value is None else f'{value:g}'.replace('.',',')+'%'
    def month_word(count):
        return 'месяц' if count%10==1 and count%100!=11 else 'месяца' if count%10 in (2,3,4) and count%100 not in (12,13,14) else 'месяцев'
    def centered(text,left,top,width,size=8,bold=False):
        length=stringWidth(text,'BodyBold' if bold else 'Body',size)
        label(c,left+(width-length)/2,top,text,size,bold,width=width-2)
    try:
        c=Canvas(temporary,pagesize=(PAPER_W,PAPER_H),invariant=1,pageCompression=1)
        c.setTitle('Анализ финансового состояния / '+r['borrower_name']);c.setAuthor('ВБетон');c.setSubject(r['layout_version'])
        rect(c,0,0,PAPER_W,PAPER_H,'#FFFFFF');draw_logo(c)
        label(c,559,33,('ОБРАЗЕЦ / УСЛОВНЫЕ ДАННЫЕ' if r['fictional'] else 'ФИНАНСОВЫЙ АНАЛИЗ')+' / '+date_label(r['assessment_date']),7.5,True,True,width=350)
        rule(c,36,56,559);label(c,36,79,'Анализ финансового состояния',22,True)
        label(c,36,100,r['borrower_name']+', ИНН '+r['inn'],10.5,True,width=523)
        loan=r['loan'];term=val(loan['term_months'])
        rect(c,36,111,523,35,PALE);rect(c,36,111,3,35,BLUE)
        main='Новый заём '+amount(val(loan['principal']))+'  /  Инвесторам '+percent(val(loan['investor_rate_pct']))+' годовых  /  '+('срок н/д' if term is None else str(int(term))+' '+month_word(int(term)))
        label(c,48,126,main.replace('.',','),12,True,width=499)
        repayment={'end':'Проценты и основной долг в конце срока.','schedule':'Погашение по согласованному графику.','unknown':'Порядок погашения не установлен.'}[loan['repayment']]
        label(c,48,140,'Эффективная ставка '+percent(val(loan['effective_investor_rate_pct']))+'. '+repayment,8.3,width=499)
        project=r['project']
        box(project['description']['text']+'. На строительство '+amount(val(loan['net_proceeds']))+', комиссия '+amount(val(loan['commission']))+'.',36,154,523,24,8.6)
        rect(c,36,184,523,34,PALE)
        label(c,44,197,'Эскроу заказчика / '+project['escrow_bank'],8.2,True,width=245)
        escrow='н/д' if val(project['escrow']) is None else f'{val(project["escrow"]):,.0f}'.replace(',',' ')+' ₽'
        label(c,44,212,escrow,11.5,True,width=245)
        box(project['security']['text'],310,191,241,25,8.6)
        scope=r['bank_scope'];p=r['period']
        label(c,36,233,f'Проанализировано {scope["operations_count"]} операций по {len(scope["accounts"])} представленным счетам за период с '+date_label(p['start'])+' по '+date_label(p['end']),8.3,width=523)
        cards=[('Выручка от основного вида деятельности',r['flows']['revenue']),('Подтверждённый долг по БКИ*',r['bki']['total']),('Просроченный долг по БКИ*',r['bki']['overdue'])]
        for i,(title,fact) in enumerate(cards):
            x=36+177*i;rect(c,x,244,169,51,PALE);box(title,x+10,251,149,23,8.7,True)
            label(c,x+10,286,money(val(fact)),17,True,width=149)
        label(c,36,316,'Денежные потоки',12,True)
        sides=[(36,'Поступления',[('revenue','Выручка от основного вида деятельности'),('received_loans','Полученные кредиты и займы'),('own_in','Собственные средства и переводы'),('other_in','Прочие поступления')]),
               (310,'Списания',[('debt_payments','Выплаты по кредитам и займам**'),('payroll','Заработная плата'),('suppliers','Поставщики'),('taxes','Налоги'),('own_out','Переводы на свои счета'),('other_out','Прочие списания')])]
        for x,title,rows in sides:
            rect(c,x,324,249,17,PALE);label(c,x+8,336,title,9,True)
            for i,(key,title) in enumerate(rows):
                top=354+14*i;label(c,x+8,top,title,7.4,width=157)
                label(c,x+241,top,money(val(r['flows'][key])),9.3,True,True,width=82);rule(c,x,top+4,x+249)
            label(c,x+8,444,'Итого',9,True)
            label(c,x+241,444,money(r['calculations']['inflows_total' if x==36 else 'outflows_total']),10.3,True,True,width=100)
        misc=r['misc_breakdown']
        box('Прочие списания: Комиссии и обслуживание '+fmt(val(misc['fees_services']))+'; Карта '+fmt(val(misc['card']))+'; Штрафы и пени '+fmt(val(misc['penalties']))+' млн ₽.',36,454,523,18,7.7)
        draw_monthly_receipts(c,r,485,22,title='Выручка от основного вида деятельности по месяцам')
        label(c,36,582,'Фактические выплаты по кредитам и займам за период',10.5,True)
        pay=r['debt_payments']
        for y,key,title in [(601,'bank','Банковские кредиторы'),(619,'nonbank','Небанковские кредиторы')]:
            label(c,44,y,title,8.6,width=140);label(c,283,y,money(val(pay[key])),10.5,True,True,width=100);rule(c,36,y+4,291)
        if val(pay['own_platform']) is not None and val(pay['own_platform'])>0:
            rect(c,310,591,249,37,PALE);rect(c,310,591,3,37,BLUE)
            label(c,320,603,'История работы с '+pay['platform_name'],9.1,True,width=229)
            number=int(val(pay['platform_contracts']))
            caption='По '+str(number)+(' договору' if number%10==1 and number%100!=11 else ' договорам')+' получено выплат'
            label(c,320,616,caption,7,width=128)
            label(c,550,616,money(val(pay['own_platform'])),11.7,True,True,width=100)
            label(c,320,625,'Включено в '+fmt(val(pay['nonbank']))+' млн ₽ небанковских выплат',6.8,width=229)
        load=r['credit_load']
        label(c,36,648,load['heading'],12,True,width=523)
        label(c,36,661,load['caption'],7.8,width=523)
        regular=load['regular_monthly_rub'];count=load['regular_contracts'];months=load['period_months']
        stats=[('Регулярно по '+str(count)+(' договору' if count%10==1 and count%100!=11 else ' договорам') if count else 'Регулярные платежи',
                f'{Decimal(regular):,.0f}'.replace(',',' ')+' ₽/мес.' if regular is not None else 'Нет данных'),
               ('Всего за '+str(months)+' '+('месяц' if months%10==1 and months%100!=11 else 'месяца' if months%10 in (2,3,4) and months%100 not in (12,13,14) else 'месяцев') if months else 'Всего за период',
                money(Decimal(load['total_rub'])) if load['total_rub'] is not None else 'Нет данных')]
        for i,(title,display) in enumerate(stats):
            x=36+265.5*i;rect(c,x,670,257.5,37,PALE)
            label(c,x+8,682,title,7.8,True,width=241.5);label(c,x+8,700,display,14.5,True,width=241.5)
        calendar=load['calendar'];bucket=load['calendar_bucket_months']
        heading='Календарный план погашения'+('' if bucket==1 else ' / периоды по '+str(bucket)+' мес.')
        label(c,36,720,heading,9.2,True,width=480);label(c,559,720,'млн ₽',7,right=True)
        if calendar:
            step=523/len(calendar)
            for i,entry in enumerate(calendar):
                x=36+i*step;start=entry['start_month'];end=entry['end_month']
                display=start[5:]+'.'+start[2:4]
                if end!=start:display+='-'+end[5:]+'.'+end[2:4]
                rect(c,x,729,step,16,PALE);centered(display,x,740,step,7.7,True)
                value=entry['included_rub']
                centered('н/д' if value is None else fmt(Decimal(value)),x,759,step,9,True)
        else:
            label(c,36,750,'Нет данных',9)
        label(c,36,778,'Вывод по имеющимся данным',10.3,True)
        box(' '.join(n['text'] for n in r['conclusions']),36,785,523,23,8.1)
        revenue_note='Выручка: денежные оплаты, включая авансы'
        refund=val(r['customer_refunds'])
        if refund is not None and refund>0:
            revenue_note+=', за вычетом подтверждённых возвратов заказчикам ('+f'{refund:,.2f}'.replace(',',' ').replace('.',',')+' ₽)'
        label(c,36,813,revenue_note+'.',7,width=523)
        bureau='Использован отчёт от '+date_label(r['bki']['report_date'])+'.' if r['bki']['report_date'] else 'Отчёт не предоставлен.'
        label(c,36,823,'* БКИ - Бюро кредитных историй. '+bureau,7,width=523)
        label(c,36,833,'** Выплаты по займам не разделены на основной долг и проценты.',7,width=523)
        c.showPage();c.save();check_file(temporary);os.replace(temporary,target)
    except Exception:
        Path(temporary).unlink(missing_ok=True);raise
    return r
