#!/usr/bin/env python3
"""Render a single A4 page from validated input. No network or model calls."""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import sys
import tempfile
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph

from analyze import InputError, analyze, load_input

INK = '#152C42'
MUTED = '#647487'
COLORS = {'green': '#268B70', 'yellow': '#D69429', 'red': '#CA5059',
          'unknown': '#B4BCC5', 'context': '#8995A2'}
PAPER_W, PAPER_H = 595.276, 841.89


class LayoutError(ValueError):
    pass


def register_fonts(font_dir=None):
    candidates = []
    if font_dir:
        folder = Path(font_dir)
        candidates.extend([(folder/'DejaVuSans.ttf', folder/'DejaVuSans-Bold.ttf'),
                           (folder/'Arial.ttf', folder/'Arial Bold.ttf')])
    assets = Path(__file__).resolve().parents[1]/'assets/fonts'
    candidates.extend([(assets/'DejaVuSans.ttf', assets/'DejaVuSans-Bold.ttf'),
                       (Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')),
                       (Path('/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf'), Path('/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf')),
                       (Path('/System/Library/Fonts/Supplemental/Arial.ttf'), Path('/System/Library/Fonts/Supplemental/Arial Bold.ttf'))])
    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont('Body', str(regular)))
            pdfmetrics.registerFont(TTFont('BodyBold', str(bold)))
            return
    raise LayoutError('Нужны кириллические TTF-шрифты: укажите --font-dir или установите fonts-dejavu-core')


def text_box(c, text, x, y, width, height, size=9, color=INK,
             bold=False, center=False):
    style = ParagraphStyle('box', fontName='BodyBold' if bold else 'Body',
                           fontSize=size, leading=size*1.28,
                           textColor=colors.HexColor(color),
                           alignment=TA_CENTER if center else TA_LEFT,
                           splitLongWords=True)
    paragraph = Paragraph(html.escape(str(text)).replace('\n', '<br/>'), style)
    _, actual = paragraph.wrap(width, height)
    if actual > height+0.1:
        raise LayoutError(f'Текст не помещается в один лист ({actual:.1f}>{height:.1f} pt): {str(text)[:70]}')
    paragraph.drawOn(c, x, y+height-actual)


def card(c, x, y, width, height, fill='#F4F7FA'):
    c.setFillColor(colors.HexColor(fill))
    c.setStrokeColor(colors.HexColor('#E1E7ED'))
    c.setLineWidth(0.6)
    c.roundRect(x, y, width, height, 10, fill=1, stroke=1)


def gauge(c, metric, x, y, width, height):
    card(c, x, y, width, height)
    text_box(c, metric['title'], x+8, y+height-31, width-16, 21,
             size=10, bold=True, center=True)
    cx, cy, radius = x+width/2, y+71, 50
    low, high = metric['thresholds']
    maximum = metric['scale_max']
    val = metric['value']
    c.setLineWidth(8)
    if metric['status'] in {'unknown', 'context'}:
        c.setStrokeColor(colors.HexColor('#D5DBE1'))
        c.arc(cx-radius, cy-radius, cx+radius, cy+radius, 0, 180)
    else:
        boundaries = [0, low/maximum, high/maximum, 1]
        band_colors = ['green', 'yellow', 'red'] if metric['reverse'] else ['red', 'yellow', 'green']
        for left, right, color in zip(boundaries[:-1], boundaries[1:], band_colors):
            c.setStrokeColor(colors.HexColor(COLORS[color]))
            c.arc(cx-radius, cy-radius, cx+radius, cy+radius,
                  startAng=180-180*right, extent=180*(right-left)-0.8)
    c.setLineWidth(0.6)
    if val is not None:
        clamped = max(0, min(1, val/maximum))
        angle = math.radians(180-180*clamped)
        c.setStrokeColor(colors.HexColor(INK))
        c.setLineWidth(2)
        c.line(cx, cy, cx+39*math.cos(angle), cy+39*math.sin(angle))
        c.setFillColor(colors.HexColor(INK))
        c.circle(cx, cy, 3.5, stroke=0, fill=1)
    c.setFont('Body', 6)
    c.setFillColor(colors.HexColor(MUTED))
    c.drawString(cx-radius-1, cy-12, '0')
    c.drawRightString(cx+radius+3, cy-12, str(int(maximum))+'×')
    display = metric['display']
    if val is not None and (val < 0 or val > maximum):
        display += ' *'
    text_box(c, display, x+6, y+36, width-12, 29,
             size=19 if val is not None else 12, bold=True, center=True)
    labels = {'red': 'СИГНАЛ РИСКА', 'yellow': 'ТРЕБУЕТ ВНИМАНИЯ', 'green': 'ЕСТЬ ЗАПАС',
              'unknown': 'НЕДОСТАТОЧНО ДАННЫХ', 'context': 'БЕЗ ОЦЕНОЧНОЙ ШКАЛЫ'}
    text_box(c, labels[metric['status']], x+5, y+21, width-10, 11,
             size=6.5, color=COLORS[metric['status']], bold=True, center=True)
    reference = ', '.join(metric['sources']) or 'источника нет'
    text_box(c, '['+reference+']', x+6, y+6, width-12, 10,
             size=6, color=MUTED, center=True)


