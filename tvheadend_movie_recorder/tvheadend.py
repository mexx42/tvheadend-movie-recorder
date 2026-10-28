"""Minimal Tvheadend HTTP API client."""

import time

import requests

TIMEOUT = 15
PAGE_SIZE = 1000


class TvheadendClient:
    def __init__(self, settings, traffic=None):
        self.base_url = f"http://{settings['host']}:{settings['port']}"
        self.auth = (settings['username'], settings['password'])
        self.dvr_config_uuid = settings.get('dvr_config_uuid') or ''
        self.traffic = traffic  # optional TrafficLog; None = no capture

    def _get(self, endpoint, **params):
        started = time.monotonic()
        try:
            response = requests.get(f'{self.base_url}/{endpoint}', auth=self.auth,
                                    params=params, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            self._capture(endpoint, params, started, error=f'{type(e).__name__}: {e}')
            raise
        self._capture(endpoint, params, started, response=response)
        response.raise_for_status()
        return response.json()

    def _capture(self, endpoint, params, started, response=None, error=None):
        if self.traffic is None:
            return
        entry = {'request': f'GET /{endpoint}', 'params': params,
                 'seconds': round(time.monotonic() - started, 3)}
        if error:
            entry['error'] = error
        else:
            entry['status'] = response.status_code
            try:
                entry['response'] = response.json()
            except ValueError:
                entry['response_text'] = response.text
        try:
            self.traffic.record(entry)
        except OSError:
            pass  # a full or read-only disk must never break recording

    def check(self):
        """Raise on connection/auth problems; return the server's software version."""
        return self._get('api/serverinfo').get('sw_version', '?')

    def channel_names(self):
        entries = self._get('api/channel/grid', limit=5000)['entries']
        return sorted({c['name'] for c in entries if c.get('enabled', True) and c.get('name')},
                      key=str.lower)

    def content_types(self):
        """List of {'key': int, 'val': str} EPG genre entries."""
        return self._get('api/epg/content_type/list', full=1)['entries']

    def genres(self):
        """Genres to pick from in Settings, see ``group_genres``."""
        return group_genres(self.content_types())

    def epg_events(self, days):
        """All EPG events that have not ended yet and start within ``days`` days."""
        now = time.time()
        horizon = now + days * 86400
        events, offset = [], 0
        while True:
            page = self._get('api/epg/events/grid', start=offset, limit=PAGE_SIZE)
            entries = page['entries']
            events.extend(entries)
            offset += len(entries)
            if not entries or offset >= page.get('totalCount', offset):
                break
        return [e for e in events
                if e.get('stop', 0) > now and e.get('start', 0) < horizon]

    def dvr_titles(self):
        """Lower-cased titles of all existing DVR entries."""
        entries = self._get('api/dvr/entry/grid', limit=5000)['entries']
        return {(e.get('disp_title') or '').lower() for e in entries}

    def schedule(self, event_id, comment):
        params = {'event_id': str(event_id), 'comment': comment}
        if self.dvr_config_uuid:
            params['config_uuid'] = self.dvr_config_uuid
        self._get('api/dvr/entry/create_by_event', **params)


def group_genres(entries):
    """Genre entries grouped by name, in id order: ``[{'name': 'Movie / Drama', 'ids': [16]}, ...]``.

    Tvheadend lists some names under several ids (e.g. twelve 'News / Current Affairs' codes) and
    events carry the ids, so a group stands for all of its ids. Entries without a name are skipped.
    """
    groups = {}
    for entry in sorted(entries, key=lambda e: e['key']):
        name = (entry.get('val') or '').strip()
        if name:
            groups.setdefault(name, []).append(entry['key'])
    return [{'name': name, 'ids': ids} for name, ids in groups.items()]


def describe_error(error):
    """Human-readable message for a failed Tvheadend request, with a hint for common causes."""
    response = getattr(error, 'response', None)
    status = getattr(response, 'status_code', None)
    if status == 401:
        return 'Tvheadend rejected the username or password (401).'
    if status == 403:
        return ('Tvheadend refused the request (403). The login may be right, but Tvheadend only '
                'accepts it from certain networks: in Tvheadend open Configuration > Users > '
                'Access Entries and add this machine\'s network, e.g. 172.16.0.0/12 when running '
                'in Docker, to "Allowed networks". Also check the user may use the HTTP API.')
    if isinstance(error, requests.exceptions.ConnectionError):
        return ('Cannot connect to Tvheadend. Check host and port; inside Docker, "localhost" is '
                'the container itself.')
    if isinstance(error, requests.exceptions.Timeout):
        return 'Tvheadend did not answer in time.'
    return str(error)
