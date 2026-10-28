import re

import pytest

from tvheadend_movie_recorder.store import DataStore
from tvheadend_movie_recorder.web import create_app


class StubScheduler:
    last_summary = None
    runs = 0

    def run_now(self):
        self.runs += 1


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path)


@pytest.fixture
def client(store):
    app = create_app(store, StubScheduler())
    return app.test_client()


def token(client, path):
    html = client.get(path, follow_redirects=True).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def create_admin(client, password='secret-pass'):
    return client.post('/setup', data={'csrf_token': token(client, '/setup'), 'username': 'admin',
                                       'password': password, 'password2': password})


@pytest.fixture
def logged_in(client):
    create_admin(client)
    return client


def test_everything_redirects_to_setup_until_admin_exists(client):
    assert client.get('/').headers['Location'].endswith('/setup')
    assert client.get('/settings').headers['Location'].endswith('/setup')
    assert create_admin(client).headers['Location'].endswith('/settings')
    assert client.get('/setup').headers['Location'].endswith('/')


def test_setup_rejects_short_or_mismatched_passwords(client, store):
    tok = token(client, '/setup')
    form = {'csrf_token': tok, 'username': 'admin'}
    assert b'at least 8' in client.post('/setup', data={**form, 'password': 'short', 'password2': 'short'}).data
    assert b'do not match' in client.post('/setup', data={**form, 'password': 'longenough1', 'password2': 'x'}).data
    assert store.load_auth() is None


def test_post_without_csrf_token_is_rejected(logged_in, store):
    assert logged_in.post('/list/names/add', data={'value': 'X'}).status_code == 400
    assert store.load_watchlist()['names'] == []


def test_login_logout_and_lockout(client):
    create_admin(client)
    client.post('/logout', data={'csrf_token': token(client, '/')})
    assert client.get('/').headers['Location'].endswith('/login')
    tok = token(client, '/login')
    bad = {'csrf_token': tok, 'username': 'admin', 'password': 'wrong'}
    for _ in range(5):
        assert client.post('/login', data=bad).status_code == 200
    good = {'csrf_token': tok, 'username': 'admin', 'password': 'secret-pass'}
    assert client.post('/login', data=good).status_code == 429  # locked even with right password


def test_login_succeeds_with_correct_credentials(client):
    create_admin(client)
    client.post('/logout', data={'csrf_token': token(client, '/')})
    tok = token(client, '/login')
    r = client.post('/login', data={'csrf_token': tok, 'username': 'admin', 'password': 'secret-pass'})
    assert r.status_code == 302 and client.get('/').status_code == 200


def test_watchlist_add_is_case_insensitive_unique_and_removable(logged_in, store):
    tok = token(logged_in, '/')
    for value in ['Bruce Willis', 'bruce willis', '  ']:
        logged_in.post('/list/names/add', data={'csrf_token': tok, 'value': value})
    assert store.load_watchlist()['names'] == ['Bruce Willis']
    logged_in.post('/list/names/remove', data={'csrf_token': tok, 'value': 'Bruce Willis'})
    assert store.load_watchlist()['names'] == []
    assert logged_in.post('/list/bogus/add', data={'csrf_token': tok, 'value': 'x'}).status_code == 404


def test_settings_blank_secrets_keep_existing_values(logged_in, store):
    store.update_config(lambda c: (c['tvheadend'].update(password='pw'), c['tmdb'].update(api_key='key')))
    tok = token(logged_in, '/settings')
    logged_in.post('/settings', data={'csrf_token': tok, 'action': 'save', 'tvh_host': 'box',
                                      'tvh_port': '9981', 'tvh_password': '', 'tmdb_api_key': '',
                                      'rec_genres': ['16', '26,27'], 'rec_genres_shown': ['16', '17', '26,27']})
    config = store.load_config()
    assert config['tvheadend']['password'] == 'pw' and config['tmdb']['api_key'] == 'key'
    assert config['tvheadend']['host'] == 'box'
    assert config['recorder']['enabled'] is False  # unchecked checkbox is not submitted
    assert (store.data_dir / 'config.yaml').stat().st_mode & 0o077 == 0  # not world-readable


def test_settings_unticked_channels_are_excluded_and_unlisted_ones_kept(logged_in, store):
    store.update_config(lambda c: c['recorder'].update(excluded_channels=['Gone TV', 'ZDF']))
    tok = token(logged_in, '/settings')
    # form lists ORF1, ORF2, ZDF; ORF1 and ZDF are ticked, ORF2 is not; 'Gone TV' was not listed
    logged_in.post('/settings', data={'csrf_token': tok, 'action': 'save', 'tvh_host': 'box',
                                      'rec_channels_shown': ['ORF1', 'ORF2', 'ZDF'],
                                      'rec_channels': ['ORF1', 'ZDF']})
    assert store.load_config()['recorder']['excluded_channels'] == ['Gone TV', 'ORF2']


def test_remove_from_channel_list_excludes_once_case_insensitively(logged_in, store):
    tok = token(logged_in, '/')
    for value in ['ORF1', 'orf1', ' ']:
        logged_in.post('/recorded/exclude-channel', data={'csrf_token': tok, 'value': value})
    assert store.load_config()['recorder']['excluded_channels'] == ['ORF1']


