#!/usr/bin/env python3
"""Fixed one-page presentation; numbers must already be independently verified."""
from __future__ import annotations
import argparse
import html
import json
import os
from pathlib import Path
import sys
import tempfile
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.pdfgen.canvas import Canvas
from pypdf import PdfReader
from render_dashboard import register_fonts, LayoutError
from check_terminology import check_payload, check_file

LAYOUT_VERSION = 'credit-committee-2.0'
W, H = 595.28, 841.89
INK, MUTED, BLUE = '#15283F', '#66767A', '#204DC8'
RULE, PALE, HIGHLIGHT = '#D8DFDD', '#F1F3F5', '#ECF0FB'
# x, top, width, height, size, leading, bold, color, alignment.
# All distances are points from the top left. No caller-supplied styles.
SLOTS = {
 'brand': (36,23,260,12,7.5,9,True,INK,0),
 'kind': (300,23,259,12,7.2,9,False,INK,2),
 'company': (36,65,523,18,9,11,True,MUTED,0),
 'title': (36,86,523,36,27,33,True,INK,0),
 'metadata': (36,124,523,14,8,10,False,MUTED,0),
 'summary': (51,157,493,34,13.2,16.5,True,INK,0),
 'loan_note': (51,196,493,13,7.8,9.5,False,MUTED,0),
 'earnings_title': (36,313,247,13,8.2,10,True,BLUE,0),
 'scenario_title': (312,313,247,13,8.2,10,True,BLUE,0),
 'current_note': (36,438,247,47,7.1,8.6,False,MUTED,0),
 'scenario_note': (312,422,247,37,7.1,8.6,False,MUTED,0),
 'stress_note': (312,465,247,30,7.1,8.6,False,MUTED,0),
 'debt_title': (36,515,523,13,8.2,10,True,BLUE,0),
 'debt_main': (36,535,523,28,8.1,10.1,False,INK,0),
 'debt_note': (36,571,523,31,7.7,9.5,False,MUTED,0),
 'bank_title': (36,623,523,13,8.2,10,True,BLUE,0),
 'bank_note': (36,643,523,65,7.7,9.5,False,INK,0),
 'questions_title': (36,734,523,13,8.2,10,True,BLUE,0),
 'questions': (36,753,523,33,7.8,10.2,False,INK,0),
 'sources': (36,802,523,16,6.1,7.4,False,MUTED,0),
 'limitations': (36,824,523,10,6.1,7.4,False,MUTED,0),
}

def box(c, text, x, top, width, height, size, leading, bold=False, color=INK, align=0):
    if not isinstance(text,str) or not text.strip():
        raise LayoutError('Пустое поле: укажи отсутствие данных явно')
    safe=html.escape(text).replace('\n','<br/>').replace('₽','<font name="BodySymbol">₽</font>')
    style=ParagraphStyle('fixed',fontName='BodyBold' if bold else 'Body',fontSize=size,
                         leading=leading,textColor=colors.HexColor(color),alignment=align,
                         splitLongWords=True)
    p=Paragraph(safe,style)
    _, actual=p.wrap(width,height)
    if actual > height + .01:
        raise LayoutError(f'Переполнение: {actual:.1f} > {height:.1f} pt; {text[:60]}')
    p.drawOn(c,x,H-top-actual)

def field(c,key,text):
    box(c,text,*SLOTS[key])

def line(c,x,top,x2,color=RULE,width=.6):
    c.setStrokeColor(colors.HexColor(color)); c.setLineWidth(width)
    c.line(x,H-top,x2,H-top)

def rect(c,x,top,width,height,color):
    c.setFillColor(colors.HexColor(color))
    c.rect(x,H-top-height,width,height,fill=1,stroke=0)

def validate(data):
    check_payload(data)
    if data.get('layout_version') != LAYOUT_VERSION:
        raise LayoutError('Нужна версия '+LAYOUT_VERSION)
    for key,count,keys in [('headlines',3,('value','label')),
                           ('earnings_rows',4,('label','value')),
                           ('scenario_rows',3,('margin','earnings','coverage'))]:
        rows=data.get(key)
        if not isinstance(rows,list) or len(rows)!=count:
            raise LayoutError(f'{key}: требуется ровно {count} строк')
        for row in rows:
            if not isinstance(row,dict) or set(row)!=set(keys):
                raise LayoutError('Неверные поля '+key)
            if any(not isinstance(row[k],str) or not row[k].strip() for k in keys):
                raise LayoutError('Числа представления должны быть явными строками: '+key)
    questions=data.get('questions')
    if not isinstance(questions,list) or len(questions)!=3 or any(not isinstance(q,str) or not q.strip() for q in questions):
        raise LayoutError('Требуется ровно три уточнения')
    required={'layout_version','headlines','earnings_rows','scenario_rows','questions',
              'earnings_period_label',*[k for k in SLOTS if k not in
              {'brand','kind','title','earnings_title','questions_title','questions'}]}
    if set(data)!=required:
        raise LayoutError('Отсутствующие или лишние поля: '+str(sorted(set(data)^required)))
    for key in required-{'headlines','earnings_rows','scenario_rows','questions'}:
        if not isinstance(data[key],str) or not data[key].strip():
            raise LayoutError('Пустое поле '+key)