def render(data, destination, font_dir=None):
    result = analyze(data)
    register_fonts(font_dir)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(suffix='.pdf', dir=str(target.parent))
    os.close(fd)
    try:
        c = Canvas(temporary, pagesize=(PAPER_W, PAPER_H), invariant=1,
                   pageCompression=1)
        c.setTitle('Финансовая проверка — '+result['company']['name'])
        c.setAuthor('Borrower Financial Analysis')
        c.setFillColor(colors.white)
        c.rect(0, 0, PAPER_W, PAPER_H, fill=1, stroke=0)
        margin, usable = 28, PAPER_W-56
        text_box(c, 'ФИНАНСЫ ЗАЁМЩИКА', margin, 793, usable, 27,
                 size=20, bold=True)
        demo = '  /  ВЫМЫШЛЕННЫЙ ПРИМЕР' if result['company'].get('fictional') else ''
        text_box(c, result['company']['name']+demo, margin, 771, usable, 19,
                 size=11, bold=True)
        period = result['period']
        metadata = f"Проверка: {result['assessment_date']}  •  Баланс: {result['balance_date']}  •  Период: {period['start']} — {period['end']}"
        text_box(c, metadata, margin, 750, usable, 15, size=7.2, color=MUTED)
        fill = {'high': '#FCEFF0', 'attention': '#FFF7E7', 'not_seen_in_metrics': '#EBF6F1'}[result['risk']]
        card(c, margin, 686, usable, 54, fill)
        text_box(c, result['conclusion'], margin+12, 714, usable-24, 17,
                 size=11, bold=True)
        completeness = 'данные ограничены' if result['data_status'] == 'limited' else 'достаточно для расчёта; проверка предварительная'
        text_box(c, f"Доступны расчёты: {result['calculation_coverage']}  •  {completeness}",
                 margin+12, 695, usable-24, 14, size=8.2, color=MUTED)

        gap, width, height = 10, (usable-20)/3, 157
        positions = [(margin+i*(width+gap), 516) for i in range(3)]
        positions += [(margin+i*(width+gap), 349) for i in range(2)]
        for metric, (x, y) in zip(result['metrics'], positions):
            gauge(c, metric, x, y, width, height)
        sx = margin+2*(width+gap)
        card(c, sx, 349, width, height, '#EEF2F7')
        text_box(c, 'ЗАПАС ПРОЧНОСТИ', sx+10, 477, width-20, 17,
                 size=9, bold=True, center=True)
        stress = result['stress']
        text_box(c, f"Прибыль −{stress['earnings_drop_pct']:g}%", sx+9, 449, width-18, 19,
                 size=11, center=True)
        display = 'Нет данных' if stress['interest_coverage'] is None else f"{stress['interest_coverage']:.2f}×".replace('.', ',')
        text_box(c, display, sx+8, 417, width-16, 29, size=21,
                 bold=True, center=True, color=COLORS[stress['status']])
        text_box(c, 'Покрытие процентов с новым займом.\nУсловный сценарий, не прогноз.',
                 sx+11, 365, width-22, 42, size=8, center=True)

        explanations = []
        for index, metric in enumerate(result['metrics'], 1):
            explanations.append(f"{index}. {metric['note']}")
        text_box(c, 'КАК ЧИТАТЬ ДАТЧИКИ', margin, 326, usable, 14, size=8.5, bold=True)
        text_box(c, '\n'.join(explanations), margin, 261, usable, 64, size=7.2, color=MUTED)
        text_box(c, 'ГЛАВНОЕ ДЛЯ КРЕДИТОРА', margin, 238, usable, 15, size=9, bold=True)
        observations = '\n'.join('• '+t for t in result['observations'])
        text_box(c, observations, margin, 190, usable, 47, size=8.1)
        text_box(c, 'ЧТО УТОЧНИТЬ ДО РЕШЕНИЯ', margin, 169, usable, 14, size=8.5, bold=True)
        text_box(c, '\n'.join('• '+t for t in result['questions']), margin, 121, usable, 47, size=8)

        source_labels = [s['id']+': '+Path(s['file']).name for s in result['sources']]
        text_box(c, 'Источники: '+'; '.join(source_labels), margin, 84, usable, 30,
                 size=6.5, color=MUTED)
        foot = result['scale_notice']+'\n* Значение за пределами шкалы показано числом. Возврат тела займа проверяется отдельно.\nРешение о финансировании принимает кредитор. Документы заёмщика не загружаются скриптами в сеть.'
        text_box(c, foot, margin, 31, usable, 46, size=6.5, color=MUTED)
        c.showPage()
        c.save()
        os.replace(temporary, target)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_json', help='Исходные факты, не редактируемый результат расчёта')
    parser.add_argument('output_pdf')
    parser.add_argument('--font-dir')
    parser.add_argument('--audit-json')
    args = parser.parse_args()
    try:
        source, output = Path(args.input_json).resolve(), Path(args.output_pdf).resolve()
        if source == output or (args.audit_json and Path(args.audit_json).resolve() in {source, output}):
            raise InputError('Пути исходника, PDF и аудита должны различаться')
        result = render(load_input(source), output, args.font_dir)
        if args.audit_json:
            audit = Path(args.audit_json)
            audit.parent.mkdir(parents=True, exist_ok=True)
            audit.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    except (InputError, LayoutError, ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Ошибка: {exc}', file=sys.stderr)
        return 2
    print(str(output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
