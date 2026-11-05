#!/usr/bin/env python3
"""Extract local PDF pages without changing the source document."""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import platform
import shutil
import subprocess
import tempfile
import json
from pathlib import Path
import sys

import pypdf
from pypdf import PdfReader

VERSION = '1'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(command, *, timeout=120):
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if completed.returncode:
        raise ValueError(f"{Path(command[0]).name}: {(completed.stderr or completed.stdout).strip()}")
    return completed.stdout.strip()


def tool_version(command):
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    return (completed.stdout + completed.stderr).strip()


def ocr_tools(mode, language):
    """Describe available local OCR before cache lookup, without recognition."""
    renderer = shutil.which('pdftoppm')
    result = {'engine': None, 'error': None, 'renderer': None}
    if mode == 'none':
        result['error'] = 'OCR отключено параметром --ocr none.'
        return result
    if not renderer:
        result['error'] = 'OCR недоступно: локальный pdftoppm не найден.'
        return result
    result['renderer'] = {'path': renderer, 'version': tool_version([renderer, '-v'])}
    tesseract = shutil.which('tesseract')
    swift = shutil.which('swift') if sys.platform == 'darwin' else None
    helper = Path(__file__).with_name('pdf_vision_ocr.swift')
    engines = ['tesseract', 'vision'] if mode == 'auto' else [mode]
    failures = []
    for engine in engines:
        try:
            if engine == 'tesseract':
                if not tesseract:
                    raise ValueError('Tesseract не найден')
                supported = run([tesseract, '--list-langs']).splitlines()[1:]
                languages = language.split('+')
                missing = set(languages) - set(supported)
                if missing:
                    raise ValueError('Tesseract не поддерживает языки: ' + ', '.join(sorted(missing)))
                result.update(engine=engine, path=tesseract, version=tool_version([tesseract, '--version']),
                              supported_languages=sorted(supported), languages=languages)
            elif engine == 'vision':
                if not swift:
                    raise ValueError('Apple Vision требует macOS и Swift')
                info = json.loads(run([swift, str(helper), '--languages']))
                mapping = {'rus': 'ru-RU', 'eng': 'en-US'}
                languages = [mapping.get(x, x) for x in language.split('+')]
                missing = set(languages) - set(info['languages'])
                if missing:
                    raise ValueError('Apple Vision не поддерживает языки: ' + ', '.join(sorted(missing)))
                result.update(engine=engine, path=swift, version=tool_version([swift, '--version']),
                              os_version=platform.mac_ver()[0], helper_sha256=digest(helper.read_bytes()),
                              revision=info['revision'], supported_languages=sorted(info['languages']), languages=languages)
            result['error'] = None
            return result
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            failures.append(str(exc))
    result['error'] = 'OCR недоступно: ' + '; '.join(failures)
    return result


def recognize(data, number, tools, dpi):
    with tempfile.TemporaryDirectory(prefix='pdf-local-ocr-') as name:
        directory = Path(name)
        source = directory / 'source.pdf'
        source.write_bytes(data)
        prefix = directory / 'page'
        run([tools['renderer']['path'], '-f', str(number), '-l', str(number),
             '-singlefile', '-r', str(dpi), '-png', str(source), str(prefix)])
        png = str(prefix.with_suffix('.png'))
        if tools['engine'] == 'vision':
            helper = Path(__file__).with_name('pdf_vision_ocr.swift')
            result = json.loads(run([tools['path'], str(helper), png, '+'.join(tools['languages'])]))
            return result['text'], result.get('minimum_confidence')
        return run([tools['path'], png, 'stdout', '-l', '+'.join(tools['languages'])]), None


