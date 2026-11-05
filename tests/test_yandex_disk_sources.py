import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/borrower-financial-analysis/scripts'
sys.path.insert(0, str(SCRIPTS))
import fetch_yandex_disk as disk

ROOT = 'vd:IuNVVCtUbELgzA:disk:/'


class FakeDisk:
    """External disk adapter; source bytes and metadata are independent inputs."""
    def __init__(self):
        self.files = {ROOT+'Займы/Пример/договор.pdf': b'abc'}
        self.modified = '2026-11-01T00:00:00Z'
        self.downloads = []
        self.requests = []

    def resource(self, path, limit=100, offset=0):
        self.requests.append((path, limit, offset))
        if path in self.files:
            value = self.files[path]
            return {'path': path, 'name': path.rsplit('/', 1)[1], 'type': 'file',
                    'size': len(value), 'md5': hashlib.md5(value).hexdigest(),
                    'modified': self.modified, 'resource_id': 'id:'+path}
        prefix = path.rstrip('/')+'/'
        children = sorted({prefix+p[len(prefix):].split('/')[0]
                           for p in self.files if p.startswith(prefix)})
        return {'path': path, 'name': path.rsplit('/', 1)[1], 'type': 'dir',
                '_embedded': {'total': len(children), 'offset': offset,
                              'items': [self.resource(p) for p in children[offset:offset+limit]]}}

    def download_link(self, path):
        self.downloads.append(path)
        return path

    def open_download(self, link):
        return io.BytesIO(self.files[link])