def test_change_password_requires_current_password(logged_in, store):
    tok = token(logged_in, '/settings')
    data = {'csrf_token': tok, 'old_password': 'wrong', 'new_password': 'brand-new-pw', 'new_password2': 'brand-new-pw'}
    logged_in.post('/settings/password', data=data)
    first = store.load_auth()['password_hash']
    logged_in.post('/settings/password', data={**data, 'old_password': 'secret-pass'})
    assert store.load_auth()['password_hash'] != first


def test_recorded_movies_are_paged_by_month(logged_in, store):
    for title, date in [('Jan', '2026-01-05T10:00:00'), ('Mar', '2026-03-02T10:00:00'),
                        ('Dec', '2025-12-31T23:00:00'), ('Broken', 'garbage')]:
        store.add_recorded({'title': title, 'date_scheduled': date, 'channel_name': 'ORF1'})

    def titles(url):
        html = logged_in.get(url).get_data(as_text=True).split('Recorded movies')[1]
        return re.findall(r'data-label="Title">([^<]+)', html)

    assert titles('/') == ['Mar']                       # newest month by default
    assert titles('/?month=2025-12') == ['Dec']
    assert titles('/?month=undated') == ['Broken']      # missing/invalid dates are listed last
    assert titles('/?month=nonsense') == ['Mar']        # unknown month falls back to the newest
    tok = token(logged_in, '/')
    r = logged_in.post('/recorded/blacklist-title', data={'csrf_token': tok, 'value': 'Jan', 'month': '2026-01'})
    assert r.headers['Location'].endswith('/?month=2026-01#recorded')  # actions stay on the viewed month


def test_settings_accept_valid_run_time_and_ignore_invalid_ones(logged_in, store):
    tok = token(logged_in, '/settings')
    base = {'csrf_token': tok, 'action': 'save', 'tvh_host': 'box', 
            'app_public_url': 'http://nas:5000/'}
    logged_in.post('/settings', data={**base, 'rec_run_at': '21:45'})
    config = store.load_config()
    assert config['recorder']['run_at'] == '21:45'
    assert config['app']['public_url'] == 'http://nas:5000'   # trailing slash dropped
    for bad in ['25:00', '7:5', 'soon', '']:
        logged_in.post('/settings', data={**base, 'rec_run_at': bad})
        assert store.load_config()['recorder']['run_at'] == '21:45'


def test_dashboard_shows_the_last_run_written_by_any_process(logged_in, store):
    assert b'No run yet.' in logged_in.get('/').data
    store.save_last_run({'started': '2026-10-28T03:00:01', 'events': 9, 'error': None,
                         'scheduled': [{'title': 'Die Hard', 'channel': 'ORF1', 'start': 1, 'matched': 'X'}]})
    page = logged_in.get('/').get_data(as_text=True)
    assert '2026-10-28 03:00:01' in page and 'Die Hard' in page and 'every day at 03:00' in page


def test_settings_genre_ticks_replace_shown_genres_and_keep_unlisted_ones(logged_in, store):
    store.update_config(lambda c: c['recorder'].update(genres=[16, 17, 99]))
    tok = token(logged_in, '/settings')
    # the form listed 16, 17 and the merged group 26-27; 17 was unticked, 26-27 ticked; 99 was not listed
    logged_in.post('/settings', data={'csrf_token': tok, 'action': 'save', 'tvh_host': 'box',
                                      'rec_genres_shown': ['16', '17', '26,27'],
                                      'rec_genres': ['16', '26,27']})
    assert store.load_config()['recorder']['genres'] == [16, 26, 27, 99]


def test_settings_page_lists_genres_from_tvheadend_with_movie_genres_first(logged_in, store, monkeypatch):
    from tvheadend_movie_recorder import web
    store.update_config(lambda c: (c['tvheadend'].update(host='box'), c['recorder'].update(genres=[16, 32, 37])))
    monkeypatch.setattr(web.TvheadendClient, 'channel_names', lambda self: ['ORF1'])
    monkeypatch.setattr(web.TvheadendClient, 'content_types', lambda self: [
        {'key': 16, 'val': 'Movie / Drama'}, {'key': 32, 'val': 'News'}, {'key': 37, 'val': 'News'},
        {'key': 64, 'val': 'Sports'}])
    html = logged_in.get('/settings').get_data(as_text=True)
    boxes = re.findall(r'name="rec_genres" value="([^"]+)"\s*(checked)?', html)
    assert boxes == [('16', 'checked'), ('32,37', 'checked'), ('64', '')]   # a group is ticked only if all its ids are
    assert html.index('value="16"') < html.index('<details>') < html.index('value="32,37"')


def test_settings_page_shows_one_error_when_tvheadend_is_unreachable(logged_in, store, monkeypatch):
    import requests
    from tvheadend_movie_recorder import web
    store.update_config(lambda c: c['tvheadend'].update(host='box'))

    def refuse(self):
        raise requests.exceptions.ConnectionError('refused')

    monkeypatch.setattr(web.TvheadendClient, 'channel_names', refuse)
    html = logged_in.get('/settings').get_data(as_text=True)
    assert html.count('Could not load channels and genres from Tvheadend') == 1