def extract(source, cache_dir, *, language='rus+eng', ocr='auto', dpi=200, min_text_chars=1):
    if ocr not in {'auto', 'vision', 'tesseract', 'none'}:
        raise ValueError('Неизвестный режим OCR.')
    if not 72 <= dpi <= 600:
        raise ValueError('dpi должен быть от 72 до 600.')
    if min_text_chars < 1:
        raise ValueError('min-text-chars должен быть положительным.')
    if not language or any(not x for x in language.split('+')):
        raise ValueError('Язык OCR необходимо указать явно.')
    source = Path(source).resolve()
    data = source.read_bytes()
    settings = dict(language=language, ocr=ocr, dpi=dpi, min_text_chars=min_text_chars)
    tools = {'pypdf': pypdf.__version__, 'extractor': VERSION,
             'extractor_sha256': digest(Path(__file__).read_bytes()), 'ocr': ocr_tools(ocr, language)}
    key = digest(json.dumps({'source': digest(data), 'settings': settings, 'tools': tools}, sort_keys=True).encode())
    directory = Path(cache_dir).resolve() / key
    directory.mkdir(parents=True, exist_ok=True)
    saved = directory / 'result.json'
    manifest = directory / 'manifest.json'
    try:
        hashes = json.loads(manifest.read_text(encoding='utf-8'))
        if all(digest((directory / name).read_bytes()) == hashes[name] for name in ['result.json', 'text.txt']):
            result = json.loads(saved.read_text(encoding='utf-8'))
            result['source']['path'] = str(source)
            result['cache_hit'] = True
            return result
    except (OSError, ValueError, KeyError, TypeError):
        pass
    pages = []
    reader = PdfReader(BytesIO(data))
    if not reader.pages:
        raise ValueError('PDF не содержит страниц.')
    for number, page in enumerate(reader.pages, 1):
        errors = []
        try:
            text = page.extract_text() or ''
        except Exception as exc:
            text = ''
            errors.append('Ошибка извлечения готового текста: ' + str(exc))
        embedded_text = text
        ocr_text = None
        usable = sum(c.isalnum() for c in text) >= min_text_chars
        method = 'embedded' if usable else 'unavailable'
        confidence = None
        if not usable:
            if tools['ocr']['engine']:
                try:
                    recognized, confidence = recognize(data, number, tools['ocr'], dpi)
                    if sum(c.isalnum() for c in recognized) >= min_text_chars:
                        ocr_text = recognized
                        text = (embedded_text.rstrip() + '\n[OCR]\n' if embedded_text.strip() else '') + recognized
                        method = 'ocr'
                    else:
                        errors.append('OCR не получило пригодного текста; страница может быть пустой или нечитаемой.')
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    errors.append('Ошибка OCR: ' + str(exc))
            else:
                errors.append(tools['ocr']['error'])
        pages.append({'page': number, 'text': text, 'embedded_text': embedded_text, 'ocr_text': ocr_text,
                      'method': method, 'errors': errors,
                      'review_required': method != 'embedded' or bool(errors), 'minimum_confidence': confidence})
    result = {'schema_version': 1, 'source': {'path': str(source), 'sha256': digest(data)},
              'settings': settings, 'tools': tools, 'cache_key': key, 'cache_hit': False,
              'complete': all(p['method'] != 'unavailable' for p in pages), 'pages': pages, 'review_pages': [p['page'] for p in pages if p['review_required']], 'text_path': str(directory / 'text.txt')}
    (directory / 'text.txt').write_text('\n'.join(f"=== Page {p['page']} ===\n{p['text']}" for p in pages), encoding='utf-8')
    (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest.write_text(json.dumps({name: digest((directory / name).read_bytes()) for name in ['result.json', 'text.txt']}), encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--cache-dir', required=True, type=Path)
    parser.add_argument('--language', default='rus+eng')
    parser.add_argument('--ocr', choices=['auto', 'vision', 'tesseract', 'none'], default='auto')
    parser.add_argument('--dpi', default=200, type=int)
    parser.add_argument('--min-text-chars', default=1, type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    source_is_output = bool(args.output and (args.output.resolve() == args.pdf.resolve()
                            or (args.output.exists() and args.pdf.exists() and args.output.samefile(args.pdf))))
    try:
        if source_is_output:
            raise ValueError('Выходной JSON не может заменять исходный PDF.')
        result = extract(args.pdf, args.cache_dir, language=args.language, ocr=args.ocr, dpi=args.dpi, min_text_chars=args.min_text_chars)
    except Exception as exc:
        result = {'complete': False, 'error': str(exc), 'pages': []}
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output and not source_is_output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded)
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
