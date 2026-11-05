"""Observable CLI contract for local page extraction."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/borrower-financial-analysis/scripts/extract_pdf_cached.py'


class PdfExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdf = self.root / 'source.pdf'

    def text_pdf(self, lines):
        c = canvas.Canvas(str(self.pdf))
        for line in lines:
            if line:
                c.drawString(50, 750, line)
            c.showPage()
        c.save()

    def run_cli(self, *args, env=None):
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.pdf), '--cache-dir',
                                 str(self.root / 'cache'), '--ocr', 'none', *args],
                                text=True, capture_output=True, env=env)
        return result, json.loads(result.stdout) if result.stdout else None

    def test_text_pages_keep_page_numbers_and_source_hash(self):
        self.text_pdf(['SOURCE A 123.45', 'SOURCE B 678.90'])
        before = self.pdf.read_bytes()
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([p['method'] for p in result['pages']], ['embedded', 'embedded'])
        self.assertEqual([p['page'] for p in result['pages']], [1, 2])
        self.assertIn('SOURCE A 123.45', result['pages'][0]['text'])
        self.assertIn('SOURCE B 678.90', result['pages'][1]['text'])
        self.assertEqual(result['source']['sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.pdf.read_bytes(), before)
        text = Path(result['text_path']).read_text()
        self.assertIn('=== Page 1 ===', text)
        self.assertIn('=== Page 2 ===', text)

    def test_unchanged_document_reuses_complete_cached_artifacts(self):
        self.text_pdf(['SOURCE A 123.45'])
        _, first = self.run_cli()
        result_path = Path(first['text_path']).parent / 'result.json'
        timestamp = result_path.stat().st_mtime_ns
        process, second = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(second['cache_hit'])
        self.assertEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(result_path.stat().st_mtime_ns, timestamp)

    def test_cache_changes_for_source_settings_and_corrupt_text(self):
        self.text_pdf(['SOURCE A 123.45'])
        _, first = self.run_cli()
        _, changed_setting = self.run_cli('--dpi', '300')
        self.assertNotEqual(first['cache_key'], changed_setting['cache_key'])
        self.text_pdf(['SOURCE B 678.90'])
        _, changed_source = self.run_cli()
        self.assertNotEqual(first['cache_key'], changed_source['cache_key'])
        Path(changed_source['text_path']).write_text('CORRUPTED')
        _, repaired = self.run_cli()
        self.assertFalse(repaired['cache_hit'])
        self.assertIn('SOURCE B 678.90', Path(repaired['text_path']).read_text())

    def test_missing_ocr_reports_scan_page_and_keeps_embedded_text(self):
        self.text_pdf(['SOURCE A 123.45', ''])
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 2)
        self.assertFalse(result['complete'])
        self.assertEqual(result['pages'][1]['method'], 'unavailable')
        self.assertIn('OCR', ' '.join(result['pages'][1]['errors']))
        self.assertEqual(result['review_pages'], [2])
        self.assertIn('SOURCE A 123.45', Path(result['text_path']).read_text())
        _, cached = self.run_cli()
        self.assertTrue(cached['cache_hit'])
        self.assertFalse(cached['complete'])

    def test_invalid_options_and_unreadable_pdf_are_explicit(self):
        self.text_pdf(['SOURCE A 123.45'])
        process, result = self.run_cli('--dpi', '0')
        self.assertEqual(process.returncode, 2)
        self.assertIn('dpi', result['error'])
        self.pdf.write_bytes(b'not a PDF')
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 2)
        self.assertFalse(result['complete'])
        self.assertTrue(result['error'])

    @unittest.skipUnless(sys.platform == 'darwin', 'Apple Vision requires macOS')
    def test_actual_vision_reads_only_scan_in_mixed_pdf_and_reuses_it(self):
        from PIL import Image, ImageDraw, ImageFont
        from reportlab.lib.utils import ImageReader
        image = Image.new('RGB', (1800, 700), 'white')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 64)
        draw.text((70, 160), 'Invoice 123.45 2026-11-05', font=font, fill='black')
        c = canvas.Canvas(str(self.pdf))
        c.drawString(50, 750, 'SOURCE A 678.90')
        c.showPage()
        c.drawImage(ImageReader(image), 40, 450, width=520, height=202)
        c.showPage()
        c.save()
        original = self.pdf.read_bytes()
        process, result = self.run_cli('--ocr', 'vision', '--language', 'eng')
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual([p['method'] for p in result['pages']], ['embedded', 'ocr'])
        self.assertIn('SOURCE A 678.90', result['pages'][0]['text'])
        self.assertIn('123.45', result['pages'][1]['text'])
        self.assertIn('2026-11-05', result['pages'][1]['text'])
        self.assertEqual(result['review_pages'], [2])
        self.assertIn('en-US', result['tools']['ocr']['supported_languages'])
        self.assertEqual(self.pdf.read_bytes(), original)
        _, cached = self.run_cli('--ocr', 'vision', '--language', 'eng')
        self.assertTrue(cached['cache_hit'])
        self.assertEqual(result['pages'], cached['pages'])

    def test_output_cannot_replace_source_pdf(self):
        self.text_pdf(['SOURCE A 123.45'])
        before = self.pdf.read_bytes()
        process, result = self.run_cli('--output', str(self.pdf))
        self.assertEqual(process.returncode, 2)
        self.assertIn('исход', result['error'])
        self.assertEqual(self.pdf.read_bytes(), before)


    def fake_tesseract(self):
        executable = self.root / 'tesseract'
        executable.write_text('#!' + sys.executable + "\n" + """
