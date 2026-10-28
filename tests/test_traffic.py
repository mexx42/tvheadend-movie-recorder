import json
import os
import time

import pytest
import requests

from tvheadend_movie_recorder import traffic as tr
from tvheadend_movie_recorder.store import DataStore
from tvheadend_movie_recorder.tmdb import MAX_CACHE_DAYS, TMDbClient, purge_cache
from tvheadend_movie_recorder.tvheadend import TvheadendClient

SETTINGS = {'host': 'tvh', 'port': 9981, 'username': 'alice', 'password': 'hunter2'}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, json.dumps(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)


def test_sanitize_blanks_sensitive_fields_and_shortens_lists():
    data = {'entries': [{'title': 'Heat', 'owner': 'alice', 'filename': '/rec/a.ts'}] * 150,
            'password': 'x', 'note': 'n' * 2000}
    clean = tr.sanitize(data)
    assert clean['password'] == tr.REDACTED
    assert clean['entries'][0] == {'title': 'Heat', 'owner': tr.REDACTED, 'filename': tr.REDACTED}
    assert len(clean['entries']) == tr.MAX_LIST_ITEMS + 1 and 'omitted' in clean['entries'][-1]
    assert clean['note'].endswith('more characters>') and len(clean['note']) < 1100


def test_client_records_requests_and_answers_without_credentials(tmp_path, monkeypatch):
    log = tr.TrafficLog(tmp_path)
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: FakeResponse({'entries': [{'title': 'Heat'}]}))
    TvheadendClient(SETTINGS, log).content_types()
    text = log.read().decode()
    entry = json.loads(text)
    assert entry['request'] == 'GET /api/epg/content_type/list' and entry['status'] == 200
    assert entry['response'] == {'entries': [{'title': 'Heat'}]}
    assert 'hunter2' not in text and 'alice' not in text and 'tvh' not in text  # no host either


def test_client_records_failures_and_still_raises(tmp_path, monkeypatch):
    log = tr.TrafficLog(tmp_path)
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: FakeResponse({'error': 'denied'}, 403))
    with pytest.raises(requests.exceptions.HTTPError):
        TvheadendClient(SETTINGS, log).check()

    def refuse(*a, **kw):
        raise requests.exceptions.ConnectionError('refused')

    monkeypatch.setattr(requests, 'get', refuse)
    with pytest.raises(requests.exceptions.ConnectionError):
        TvheadendClient(SETTINGS, log).check()
    first, second = [json.loads(line) for line in log.read().decode().splitlines()]
    assert first['status'] == 403 and 'ConnectionError' in second['error']


def test_client_without_log_and_unwritable_log_do_not_break_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: FakeResponse({'sw_version': '4.3'}))
    assert TvheadendClient(SETTINGS).check() == '4.3'
    blocked = tmp_path / 'file'
    blocked.write_text('x')  # a file where the directory should be: every write fails
    assert TvheadendClient(SETTINGS, tr.TrafficLog(blocked / 'sub')).check() == '4.3'


def test_log_rotates_and_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, 'MAX_FILE_BYTES', 2000)
    log = tr.TrafficLog(tmp_path)
    for i in range(100):
        log.record({'request': f'GET /{i}', 'pad': 'x' * 100})
    assert log.size() <= 2 * 2000
    lines = [json.loads(line)['request'] for line in log.read().decode().splitlines()]
    assert lines[-1] == 'GET /99' and lines == sorted(lines, key=lambda s: int(s.split('/')[1]))
    assert (tmp_path / 'tvheadend-traffic.jsonl').stat().st_mode & 0o077 == 0
    log.clear()
    assert log.read() == b'' and log.size() == 0


def test_tmdb_cache_never_outlives_six_months(tmp_path):
    client = TMDbClient({'api_key': 'k', 'language': 'de-DE', 'cache_expiry_days': 9999}, tmp_path)
    assert client.expiry_seconds == MAX_CACHE_DAYS * 86400
    old, fresh = tmp_path / 'old.json', tmp_path / 'fresh.json'
    old.write_text('{}'); fresh.write_text('{}')
    past = time.time() - (MAX_CACHE_DAYS + 1) * 86400
    os.utime(old, (past, past))
    purge_cache(tmp_path, 9999)
    assert not old.exists() and fresh.exists()


def test_traffic_log_is_downloadable_and_deletable_in_the_web_ui(tmp_path):
    import re
    from tvheadend_movie_recorder.web import create_app

    class Sch:
        last_summary = None

    store = DataStore(tmp_path)
    client = create_app(store, Sch()).test_client()
    html = client.get('/setup').get_data(as_text=True)
    tok = re.search(r'csrf_token" value="([^"]+)"', html).group(1)
    client.post('/setup', data={'csrf_token': tok, 'username': 'a', 'password': 'password1', 'password2': 'password1'})
    assert client.get('/settings/traffic.jsonl').status_code == 302  # nothing recorded yet
    store.traffic.record({'request': 'GET /api/serverinfo', 'status': 200})
    r = client.get('/settings/traffic.jsonl')
    assert r.status_code == 200 and b'serverinfo' in r.data
    assert 'attachment' in r.headers['Content-Disposition']
    tok = re.search(r'csrf_token" value="([^"]+)"', client.get('/settings').get_data(as_text=True)).group(1)
    client.post('/settings/traffic/clear', data={'csrf_token': tok})
    assert store.traffic.read() == b''
