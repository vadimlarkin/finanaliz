#!/usr/bin/env python3
"""Reject noncanonical principal terminology in generated reports and data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import unicodedata

# Financial reports use the canonical term «основной долг» in the correct case.
# Match the colloquial noun in singular and plural, independently of word order.
FORBIDDEN = re.compile(r'(?<!\w)тел(?:о|а|у|ом|е|ам|ами|ах)(?!\w)', re.IGNORECASE)


class TerminologyError(ValueError):
    pass


def check_text(text: str) -> None:
    normalized = unicodedata.normalize('NFKC', text)
    normalized = ''.join(c for c in normalized if unicodedata.category(c) != 'Cf')
    if FORBIDDEN.search(normalized):
        raise TerminologyError('Недопустимая терминология: используйте «основной долг» в нужном падеже.')


def check_payload(value) -> None:
    if isinstance(value, str):
        check_text(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            check_payload(key)
            check_payload(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            check_payload(item)


def check_file(path) -> None:
    path = Path(path)
    if path.suffix.lower() == '.pdf':
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise TerminologyError('Для проверки PDF установите зависимости scripts/requirements.txt.') from exc
        reader = PdfReader(str(path))
        if not reader.pages:
            raise TerminologyError('Пустой PDF: проверка не выполнена.')
        for page in reader.pages:
            text = page.extract_text()
            if not text or not text.strip():
                raise TerminologyError('В PDF есть страница без извлекаемого текста: проверка не выполнена.')
            check_text(text)
        check_payload(dict(reader.metadata or {}))
    elif path.suffix.lower() == '.json':
        check_payload(json.loads(path.read_text(encoding='utf-8-sig')))
    else:
        check_text(path.read_text(encoding='utf-8-sig'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', help='PDF, JSON, Markdown или текстовый отчёт')
    args = parser.parse_args(argv)
    for file in args.files:
        try:
            check_file(file)
        except (OSError, ValueError, UnicodeError) as exc:
            print(f'{Path(file).name}: {exc}', file=sys.stderr)
            return 2
    print('Терминология проверена: основной долг.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
