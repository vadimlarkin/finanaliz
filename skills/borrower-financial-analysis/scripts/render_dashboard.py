#!/usr/bin/env python3
"""Render the fixed credit-committee A4 template from validated input, offline."""
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

LAYOUT_VERSION = 'credit-committee-1.0'
PAPER_W, PAPER_H = 595.28, 841.89
INK, MUTED, BLUE = '#15283F', '#66767A', '#204DC8'
RULE, PALE, HIGHLIGHT = '#D8DFDD', '#F1F3F5', '#ECF0FB'
# The only design authority: coordinates are points measured from the top left.
# x, top, width, height, font size, bold. Fields never change size or position.
SLOTS = {
 'brand': (36,23,260,12,7.5,True),
 'kind': (310,23,249,12,7.2,False),
 'company': (36,68,523,18,9,True),
 'title': (36,91,523,40,27,True),
 'summary': (51,156,493,39,14,True),
 'coverage': (51,202,493,13,8,False),
 'loan_amount': (36,245,162,34,25,False),
 'loan_rate': (216,245,162,34,25,False),
 'loan_term': (396,245,163,34,25,False),
 'loan_amount_label': (36,282,170,22,7,True),
 'loan_rate_label': (216,282,170,22,7,True),
 'loan_term_label': (396,282,163,22,7,True),
 'head_title': (36,332,240,13,7,True),
 'head_value': (292,332,100,13,7,True),
 'head_note': (412,332,147,13,7,True),
 'stress_title': (36,599,365,19,10,True),
 'stress_value': (436,588,123,37,27,False),
 'stress_note': (36,623,523,26,8,False),
 'action_title': (36,669,523,14,8,True),
 'actions': (36,690,523,58,8.5,False),
 'sources': (36,765,523,22,6.3,False),
 'limitations': (36,790,523,27,6.3,False),
 'footer': (36,815,523,14,6.6,True),
}
# Exact hashes of redistributable font assets; never select an OS fallback.
FONT_FILES = {'Body': 'LiberationSans-Regular.ttf', 'BodyBold': 'LiberationSans-Bold.ttf',
              'BodySymbol': 'DejaVuSans.ttf'}
FONT_HASHES = {'LiberationSans-Regular.ttf': 'f8ace1f892b2bd9dc1792ba7f097fa7588f84fed48321480e04de5390828221f', 'LiberationSans-Bold.ttf': '361c61b82d575c5c35fd9157fda8b0194bcfcd0d88ea8521a4fb5dd53d33dddc', 'DejaVuSans.ttf': '7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954'}

class LayoutError(ValueError):
    pass


def register_fonts(font_dir=None):
    folder = Path(font_dir) if font_dir else Path(__file__).resolve().parents[1]/'assets/fonts'
    for name, file in FONT_FILES.items():
        path = folder/file
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != FONT_HASHES[file]:
            raise LayoutError(f'Нужен неизменённый шрифт макета {LAYOUT_VERSION}: {file}')
        pdfmetrics.registerFont(TTFont(name,str(path)))


def text_box(c, text, x, y, width, height, size=9, color=INK, bold=False, center=False):
    """Existing helper interface: y is measured from the PDF bottom."""
    style=ParagraphStyle('fixed',fontName='BodyBold' if bold else 'Body',fontSize=size,
                         leading=size*1.28,textColor=colors.HexColor(color),alignment=TA_LEFT,
                         splitLongWords=True)
    content=html.escape(str(text)).replace('\n','<br/>').replace('₽','<font name="BodySymbol">₽</font>')
    p=Paragraph(content,style)
    _,actual=p.wrap(width,height)
    if actual>height+.1:
        raise LayoutError(f'Поле фиксированного макета переполнено ({actual:.1f}>{height:.1f} pt): {str(text)[:80]}')
    p.drawOn(c,x,y+height-actual)


def field(c, name, text, color=INK):
    x,top,w,h,size,bold=SLOTS[name]
    text_box(c,text,x,PAPER_H-top-h,w,h,size,color,bold)


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