class SharedDiskSources(unittest.TestCase):
    def test_paths_accept_selected_disk_only_and_decode_folder_url(self):
        expected = ROOT+'Займы/Пример'
        for value in ['Займы/Пример', expected, 'vd:/IuNVVCtUbELgzA/disk/Займы/Пример',
                      'https://disk.360.yandex.ru/client/vd/IuNVVCtUbELgzA/disk/%D0%97%D0%B0%D0%B9%D0%BC%D1%8B/%D0%9F%D1%80%D0%B8%D0%BC%D0%B5%D1%80']:
            self.assertEqual(disk.normalize_source(value), expected)
        for value in ['disk:/Займы/Пример', 'vd:OTHER:disk:/Займы', ROOT, '../outside',
                      'Займы/../other', 'https://example.org/folder',
                      'https://disk.360.yandex.ru/client/vd/IuNVVCtUbELgzA/disk/%2e%2e/other']:
            with self.subTest(value=value), self.assertRaises(disk.FetchError):
                disk.normalize_source(value)

    def test_folder_download_is_verified_and_has_provenance(self):
        client = FakeDisk()
        with tempfile.TemporaryDirectory() as tmp:
            result = disk.sync_source('Займы/Пример', tmp, client=client)
            self.assertEqual(result['status'], 'complete', result)
            entry = result['files'][ROOT+'Займы/Пример/договор.pdf']
            self.assertEqual(entry['status'], 'verified')
            self.assertEqual(entry['size'], 3)
            self.assertEqual(entry['md5'], '900150983cd24fb0d6963f7d28e17f72')
            self.assertEqual(entry['sha256'], 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
            self.assertEqual(Path(tmp, entry['local_path']).read_bytes(), b'abc')
            self.assertEqual(entry['relative_path'], 'договор.pdf')
            self.assertEqual(entry['resource_id'], 'id:'+ROOT+'Займы/Пример/договор.pdf')
            self.assertEqual(result['org_id'], '8117507')
            self.assertTrue(entry['downloaded_at'])
            self.assertEqual(json.loads(Path(tmp, 'source-manifest.json').read_text())['status'], 'complete')

    def test_nested_pagination_preserves_equal_names_archives_and_signatures(self):
        client = FakeDisk()
        selected = ROOT+'Займы/Пример'
        client.files = {selected+'/папка А/договор.pdf': b'abc',
                        selected+'/папка Б/договор.pdf': b'def',
                        selected+'/комплект.zip': b'ARCHIVE',
                        selected+'/комплект.zip.sig': b'SIGNATURE',
                        ROOT+'Займы/Сосед/секрет.pdf': b'NEVER'}
        with tempfile.TemporaryDirectory() as tmp:
            result = disk.sync_source(selected, tmp, client=client, page_size=1)
            self.assertEqual(result['status'], 'complete', result)
            self.assertEqual(len(result['files']), 4)
            self.assertEqual({e['relative_path'] for e in result['files'].values()},
                             {'папка А/договор.pdf', 'папка Б/договор.pdf', 'комплект.zip', 'комплект.zip.sig'})
            for path, entry in result['files'].items():
                self.assertEqual(Path(tmp, entry['local_path']).read_bytes(), client.files[path])
            self.assertFalse(any('Сосед' in path for path, _, _ in client.requests))

    def test_resume_rechecks_local_bytes_and_keeps_previous_source_versions(self):
        client = FakeDisk()
        source = ROOT+'Займы/Пример'
        remote = source+'/договор.pdf'
        with tempfile.TemporaryDirectory() as tmp:
            first = disk.sync_source(source, tmp, client=client)
            old_path = Path(tmp, first['files'][remote]['local_path'])
            second = disk.sync_source(source, tmp, client=client)
            self.assertTrue(second['files'][remote]['reused'])
            self.assertEqual(len(client.downloads), 1)
            old_path.write_bytes(b'CORRUPTED')
            third = disk.sync_source(source, tmp, client=client)
            self.assertEqual(Path(tmp, third['files'][remote]['local_path']).read_bytes(), b'abc')
            previous_good = Path(tmp, third['files'][remote]['local_path'])
            client.files[remote] = b'new version'
            client.modified = '2026-11-05T00:00:00Z'
            fourth = disk.sync_source(source, tmp, client=client)
            self.assertEqual(Path(tmp, fourth['files'][remote]['local_path']).read_bytes(), b'new version')
            self.assertEqual(previous_good.read_bytes(), b'abc')
            self.assertEqual(len(list(Path(tmp, 'history').glob('*.json'))), 3)

    def test_interrupted_transfer_is_not_verified_and_resume_preserves_good_files(self):
        client = FakeDisk()
        source = ROOT+'Займы/Пример'
        client.files[source+'/ещё.pdf'] = b'def'
        original = client.open_download
        def broken(link):
            if link.endswith('ещё.pdf'):
                raise OSError('https://downloader.disk.yandex.ru/?token=PRIVATE')
            return original(link)
        client.open_download = broken
        with tempfile.TemporaryDirectory() as tmp:
            result = disk.sync_source(source, tmp, client=client)
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['files'][source+'/договор.pdf']['status'], 'verified')
            self.assertEqual(result['files'][source+'/ещё.pdf']['status'], 'failed')
            self.assertNotIn('PRIVATE', Path(tmp, 'source-manifest.json').read_text())
            self.assertEqual(list(Path(tmp).rglob('*.part')), [])
            client.open_download = original
            resumed = disk.sync_source(source, tmp, client=client)
            self.assertEqual(resumed['status'], 'complete')
            self.assertTrue(resumed['files'][source+'/договор.pdf']['reused'])

    def test_empty_and_access_failures_are_different(self):
        for code, status in [('unauthorized', 401), ('forbidden', 403), ('not_found', 404),
                             ('locked', 423), ('rate_limited', 429), ('unavailable', 503)]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                client = FakeDisk()
                def resource(*args, **kwargs):
                    raise disk.FetchError(code, status)
                client.resource = resource
                result = disk.sync_source('Займы/Пример', tmp, client=client)
                self.assertEqual(result['status'], 'incomplete')
                self.assertFalse(result['listing_complete'])
                self.assertEqual(result['errors'][0]['http_status'], status)
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeDisk()
            client.files.clear()
            result = disk.sync_source('Займы/Пустая', tmp, client=client)
            self.assertEqual(result['status'], 'empty')
            self.assertTrue(result['listing_complete'])

    def test_size_and_checksum_mismatch_cannot_produce_verified_file(self):
        for content in [b'a', b'wrong size', b'xyz']:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                client = FakeDisk()
                client.open_download = lambda _: io.BytesIO(content)
                result = disk.sync_source('Займы/Пример', tmp, client=client)
                self.assertEqual(result['status'], 'incomplete')
                self.assertEqual(next(iter(result['files'].values()))['status'], 'failed')
                self.assertNotEqual(result['errors'][0]['code'], 'not_found')

    def test_source_root_cannot_change_in_same_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk.sync_source('Займы/Пример', tmp, client=FakeDisk())
            with self.assertRaises(disk.FetchError):
                disk.sync_source('Займы/Сосед', tmp, client=FakeDisk())

    def test_source_cannot_escape_selected_folder_or_follow_local_symlink(self):
        client = FakeDisk()
        original = client.resource
        def malicious(path, limit=100, offset=0):
            result = original(path, limit, offset)
            if result.get('type') == 'dir':
                result['_embedded']['items'] = [{'path': ROOT+'Займы/Сосед/x', 'type': 'file'}]
            return result
        client.resource = malicious
        with tempfile.TemporaryDirectory() as tmp:
            result = disk.sync_source('Займы/Пример', tmp, client=client)
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['errors'][0]['code'], 'outside_selected_source')
            self.assertEqual(client.downloads, [])
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            Path(tmp, 'originals').symlink_to(outside, target_is_directory=True)
            result = disk.sync_source('Займы/Пример', tmp, client=FakeDisk())
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_case_colliding_remote_directories_do_not_merge_locally(self):
        client = FakeDisk()
        source = ROOT+'Займы/Пример'
        client.files = {source+'/Folder/one.txt': b'abc', source+'/folder/two.txt': b'def'}
        with tempfile.TemporaryDirectory() as tmp:
            result = disk.sync_source(source, tmp, client=client)
            self.assertEqual(result['status'], 'incomplete')
            self.assertIn('local_name_collision', [e['code'] for e in result['errors']])

    def test_midstream_failure_and_changed_remote_source_require_new_download(self):
        source = ROOT+'Займы/Пример'
        class BrokenStream(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError('connection interrupted at PRIVATE-SIGNED-URL')
                return super().read(1)
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeDisk()
            client.open_download = lambda _: BrokenStream(b'abc')
            result = disk.sync_source(source, tmp, client=client)
            self.assertEqual(result['status'], 'incomplete')
            self.assertFalse(any(e['status'] == 'verified' for e in result['files'].values()))
            self.assertFalse(list(Path(tmp).rglob('*.part')))
            self.assertNotIn('PRIVATE', Path(tmp, 'source-manifest.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeDisk()
            def changed(link):
                client.files[link] = b'changed'
                client.modified = '2026-11-05T12:00:00Z'
                return io.BytesIO(b'abc')
            client.open_download = changed
            result = disk.sync_source(source, tmp, client=client)
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['errors'][0]['code'], 'source_changed_during_download')

    def test_keychain_failure_invalidates_previous_complete_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk.sync_source('Займы/Пример', tmp, client=FakeDisk())
            with patch('subprocess.run', side_effect=OSError('PRIVATE')), redirect_stdout(io.StringIO()) as output:
                code = disk.main(['Займы/Пример', tmp])
            self.assertEqual(code, 2)
            result = json.loads(Path(tmp, 'source-manifest.json').read_text())
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['errors'][0]['code'], 'keychain_unavailable')
            self.assertNotIn('PRIVATE', output.getvalue())

    def test_hundred_item_pagination_and_missing_page_are_not_silent(self):
        client = FakeDisk()
        source = ROOT+'Займы/Пример'
        client.files = {source+f'/файл-{i:03}.pdf': b'abc' for i in range(103)}
        with tempfile.TemporaryDirectory() as tmp:
            result = disk.sync_source(source, tmp, client=client)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(len(result['files']), 103)
        original = client.resource
        def missing_page(path, limit=100, offset=0):
            result = original(path, limit, offset)
            if offset:
                result['_embedded']['items'] = []
            return result
        client.resource = missing_page
        with tempfile.TemporaryDirectory() as tmp:
            result = disk.sync_source(source, tmp, client=client)
            self.assertEqual(result['status'], 'incomplete')
            self.assertFalse(result['listing_complete'])
            self.assertIn('incomplete_listing', [e['code'] for e in result['errors']])

    def test_without_remote_version_markers_same_size_is_not_evidence_for_reuse(self):
        client = FakeDisk()
        original = client.resource
        def no_markers(*args, **kwargs):
            result = original(*args, **kwargs)
            for key in ('md5', 'sha256', 'modified'):
                result.pop(key, None)
            return result
        client.resource = no_markers
        with tempfile.TemporaryDirectory() as tmp:
            disk.sync_source('Займы/Пример', tmp, client=client)
            client.files[ROOT+'Займы/Пример/договор.pdf'] = b'xyz'
            second = disk.sync_source('Займы/Пример', tmp, client=client)
            entry = next(iter(second['files'].values()))
            self.assertEqual(Path(tmp, entry['local_path']).read_bytes(), b'xyz')
            self.assertFalse(entry['reused'])

    def test_download_link_cannot_send_credentials_to_an_untrusted_or_insecure_host(self):
        for url in ['http://downloader.disk.yandex.ru/file', 'https://example.org/file',
                    'https://user:password@downloader.disk.yandex.ru/file']:
            with self.subTest(url=url):
                client = disk.RestDisk('FAKE-OAUTH-TEST', opener=lambda request, timeout:
                                       io.BytesIO(json.dumps({'href': url}).encode()))
                with self.assertRaises(disk.FetchError):
                    client.download_link(ROOT+'IT/demo.txt')

    def test_rest_adapter_uses_read_only_shared_routes_and_never_forwards_oauth(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            if '/resources/download?' in request.full_url:
                return io.BytesIO(b'{"href":"https://downloader.disk.yandex.ru/test?secret=SIGNED"}')
            if request.full_url.startswith('https://downloader.'):
                return io.BytesIO(b'abc')
            return io.BytesIO(b'{"type":"file","path":"vd:/IuNVVCtUbELgzA/disk/IT/demo.txt","size":3}')
        client = disk.RestDisk('FAKE-OAUTH-TEST', opener=opener)
        meta = client.resource(ROOT+'IT/demo.txt')
        link = client.download_link(ROOT+'IT/demo.txt')
        with client.open_download(link) as response:
            self.assertEqual(response.read(), b'abc')
        self.assertEqual(meta['type'], 'file')
        self.assertEqual(parse_qs(urlsplit(calls[0].full_url).query)['path'], [ROOT+'IT/demo.txt'])
        self.assertEqual(calls[0].get_header('Authorization'), 'OAuth FAKE-OAUTH-TEST')
        self.assertIsNone(calls[-1].get_header('Authorization'))
        self.assertTrue(all(r.get_method() == 'GET' for r in calls))

    def test_rest_errors_have_distinct_safe_codes_and_bounded_retries(self):
        for status, code in [(401, 'unauthorized'), (403, 'forbidden'), (404, 'not_found'),
                             (423, 'locked'), (429, 'rate_limited'), (503, 'unavailable')]:
            with self.subTest(status=status):
                calls = []
                def opener(request, timeout):
                    calls.append(request)
                    raise HTTPError(request.full_url+'?secret=PRIVATE', status, 'PRIVATE', {}, None)
                client = disk.RestDisk('FAKE-OAUTH-TEST', opener=opener, sleep=lambda _: None)
                with self.assertRaises(disk.FetchError) as caught:
                    client.resource(ROOT+'Займы/Пример')
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.http_status, status)
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertEqual(len(calls), 3 if status in (429, 503) else 1)


if __name__ == '__main__':
    unittest.main()