def render(data,destination,font_dir=None):
    validate(data); register_fonts(font_dir)
    target=Path(destination); target.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(suffix='.pdf',dir=target.parent); os.close(fd)
    try:
        c=Canvas(tmp,pagesize=(W,H),invariant=1,pageCompression=1)
        c.setTitle('Кредитная оценка - '+data['company'])
        c.setAuthor('Borrower Financial Analysis'); c.setSubject(LAYOUT_VERSION)
        rect(c,0,0,W,H,'#FFFFFF')
        field(c,'brand','ФИНАНСЫ ЗАЁМЩИКА')
        field(c,'kind','ПРЕДВАРИТЕЛЬНАЯ КРЕДИТНАЯ ОЦЕНКА')
        line(c,36,46,559)
        for key in ['company','metadata']:field(c,key,data[key])
        field(c,'title','Кредитная оценка')
        rect(c,36,147,523,66,PALE);rect(c,36,147,3,66,BLUE)
        for key in ['summary','loan_note']:field(c,key,data[key])
        for x,row in zip((36,216,396),data['headlines']):
            box(c,row['value'],x,232,163,34,25,30)
            box(c,row['label'],x,271,163,20,6.9,8.3,True,MUTED)
        line(c,36,299,559,INK,.9)
        field(c,'earnings_title','МАСШТАБ И ЗАРАБОТОК')
        field(c,'scenario_title',data['scenario_title'])
        box(c,'ПОКАЗАТЕЛЬ',36,334,180,12,6.6,8,True,MUTED)
        box(c,data['earnings_period_label'],220,334,63,12,6.6,8,True,MUTED,2)
        for x,w,txt,align in [(312,75,'МАРЖА¹',0),(390,64,'ЗАРАБОТОК',2),(485,74,'ПОКРЫТИЕ',2)]:
            box(c,txt,x,334,w,12,6.6,8,True,MUTED,align)
        for i,row in enumerate(data['earnings_rows']):
            y=354+i*20
            box(c,row['label'],36,y,180,13,8,10)
            box(c,row['value'],220,y-1,63,14,9,11,True,INK,2)
            line(c,36,y+15,283)
        rect(c,312,390,247,22,HIGHLIGHT)
        for i,row in enumerate(data['scenario_rows']):
            y=354+i*20
            box(c,row['margin'],312,y,75,14,8.5,10.5)
            box(c,row['earnings'],390,y,64,14,8.5,10.5,False,INK,2)
            box(c,row['coverage'],485,y-1,74,15,10,12,True,BLUE if i==2 else INK,2)
            line(c,312,y+15,559)
        for y in [503,611,795]:line(c,36,y,559)
        line(c,36,723,559,INK,.9)
        for key in ['current_note','scenario_note','stress_note','debt_title','debt_main',
                    'debt_note','bank_title','bank_note','sources','limitations']:
            field(c,key,data[key])
        field(c,'questions_title','ДО РЕШЕНИЯ / ТРИ УТОЧНЕНИЯ')
        field(c,'questions','\n'.join(f'{i}. {q}' for i,q in enumerate(data['questions'],1)))
        c.showPage();c.save()
        check_file(tmp)
        if len(PdfReader(tmp).pages)!=1:raise LayoutError('Отчёт должен состоять из одной страницы')
        os.replace(tmp,target)
    except Exception:
        Path(tmp).unlink(missing_ok=True);raise
    return target

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('report_json');p.add_argument('output_pdf');p.add_argument('--font-dir')
    a=p.parse_args()
    try:
        if Path(a.report_json).resolve()==Path(a.output_pdf).resolve():
            raise LayoutError('Пути исходника и PDF должны различаться')
        data=json.loads(Path(a.report_json).read_text(encoding='utf-8'))
        render(data,a.output_pdf,a.font_dir)
    except (ValueError,OSError,KeyError,TypeError) as e:
        print(f'Ошибка: {e}',file=sys.stderr);return 2
    print(a.output_pdf);return 0

if __name__=='__main__':raise SystemExit(main())
