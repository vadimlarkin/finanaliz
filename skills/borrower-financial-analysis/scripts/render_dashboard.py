#!/usr/bin/env python3
"""Render the approved one-page investor report from evidence-linked input, offline."""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import os
import sys
import tempfile
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph
from analyze import ACCEPTED, InputError, analyze, load_input
from report_data import FLOW_ROWS, STOCK_ROWS, CHART_ROWS
from reportlab.pdfbase.pdfmetrics import stringWidth
from datetime import date
from decimal import Decimal
from check_terminology import check_file, check_text

LAYOUT_VERSION = 'investor-one-page-2.2'
PAPER_W, PAPER_H = 595.28, 841.89
INK, MUTED, BLUE = '#23282D', '#23282D', '#0072EF'
RULE, PALE, HIGHLIGHT = '#E9F5FF', '#E9F5FF', '#E9F5FF'
ASSETS = Path(__file__).resolve().parents[1]/'assets'
LOGO_HASH = '33d4ebd4c516e49244922dd5c345d4ff163033693cc345c215f6ea501bcd043e'
LOGO_W, LOGO_H = 585.0041, 120.317
# 35.3 x 7.3 mm, above the guidebook's 24 x 5 mm minimum.
LOGO_X, LOGO_TOP, LOGO_WIDTH = 36, 23, 100
# Exact hashes of redistributable font assets; never select an OS fallback.
FONT_FILES = {'Body': 'RobotoFlex-Text.ttf', 'BodyBold': 'RobotoFlex-Heading.ttf'}
FONT_HASHES = {'RobotoFlex-Text.ttf': '1ea45867601ead14da44818408a8d6af5c28e643b345258ede8e3b2873bd21f8',
               'RobotoFlex-Heading.ttf': '751b033f8cc4fae744b18675dc777281a10a440fbda2711b0187cf3f6bb5a8da'}

class LayoutError(ValueError):
    pass


def register_fonts(font_dir=None):
    folder = Path(font_dir) if font_dir else ASSETS/'fonts'
    for name, file in FONT_FILES.items():
        path = folder/file
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != FONT_HASHES[file]:
            raise LayoutError(f'Нужен неизменённый шрифт макета {LAYOUT_VERSION}: {file}')
        pdfmetrics.registerFont(TTFont(name,str(path)))


def draw_logo(c):
    path = ASSETS/'brand/vbeton-logo.pdfpath'
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != LOGO_HASH:
        raise LayoutError('Нужен неизменённый векторный логотип ВБетон из гайдбука')
    c.beginForm('VBetonLogo', 0, 0, LOGO_W, LOGO_H)
    c.addLiteral(path.read_text(encoding='ascii'))
    c.endForm()
    scale = LOGO_WIDTH/LOGO_W
    c.saveState()
    c.translate(LOGO_X, PAPER_H-LOGO_TOP-LOGO_H*scale)
    c.scale(scale, scale)
    c.doForm('VBetonLogo')
    c.restoreState()


def text_box(c, text, x, y, width, height, size=9, color=INK, bold=False, center=False):
    """Existing helper interface: y is measured from the PDF bottom."""
    check_text(str(text))
    style=ParagraphStyle('fixed',fontName='BodyBold' if bold else 'Body',fontSize=size,
                         leading=size*1.28,textColor=colors.HexColor(color),alignment=TA_LEFT,
                         splitLongWords=True)
    content=html.escape(str(text)).replace('\n','<br/>')
    p=Paragraph(content,style)
    _,actual=p.wrap(width,height)
    if actual>height+.1:
        raise LayoutError(f'Поле фиксированного макета переполнено ({actual:.1f}>{height:.1f} pt): {str(text)[:80]}')
    p.drawOn(c,x,y+height-actual)


def rect(c,x,top,w,h,fill):
    c.setFillColor(colors.HexColor(fill));c.rect(x,PAPER_H-top-h,w,h,fill=1,stroke=0)


def rule(c,x,top,x2,color=RULE,width=.6):
    c.setStrokeColor(colors.HexColor(color));c.setLineWidth(width)
    c.line(x,PAPER_H-top,x2,PAPER_H-top)


def loan_fact(data,key):
    fact=data['facts'].get(key,{})
    if fact.get('status') not in ACCEPTED or fact.get('value') is None:
        return None,[]
    return fact['value'],fact.get('sources',[])


def compact(value):
    return f'{value:g}'.replace('.',',')


def amount(value):
    if value is None:return 'Нет данных'
    if abs(value)>=1000000:return compact(value/1000000)+' млн ₽'
    if abs(value)>=1000:return compact(value/1000)+' тыс. ₽'
    return compact(value)+' ₽'


def label(c, x, top, text, size=9.5, bold=False, right=False, width=None):
    text = str(text)
    check_text(text)
    font = 'BodyBold' if bold else 'Body'
    length = stringWidth(text, font, size)
    if width is not None and length > width+.1:
        raise LayoutError(f'Поле фиксированного макета переполнено: {text[:80]}')
    if right:
        x -= length
    if x < 35.9 or x+length > 559.1:
        raise LayoutError(f'Текст выходит за поля A4: {text[:80]}')
    c.setFillColor(colors.HexColor(INK))
    c.setFont(font, size)
    c.drawString(x, PAPER_H-top, text)


