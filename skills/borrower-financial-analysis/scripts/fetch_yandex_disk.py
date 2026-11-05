#!/usr/bin/env python3
"""Read a selected shared-disk source into a versioned local work directory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import subprocess
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

VD_HASH = 'IuNVVCtUbELgzA'
ORG_ID = '8117507'
ROOT = 'vd:' + VD_HASH + ':disk:/'


class FetchError(ValueError):
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


HTTP_CODES = {401: 'unauthorized', 403: 'forbidden', 404: 'not_found',
              423: 'locked', 429: 'rate_limited', 503: 'unavailable'}


def _download_url(value):
    url = urlsplit(value)
    host = url.hostname or ''
    if (url.scheme != 'https' or url.username or url.password or url.fragment
            or url.port not in (None, 443)
            or not any(host == d or host.endswith('.'+d) for d in ('yandex.ru', 'yandex.net', 'yandex.com'))):
        raise FetchError('unsafe_download_url')
    return value


class _ReadOnlyRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.has_header('Authorization'):
            raise FetchError('api_redirect_not_allowed')
        _download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class RestDisk:
    """Read-only shared-disk adapter; tokens and signed links never enter manifests."""
    def __init__(self, token, *, opener=None, sleep=time.sleep):
        self._token = token
        self._open = opener or build_opener(_ReadOnlyRedirect()).open
        self._sleep = sleep

    def _request(self, endpoint, params):
        url = 'https://cloud-api.yandex.net/v1/disk'+endpoint+'?'+urlencode(params)
        request = Request(url, headers={'Authorization': 'OAuth '+self._token, 'Accept': 'application/json'})
        for attempt in range(3):
            try:
                with self._open(request, timeout=30) as response:
                    value = json.load(response)
                if not isinstance(value, dict):
                    raise FetchError('invalid_api_response')
                return value
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if code in (429, 503) and attempt < 2:
                    self._sleep(2**attempt)
                    continue
                raise FetchError(HTTP_CODES.get(code, 'http_error'), code) from None
            except (URLError, OSError, HTTPException):
                raise FetchError('network_error') from None
            except (ValueError, TypeError):
                raise FetchError('invalid_api_response') from None

    def resource(self, path, limit=100, offset=0):
        return self._request('/virtual-disks/resources',
                             {'path': normalize_source(path), 'limit': limit, 'offset': offset, 'sort': 'name'})

    def download_link(self, path):
        link = self._request('/virtual-disks/resources/download', {'path': normalize_source(path)})
        if link.get('method', 'GET') != 'GET' or not isinstance(link.get('href'), str):
            raise FetchError('invalid_download_link')
        return _download_url(link['href'])

    def open_download(self, link):
        # This GET has no OAuth header, including after permitted HTTPS redirects.
        try:
            return self._open(Request(_download_url(link)), timeout=60)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise FetchError(HTTP_CODES.get(code, 'download_http_error'), code) from None
        except (URLError, OSError, HTTPException):
            raise FetchError('download_network_error') from None


def normalize_source(value):
    """Accept an explicit path or folder URL; never fall back to personal disk."""
    value = str(value).strip()
    if value.startswith('https://'):
        url = urlsplit(value)
        prefix = '/client/vd/' + VD_HASH + '/disk/'
        if url.hostname != 'disk.360.yandex.ru' or url.username or url.password or not url.path.startswith(prefix):
            raise FetchError('invalid_source_url')
        value = '/'.join(unquote(p) for p in url.path[len(prefix):].split('/'))
    elif value.startswith('vd:/'+VD_HASH+'/disk/'):
        value = value[len('vd:/'+VD_HASH+'/disk/'):]
    elif value.startswith(ROOT):
        value = value[len(ROOT):]
    elif ':' in value or value.startswith('/'):
        raise FetchError('wrong_disk_or_path')
    value = value.rstrip('/')
    parts = value.split('/')
    if not value or any(p in ('', '.', '..') or '\\' in p or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in parts):
        raise FetchError('unsafe_source_path')
    return ROOT + value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _local(root, relative):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in ('.', '..') for p in parts):
        raise FetchError('unsafe_local_path')
    target = root
    for part in parts:
        target = target / part
        if target.is_symlink():
            raise FetchError('local_symlink')
    return target


def _write_manifest(root, manifest):
    target = _local(root, 'source-manifest.json')
    temp = _local(root, '.manifest-'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _hashes(path):
    md5, sha = hashlib.md5(), hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            size += len(chunk)
            md5.update(chunk)
            sha.update(chunk)
    return size, md5.hexdigest(), sha.hexdigest()


def sync_source(source, workdir, *, client, page_size=100, max_files=10000,
                max_bytes=2*1024**3):
    """Return and persist complete/empty/incomplete provenance, never remote text errors.

    The injected client is a read-only disk adapter: resource, download_link,
    open_download. Only verified entries from THIS manifest are analysis inputs.
    """
    source = normalize_source(source)
    if not 1 <= page_size <= 100 or max_files < 1 or max_bytes < 0:
        raise FetchError('invalid_limits')
    root = Path(workdir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # flock releases on process death; a killed run does not leave a stale lock.
    import fcntl
    lockpath = _local(root, '.source-fetch.lock')
    with lockpath.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FetchError('run_in_progress') from None
        return _sync(source, root, client, page_size, max_files, max_bytes)


def _sync(source, root, client, page_size, max_files, max_bytes):
    manifest_path = _local(root, 'source-manifest.json')
    old = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    if old and (old.get('source') != source or old.get('schema_version') != 'shared-disk-sources-1.0'):
        raise FetchError('workdir_source_mismatch')
    run = uuid.uuid4().hex
    if old:
        history = _local(root, 'history/'+run+'.json')
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding='utf-8')
    result = {'schema_version': 'shared-disk-sources-1.0', 'org_id': ORG_ID,
              'vd_hash': VD_HASH, 'source': source, 'run_id': run,
              'started_at': _now(), 'status': 'incomplete', 'listing_complete': False,
              'files': {}, 'errors': []}
    for path, entry in old.get('files', {}).items():
        result['files'][path] = dict(entry, status='not_seen')
    _write_manifest(root, result)

    def error(path, exc):
        result['errors'].append({'path': path,
                                 'code': exc.code if isinstance(exc, FetchError) else 'local_or_network_error',
                                 'http_status': getattr(exc, 'http_status', None)})

    discovered = {}
    used_names = {}

    def canonical(raw):
        path = raw.get('path')
        if not isinstance(path, str):
            raise FetchError('invalid_remote_metadata')
        path = normalize_source(path)
        if path != source and not path.startswith(source+'/'):
            raise FetchError('outside_selected_source')
        return path

    def visit(raw, depth=0):
        path = canonical(raw)
        if depth > 64:
            raise FetchError('folder_depth_limit')
        relative = path[len(source)+1:] if path != source else path.rsplit('/', 1)[1]
        parts = relative.split('/')
        for index in range(1, len(parts)+1):
            prefix = '/'.join(parts[:index])
            collision = unicodedata.normalize('NFC', prefix).casefold()
            if collision in used_names and used_names[collision] != prefix:
                raise FetchError('local_name_collision')
            used_names[collision] = prefix
        if raw.get('type') == 'file':
            if path in discovered:
                raise FetchError('listing_changed_or_repeated')
            if len(discovered) >= max_files:
                raise FetchError('file_count_limit')
            discovered[path] = relative
            return
        if raw.get('type') != 'dir':
            raise FetchError('invalid_remote_type')
        offset, total = 0, None
        while True:
            page = client.resource(path, limit=page_size, offset=offset)
            embedded = page.get('_embedded', {})
            items, count = embedded.get('items'), embedded.get('total')
            if canonical(page) != path or not isinstance(items, list) or type(count) is not int or count < 0:
                raise FetchError('invalid_listing')
            if embedded.get('offset') != offset or total is not None and total != count:
                raise FetchError('listing_changed_or_repeated')
            total = count
            if len(items) > page_size or offset+len(items) > total or not items and offset < total:
                raise FetchError('incomplete_listing')
            for item in items:
                child = canonical(item)
                if child.rsplit('/', 1)[0] != path:
                    raise FetchError('outside_selected_folder')
                visit(item, depth+1)
            offset += len(items)
            if offset == total:
                break

    try:
        visit(client.resource(source))
        result['listing_complete'] = True
    except (FetchError, OSError, ValueError, TypeError, KeyError) as exc:
        error(source, exc)

    spent = 0
    for path, relative in discovered.items():
        prior = old.get('files', {}).get(path, {})
        entry = {'remote_path': path, 'original_name': path.rsplit('/', 1)[1],
                 'relative_path': relative, 'status': 'failed'}
        try:
            meta = client.resource(path)
            if canonical(meta) != path or meta.get('type') != 'file' or type(meta.get('size')) is not int or meta['size'] < 0:
                raise FetchError('invalid_remote_metadata')
            for name in ('md5', 'sha256'):
                value = meta.get(name)
                if value is not None and not re.fullmatch('[0-9a-fA-F]{'+str(32 if name == 'md5' else 64)+'}', value):
                    raise FetchError('invalid_remote_checksum')
            fingerprint = {k: meta.get(k) for k in ('resource_id', 'modified', 'size', 'md5', 'sha256')}
            entry.update({'resource_id': meta.get('resource_id'), 'modified': meta.get('modified'),
                          'size': meta['size'], 'remote_md5': meta.get('md5'),
                          'remote_sha256': meta.get('sha256'), 'fingerprint': fingerprint})
            version_known = any(fingerprint.get(k) for k in ('modified', 'md5', 'sha256'))
            if version_known and prior.get('fingerprint') == fingerprint and prior.get('local_path'):
                previous_file = _local(root, prior['local_path'])
                if previous_file.is_file() and _hashes(previous_file) == (prior['size'], prior['md5'], prior['sha256']):
                    entry.update({k: prior[k] for k in ('local_path', 'md5', 'sha256', 'downloaded_at')})
                    entry.update(status='verified', checked_at=_now(), reused=True)
                    result['files'][path] = entry
                    _write_manifest(root, result)
                    continue
            if spent+meta['size'] > max_bytes:
                raise FetchError('download_size_limit')
            local = _local(root, 'originals/'+run+'/'+relative)
            local.parent.mkdir(parents=True, exist_ok=True)
            part = _local(root, str(local.relative_to(root))+'.'+uuid.uuid4().hex+'.part')
            try:
                link = client.download_link(path)
                with client.open_download(link) as response, part.open('xb') as stream:
                    count = 0
                    while True:
                        chunk = response.read(1024*1024)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > meta['size']:
                            raise FetchError('download_size_mismatch')
                        stream.write(chunk)
                size, md5, sha = _hashes(part)
                if size != meta['size']:
                    raise FetchError('download_size_mismatch')
                if meta.get('md5') and md5 != meta['md5'].lower() or meta.get('sha256') and sha != meta['sha256'].lower():
                    raise FetchError('download_checksum_mismatch')
                after = client.resource(path)
                if canonical(after) != path or after.get('type') != 'file' or any(after.get(k) != v for k, v in fingerprint.items()):
                    raise FetchError('source_changed_during_download')
                os.replace(part, local)
                spent += size
                entry.update(local_path=str(local.relative_to(root)), md5=md5, sha256=sha,
                             downloaded_at=_now(), checked_at=_now(), status='verified', reused=False)
            finally:
                part.unlink(missing_ok=True)
        except (FetchError, OSError, HTTPException, ValueError, TypeError, KeyError) as exc:
            error(path, exc)
            entry['error'] = result['errors'][-1]['code']
        result['files'][path] = entry
        _write_manifest(root, result)
    result['finished_at'] = _now()
    result['status'] = ('empty' if not discovered else 'complete') if not result['errors'] else 'incomplete'
    _write_manifest(root, result)
    return result


class _UnavailableDisk:
    def __init__(self, error):
        self.error = error

    def resource(self, *args, **kwargs):
        raise self.error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='Папка или файл общего диска ВБетон, полный путь или ссылка')
    parser.add_argument('workdir', help='Отдельная локальная рабочая папка получения исходников')
    parser.add_argument('--keychain-account', default='law@vbeton.ru')
    parser.add_argument('--page-size', type=int, default=100)
    parser.add_argument('--max-files', type=int, default=10000)
    parser.add_argument('--max-bytes', type=int, default=2*1024**3)
    args = parser.parse_args(argv)
    try:
        # Validate before opening Keychain. Neither secret nor signed URL is a CLI argument.
        normalize_source(args.source)
        try:
            credential = subprocess.run(['/usr/bin/security', 'find-generic-password',
                                         '-s', 'Yandex.Disk.MCP', '-a', args.keychain_account, '-w'],
                                        capture_output=True, check=True, text=True, timeout=30).stdout.strip()
            client = RestDisk(credential) if credential else _UnavailableDisk(FetchError('keychain_empty'))
        except (OSError, subprocess.SubprocessError):
            client = _UnavailableDisk(FetchError('keychain_unavailable'))
        result = sync_source(args.source, args.workdir, client=client,
                             page_size=args.page_size, max_files=args.max_files, max_bytes=args.max_bytes)
        summary = {key: result[key] for key in ('status', 'source', 'listing_complete', 'errors')}
        summary['verified_files'] = sum(e['status'] == 'verified' for e in result['files'].values())
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if result['status'] in ('complete', 'empty') else 2
    except (FetchError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'incomplete', 'code': exc.code if isinstance(exc, FetchError)
                          else 'local_or_input_error', 'http_status': getattr(exc, 'http_status', None)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