from pathlib import Path
import sys
root = Path(__file__).parent
if '--version' in sys.argv:
    print('fake-tesseract ' + (root / 'version').read_text())
elif '--list-langs' in sys.argv:
    print('List of available languages (1):\\neng')
else:
    with (root / 'ocr-calls').open('a') as f:
        f.write('page\\n')
    print('OCR SAMPLE 123.45')
""")
        executable.chmod(0o755)
        (self.root / 'version').write_text('1')
        return dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ.get('PATH', ''))

    def test_system_ocr_adapter_runs_once_and_version_change_invalidates_cache(self):
        self.text_pdf(['SOURCE A 123.45', ''])
        environment = self.fake_tesseract()
        process, first = self.run_cli('--ocr', 'tesseract', '--language', 'eng', env=environment)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual([p['method'] for p in first['pages']], ['embedded', 'ocr'])
        _, second = self.run_cli('--ocr', 'tesseract', '--language', 'eng', env=environment)
        self.assertTrue(second['cache_hit'])
        self.assertEqual((self.root / 'ocr-calls').read_text().splitlines(), ['page'])
        (self.root / 'version').write_text('2')
        _, third = self.run_cli('--ocr', 'tesseract', '--language', 'eng', env=environment)
        self.assertFalse(third['cache_hit'])
        self.assertNotEqual(first['cache_key'], third['cache_key'])
        self.assertEqual((self.root / 'ocr-calls').read_text().splitlines(), ['page', 'page'])

    def test_unsupported_language_does_not_run_recognition(self):
        self.text_pdf([''])
        environment = self.fake_tesseract()
        process, result = self.run_cli('--ocr', 'tesseract', '--language', 'rus+eng', env=environment)
        self.assertEqual(process.returncode, 2)
        self.assertIn('rus', ' '.join(result['pages'][0]['errors']))
        self.assertFalse((self.root / 'ocr-calls').exists())

    def test_short_embedded_fragment_is_preserved_when_page_needs_ocr(self):
        self.text_pdf(['ID'])
        environment = self.fake_tesseract()
        process, result = self.run_cli('--ocr', 'tesseract', '--language', 'eng',
                                       '--min-text-chars', '5', env=environment)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertIn('ID', result['pages'][0]['text'])
        self.assertIn('OCR SAMPLE 123.45', result['pages'][0]['text'])

    def image_pdf_with_page_number(self):
        from PIL import Image, ImageDraw
        from reportlab.lib.utils import ImageReader
        image = Image.new('RGB', (1200, 400), 'white')
        ImageDraw.Draw(image).text((40, 100), 'Invoice 123.45 2026-11-05', fill='black', font_size=45)
        c = canvas.Canvas(str(self.pdf))
        c.drawImage(ImageReader(image), 40, 450, width=520, height=173)
        c.drawString(290, 25, '1')
        c.showPage()
        c.save()

    def test_scan_with_embedded_page_number_is_incomplete_without_ocr(self):
        self.image_pdf_with_page_number()
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertFalse(result['complete'])
        self.assertEqual(result['pages'][0]['method'], 'unavailable')
        self.assertEqual(result['pages'][0]['embedded_text'].strip(), '1')
        self.assertEqual(result['review_pages'], [1])

    @unittest.skipUnless(sys.platform == 'darwin', 'Apple Vision requires macOS')
    def test_vision_reads_scan_beyond_embedded_page_number(self):
        self.image_pdf_with_page_number()
        process, result = self.run_cli('--ocr', 'vision', '--language', 'eng')
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        page = result['pages'][0]
        self.assertEqual(page['method'], 'ocr')
        self.assertEqual(page['ocr_reason'], 'raster_content')
        self.assertEqual(page['embedded_text'].strip(), '1')
        self.assertIn('123.45', page['ocr_text'])
        self.assertIn('2026-11-05', page['ocr_text'])
        self.assertTrue(page['text'].startswith('1\n[OCR]\n'))
        self.assertEqual(result['review_pages'], [1])

    def test_embedded_text_with_raster_logo_explains_conservative_ocr_requirement(self):
        from PIL import Image
        from reportlab.lib.utils import ImageReader
        c = canvas.Canvas(str(self.pdf))
        c.drawString(50, 750, 'Long usable embedded invoice text with amount 678.90 and date 2026-11-05')
        c.drawImage(ImageReader(Image.new('RGB', (20, 20), 'blue')), 50, 700, width=20, height=20)
        c.showPage()
        c.save()
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 2)
        self.assertEqual(result['pages'][0]['raster_images'], 1)
        self.assertEqual(result['pages'][0]['ocr_reason'], 'raster_content')
        self.assertIn('678.90', result['pages'][0]['embedded_text'])
        self.assertEqual(result['review_pages'], [1])