def date_label(value):
    return date.fromisoformat(value).strftime('%d.%m.%Y') if value else 'Дата не задана'


def million(fact):
    if fact.get('value') is None:
        return 'Противоречие' if fact.get('status') == 'conflicting' else 'Нет данных'
    scaled = Decimal(str(fact['value']))/Decimal('1000000')
    value = format(scaled, 'f').rstrip('0') if scaled and abs(scaled)<Decimal('0.05') else f'{scaled:,.1f}'
    value = value.replace(',', ' ').replace('.', ',')
    return value+('*' if fact.get('reconstructed') else '')


def period_label(period, year):
    if not period:
        return str(year)
    start, end = (date.fromisoformat(period[k]) for k in ('start', 'end'))
    if start.month == 1 and start.day == 1 and end.month == 12 and end.day == 31:
        return f'{year} год'
    if start.month == 1 and start.day == 1:
        import calendar
        if end.day == calendar.monthrange(end.year, end.month)[1]:
            return f'{end.month} мес. {year}'
    return f'по {end:%d.%m.%Y}'


def render(data,destination,font_dir=None):
    result=analyze(data)
    result['layout_version']=LAYOUT_VERSION
    register_fonts(font_dir)
    target=Path(destination);target.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(suffix='.pdf',dir=str(target.parent));os.close(fd)
    try:
        c=Canvas(temporary,pagesize=(PAPER_W,PAPER_H),invariant=1,pageCompression=1)
        c.setTitle('Финансовый отчёт / '+result['company']['name'])
        c.setAuthor('ВБетон')
        c.setSubject('Fixed layout '+LAYOUT_VERSION)
        rect(c,0,0,PAPER_W,PAPER_H,'#FFFFFF')
        draw_logo(c)
        fictional=bool(result['company'].get('fictional'))
        label(c,559,33,'ОБРАЗЕЦ / УСЛОВНЫЕ ДАННЫЕ' if fictional else 'ФИНАНСОВЫЙ АНАЛИЗ',8,True,True)
        rule(c,36,56,559)
        label(c,36,81,'Финансовый отчёт',22,True)
        label(c,36,101,result['company']['name'],11,True,width=350)
        label(c,559,101,'Дата анализа: '+date_label(result['assessment_date']),9.5,right=True)
        rect(c,36,114,523,34,PALE);rect(c,36,114,3,34,BLUE)
        text_box(c,result['conclusion'],48,PAPER_H-144,499,26,10.2,bold=True)
        for x,key,title in [(36,'new_loan_amount','СУММА ЗАЙМА'),(209,'new_loan_rate_pct','СТАВКА ЗАЁМЩИКА'),(389,'new_loan_term_months','СРОК И ВОЗВРАТ')]:
            value,_=loan_fact(data,key)
            if value is None:
                display='Нет данных'
            elif key=='new_loan_amount':
                display=amount(value)
            elif key=='new_loan_rate_pct':
                display=compact(value)+'% годовых'
            else:
                suffix={'bullet':' / основной долг в конце','schedule':' / по графику'}.get(data.get('loan_repayment'),' / порядок не задан')
                display=compact(value)+' мес.'+suffix
            label(c,x,165,title,7.7,True)
            label(c,x,184,display,9.5 if x==389 else 12,True,width=170)
        label(c,36,211,'Финансовое состояние',12.5,True)
        label(c,559,211,'млн ₽',9,right=True)
        history=result['report']['financial_history']
        rect(c,36,222,523,20,PALE)
        label(c,44,236,'Результаты',9,True)
        for x,entry in zip((347,447,550),history):
            star='*' if any(entry['facts'][k]['reconstructed'] for k,_ in FLOW_ROWS) else ''
            label(c,x,236,period_label(entry['period'],entry['year'])+star,9,True,True,88)
        for i,(key,title) in enumerate(FLOW_ROWS):
            top=257+i*16
            label(c,44,top,title,9.8)
            for x,entry in zip((347,447,550),history):
                fact=entry['facts'][key]
                label(c,x,top,million(fact),9.5 if fact['value'] is not None else 8.5,right=True,width=88)
            rule(c,36,top+5,559)
        rect(c,36,302,523,20,PALE)
        label(c,44,316,'Активы и обязательства',9,True)
        for x,entry in zip((347,447,550),history):
            star='*' if any(entry['facts'][k]['reconstructed'] for k,_ in STOCK_ROWS) else ''
            label(c,x,316,date_label(entry['balance_date'])+star,9,True,True,88)
        for i,(key,title) in enumerate(STOCK_ROWS):
            top=337+i*16
            if key=='equity':rect(c,36,top-11,523,16,PALE)
            label(c,44,top,title,9.8,key=='equity',width=273)
            for x,entry in zip((347,447,550),history):
                fact=entry['facts'][key]
                label(c,x,top,million(fact),9.5 if fact['value'] is not None else 8.5,key=='equity',True,88)
            rule(c,36,top+5,559)
        chart=result['report']['chart']
        label(c,36,526,'Поступления и погашения',11.5,True)
        period=chart['period']
        caption=(date_label(period['start'])+'–'+date_label(period['end'])+' / млн ₽') if period else 'Период не задан'
        label(c,36,541,caption,7.8,width=252)
        ready=chart['bank_data_complete'] and all(chart['facts'][k]['value'] is not None for k,_ in CHART_ROWS)
        values=[chart['facts'][k]['value']/1000000 for k,_ in CHART_ROWS] if ready else []
        low=min([0]+values);high=max([0]+values)
        span=high-low or 1
        plot_x,plot_width=124,120
        zero=plot_x+plot_width*(-low)/span
        for i,(key,title) in enumerate(CHART_ROWS):
            top=556+18*i
            label(c,44,top+8,title,9)
            if ready:
                value=chart['facts'][key]['value']/1000000
                rect(c,plot_x,top-1,plot_width,12,PALE)
                endpoint=plot_x+plot_width*(value-low)/span
                rect(c,min(zero,endpoint),top-1,abs(endpoint-zero),12,BLUE)
                if low<0:
                    c.setStrokeColor(colors.HexColor(INK));c.setLineWidth(.5)
                    c.line(zero,PAPER_H-top+1,zero,PAPER_H-top-11)
                label(c,288,top+8,million(chart['facts'][key]),9,True,True,40)
            else:
                label(c,288,top+8,'Неполные данные' if period and not chart['bank_data_complete'] else 'Нет данных',8,right=True,width=158)
        debt=result['report']['current_debt']
        label(c,310,526,'Кредитная нагрузка**',11.5,True)
        label(c,310,541,('на '+date_label(debt['as_of']) if debt['as_of'] else 'Дата не задана')+' / основной долг, млн ₽',7.8,width=249)
        for i,(key,title) in enumerate([
            ('bank_principal','Банковские'),
            ('unrelated_nonbank_principal','Небанковские, без связанных'),
            ('related_party_principal','Связанные'),
        ]):
            top=566+23*i
            label(c,318,top,title,9.2,width=174)
            fact=debt['facts'][key]
            label(c,550,top,million(fact),12 if fact['value'] is not None else 8.5,True,True,58)
            rule(c,310,top+7,559)
        label(c,36,656,'Влияние нового займа',12.5,True)
        rect(c,36,666,523,20,PALE)
        label(c,44,680,'Соотношение',8.5,True)
        label(c,431,680,'До займа',8.5,True,True)
        label(c,550,680,'После займа',8.5,True,True)
        rect(c,446,686,113,88,PALE)
        for i,row in enumerate(result['loan_comparison']['rows']):
            top=710+i*40
            label(c,44,top,row['title'],10,width=300)
            stamp=(date_label(row['as_of']) if row['id']=='leverage' else
                   'Последние 12 мес. / '+date_label(row['period']['start'])+'–'+date_label(row['period']['end'])
                   if row['period'] else 'Последние 12 мес. / период не задан')
            reasons=list(dict.fromkeys(cell['reason'] for cell in (row['before'],row['after']) if cell['value'] is None))
            note=stamp+(' / '+('; '.join(reasons)) if reasons else '')
            text_box(c,note,44,PAPER_H-top-19,300,17,7.3)
            for x,cell in [(431,row['before']),(550,row['after'])]:
                label(c,x,top+5,cell['display'],14 if cell['value'] is not None else 8.5,True,True,105)
            rule(c,36,top+24,559)
        if any(f['reconstructed'] for e in history for f in e['facts'].values()):
            label(c,36,794,'* Данные текущего периода восстановлены из ОСВ.',7.5,width=523)
        label(c,36,806,'** Основной долг на указанную дату, без учёта рассматриваемого нового займа.',7.5,width=523)
        if fictional:
            label(c,36,819,'Все данные вымышлены. Образец предназначен для проверки состава и читаемости.',7.5,width=523)
        c.showPage();c.save()
        check_file(temporary)
        os.replace(temporary,target)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return result


def same_file(a, b):
    return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))


def write_audit(path, result):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    content=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    fd,temporary=tempfile.mkstemp(suffix='.json',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(content)
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_json',help='Исходные факты, не редактируемый результат расчёта')
    parser.add_argument('output_pdf')
    parser.add_argument('--font-dir',help='Каталог с точными копиями шрифтов зафиксированного макета')
    parser.add_argument('--audit-json')
    args=parser.parse_args()
    try:
        source,output=Path(args.input_json).resolve(),Path(args.output_pdf).resolve()
        paths=[source,output]+([Path(args.audit_json).resolve()] if args.audit_json else [])
        if any(same_file(a,b) for i,a in enumerate(paths) for b in paths[i+1:]):
            raise InputError('Пути исходника, PDF и аудита должны различаться')
        result=render(load_input(source),output,args.font_dir)
        if args.audit_json:
            write_audit(args.audit_json,result)
    except (InputError,LayoutError,ValueError,OSError,KeyError,TypeError) as exc:
        print(f'Ошибка: {exc}',file=sys.stderr);return 2
    print(str(output));return 0

if __name__=='__main__':raise SystemExit(main())