def render(data,destination,font_dir=None):
    result=analyze(data)
    register_fonts(font_dir)
    target=Path(destination);target.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(suffix='.pdf',dir=str(target.parent));os.close(fd)
    try:
        c=Canvas(temporary,pagesize=(PAPER_W,PAPER_H),invariant=1,pageCompression=1)
        c.setTitle('Заключение по новому займу — '+result['company']['name'])
        c.setAuthor('Borrower Financial Analysis')
        c.setSubject('Fixed layout '+LAYOUT_VERSION)
        rect(c,0,0,PAPER_W,PAPER_H,'#FFFFFF')
        field(c,'brand','ФИНАНСЫ ЗАЁМЩИКА')
        field(c,'kind','ПРЕДВАРИТЕЛЬНАЯ ОЦЕНКА ЗАЁМЩИКА')
        rule(c,36,46,559)
        demo=' / ВЫМЫШЛЕННЫЙ ПРИМЕР' if result['company'].get('fictional') else ''
        field(c,'company',result['company']['name']+demo,MUTED)
        field(c,'title','Заключение по новому займу')
        rect(c,36,144,523,78,PALE);rect(c,36,144,3,78,BLUE)
        field(c,'summary',result['conclusion'])
        field(c,'coverage',f"Предварительный анализ • рассчитано {result['calculation_coverage']} показателей",MUTED)
        definitions=[('amount','new_loan_amount','СУММА ЗАЙМА'),('rate','new_loan_rate_pct','ГОДОВЫХ'),('term','new_loan_term_months','СРОК')]
        for slot,key,label in definitions:
            val,refs=loan_fact(data,key)
            display=amount(val) if slot=='amount' else 'Нет данных' if val is None else compact(val)+('%' if slot=='rate' else ' мес.')
            # Missing-value font is a fixed design token, never an auto-fit rule.
            if val is None:
                x,top,w,h,_,_=SLOTS['loan_'+slot]
                text_box(c,display,x,PAPER_H-top-h,w,h,15)
            else:field(c,'loan_'+slot,display)
            if slot=='term':
                label += ' / ТЕЛО В КОНЦЕ' if data.get('loan_repayment')=='bullet' else ' / ПО ГРАФИКУ' if data.get('loan_repayment')=='schedule' else ''
            field(c,'loan_'+slot+'_label',label+(' ['+', '.join(refs)+']' if refs else ''),MUTED)
        rule(c,36,319,559,INK,.9)
        for key,label in [('title','ПОКАЗАТЕЛЬ'),('value','ЗНАЧЕНИЕ'),('note','ПРОЧТЕНИЕ')]:field(c,'head_'+key,label,MUTED)
        for i,metric in enumerate(result['metrics']):
            top=363+i*43
            if i==4:rect(c,36,top-7,523,43,HIGHLIGHT)
            text_box(c,metric['title'],36,PAPER_H-top-30,245,30,10)
            val=metric['display'] if metric['value'] is not None else 'Нет данных'
            text_box(c,val,292,PAPER_H-(top-4)-33,100,33,18 if metric['value'] is not None else 10,bold=True)
            refs=' ['+', '.join(metric['sources'])+']' if metric['sources'] else ''
            # The analytical limitation remains visible even when no ratio is available.
            note=metric['note']+refs
            text_box(c,note,412,PAPER_H-(top-1)-38,147,38,7.1,color=MUTED)
            rule(c,36,top+31,559)
        stress=result['stress']
        field(c,'stress_title',f"Стресс: прибыль -{stress['earnings_drop_pct']:g}%")
        if stress['interest_coverage'] is None:
            x,top,w,h,_,_=SLOTS['stress_value']
            text_box(c,'Нет данных',x,PAPER_H-top-h,w,h,15,color=MUTED)
        else:field(c,'stress_value',f"{stress['interest_coverage']:.2f}×".replace('.',','),BLUE)
        field(c,'stress_note','Покрытие процентов после займа; основной долг не включён. Условный сценарий, не прогноз.',MUTED)
        rule(c,36,653,559,INK,.9)
        field(c,'action_title','УСЛОВИЯ ДЛЯ ДАЛЬНЕЙШЕГО РАССМОТРЕНИЯ',BLUE)
        lines=[*result['observations'],*result['questions']]
        field(c,'actions','\n'.join(f'{i:02d}  {s}' for i,s in enumerate(lines,1)))
        rule(c,36,756,559)
        field(c,'sources','Источники: '+'; '.join(s['id']+': '+Path(s['file']).name for s in result['sources']),MUTED)
        period=result['period']
        meta=f"Проверка {result['assessment_date']} • Баланс {result['balance_date']} • Период {period['start']} — {period['end']}"
        field(c,'limitations',meta+'\n'+' '.join(result['limitations']),MUTED)
        field(c,'footer','ФИНАНСОВЫЙ АНАЛИЗ / '+LAYOUT_VERSION,MUTED)
        c.showPage();c.save();os.replace(temporary,target)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_json',help='Исходные факты, не редактируемый результат расчёта')
    parser.add_argument('output_pdf')
    parser.add_argument('--font-dir',help='Каталог с точными копиями шрифтов зафиксированного макета')
    parser.add_argument('--audit-json')
    args=parser.parse_args()
    try:
        source,output=Path(args.input_json).resolve(),Path(args.output_pdf).resolve()
        if source==output or (args.audit_json and Path(args.audit_json).resolve() in {source,output}):
            raise InputError('Пути исходника, PDF и аудита должны различаться')
        result=render(load_input(source),output,args.font_dir)
        if args.audit_json:
            audit=Path(args.audit_json);audit.parent.mkdir(parents=True,exist_ok=True)
            audit.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    except (InputError,LayoutError,ValueError,OSError,KeyError,TypeError) as exc:
        print(f'Ошибка: {exc}',file=sys.stderr);return 2
    print(str(output));return 0

if __name__=='__main__':raise SystemExit(main())
