import time

import pytest

from tvheadend_movie_recorder import recorder as rec
from tvheadend_movie_recorder.store import DataStore


def event(title='Movie', genre=(16,), channel='ORF1', **kw):
    now = int(time.time())
    return {'title': title, 'genre': list(genre), 'channelName': channel, 'eventId': 7,
            'start': now + 3600, 'stop': now + 7200, **kw}


def test_group_genres_merges_ids_that_share_a_name_and_skips_unnamed_entries():
    from tvheadend_movie_recorder.tvheadend import group_genres
    entries = [{'key': 18, 'val': 'Adventure / Western / War'}, {'key': 0, 'val': ''},
               {'key': 16, 'val': 'Movie / Drama'}, {'key': 26, 'val': 'Movie / drama '},
               {'key': 32, 'val': 'News'}, {'key': 37, 'val': 'News'}]
    assert group_genres(entries) == [
        {'name': 'Movie / Drama', 'ids': [16]},
        {'name': 'Adventure / Western / War', 'ids': [18]},
        {'name': 'Movie / drama', 'ids': [26]},      # different spelling = its own entry
        {'name': 'News', 'ids': [32, 37]}]


def test_filter_requires_all_genres_to_be_movie_genres():
    events = [event('a', genre=(16,)), event('b', genre=(16, 32)), event('c', genre=())]
    assert [e['title'] for e in rec.filter_events(events, {16}, [])] == ['a']


def test_filter_excludes_channels_by_exact_name_case_insensitively():
    events = [event('a', channel='ORF1'), event('b', channel='ORF1 HD'), event('c', channel='orf1')]
    kept = rec.filter_events(events, {16}, ['ORF1'])
    assert [e['title'] for e in kept] == ['b']


def test_filter_with_no_exclusions_keeps_all_channels():
    events = [event('a', channel='ORF1'), event('b', channel='ZDF')]
    assert len(rec.filter_events(events, {16}, [])) == 2


def test_blacklist_matches_title_substring_only():
    wl = {'blacklist': ['soko']}
    assert 'title' in rec.is_blacklisted(event('SOKO Donau'), wl)
    assert rec.is_blacklisted(event('Heat', channel='soko tv'), wl) is None


def test_name_matching_ignores_case():
    assert rec.match_description(event(description='mit bruce WILLIS'), ['Bruce Willis']) == 'Bruce Willis'
    info = {'directors': ['m night shyamalan'], 'cast': [], 'producers': []}
    assert rec.match_people(info, ['M Night Shyamalan']) == 'M Night Shyamalan'
    assert rec.match_people(info, ['Someone Else']) is None


class FakeTVH:
    def __init__(self, events, existing=()):
        self.events, self.existing, self.scheduled = events, set(existing), []

    def epg_events(self, days):
        return self.events

    def dvr_titles(self):
        return set(self.existing)

    def schedule(self, event_id, comment):
        self.scheduled.append((event_id, comment))


@pytest.fixture
def store(tmp_path):
    s = DataStore(tmp_path)
    s.update_config(lambda c: c['tvheadend'].update(host='tvh'))
    s.update_watchlist(lambda w: w['names'].extend(['Bruce Willis']))
    return s


def run_with(store, monkeypatch, fake):
    monkeypatch.setattr(rec, 'TvheadendClient', lambda settings, traffic=None: fake)
    return rec.Recorder(store).run()


def test_run_schedules_description_match_once(store, monkeypatch):
    fake = FakeTVH([event('Die Hard', description='Action with Bruce Willis'),
                    event('Other', description='nobody')])
    summary = run_with(store, monkeypatch, fake)
    assert [e['title'] for e in summary['scheduled']] == ['Die Hard'] and summary['error'] is None
    assert fake.scheduled == [(7, '[Bruce Willis]')]
    assert store.load_recorded()['movies'][0]['matched_name'] == 'Bruce Willis'


def test_run_skips_existing_blacklisted_and_excluded_channels(store, monkeypatch):
    store.update_watchlist(lambda w: w['blacklist'].append('blocked'))
    store.update_config(lambda c: c['recorder'].update(excluded_channels=['zdf']))
    desc = {'description': 'Bruce Willis'}
    fake = FakeTVH([event('Booked', **desc), event('Blocked movie', **desc),
                    event('Elsewhere', channel='ZDF', **desc)], existing={'booked'})
    assert run_with(store, monkeypatch, fake)['scheduled'] == []
    assert fake.scheduled == []


def test_run_reports_missing_host_and_unmatched_genres(store, monkeypatch):
    store.update_config(lambda c: c['recorder'].update(genres=[]))
    assert 'genres' in run_with(store, monkeypatch, FakeTVH([]))['error']
    store.update_config(lambda c: c['tvheadend'].update(host=''))
    assert 'host' in run_with(store, monkeypatch, FakeTVH([]))['error']


def test_run_matches_via_tmdb_people_and_stores_imdb_id(store, monkeypatch):
    store.update_config(lambda c: c['tmdb'].update(api_key='k'))

    class FakeTMDb:
        def __init__(self, *a): pass
        def movie_info(self, title):
            return {'directors': [], 'cast': ['bruce willis'], 'producers': [], 'imdb_id': '0112864'}

    monkeypatch.setattr(rec, 'TMDbClient', FakeTMDb)
    summary = run_with(store, monkeypatch, FakeTVH([event('Hard Target')]))
    assert [e['title'] for e in summary['scheduled']] == ['Hard Target']
    assert store.load_recorded()['movies'][0]['imdb_id'] == '0112864'


def test_tvheadend_errors_get_actionable_hints():
    import requests
    from tvheadend_movie_recorder.tvheadend import describe_error

    def http_error(status):
        response = requests.Response()
        response.status_code = status
        return requests.exceptions.HTTPError(response=response)

    assert 'username or password' in describe_error(http_error(401))
    assert 'Access Entries' in describe_error(http_error(403))
    assert 'Cannot connect' in describe_error(requests.exceptions.ConnectionError())
    assert describe_error(http_error(500)) == str(http_error(500))


def test_run_passes_the_traffic_log_only_when_capture_is_enabled(store, monkeypatch):
    seen = []
    fake = FakeTVH([])
    monkeypatch.setattr(rec, 'TvheadendClient', lambda settings, traffic=None: seen.append(traffic) or fake)
    rec.Recorder(store).run()
    store.update_config(lambda c: c['tvheadend'].update(capture_traffic=True))
    rec.Recorder(store).run()
    assert seen == [None, store.traffic]
