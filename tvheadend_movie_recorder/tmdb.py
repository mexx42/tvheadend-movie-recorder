"""TMDb lookup (people and IMDb id for a movie title) with an on-disk JSON cache."""

import hashlib
import json
import time

import requests

BASE_URL = 'https://api.themoviedb.org/3'
TIMEOUT = 15
# TMDb's API terms forbid caching their data for longer than 6 months.
MAX_CACHE_DAYS = 180


def cache_expiry_seconds(days):
    return min(days, MAX_CACHE_DAYS) * 86400


def purge_cache(cache_dir, days):
    """Delete cached lookups older than the cache expiry (never more than MAX_CACHE_DAYS)."""
    cutoff = time.time() - cache_expiry_seconds(days)
    for path in cache_dir.glob('*.json'):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


class TMDbClient:
    def __init__(self, settings, cache_dir):
        self.api_key = settings['api_key']
        self.language = settings['language']
        self.expiry_seconds = cache_expiry_seconds(settings['cache_expiry_days'])
        self.cache_dir = cache_dir

    def _get(self, path, **params):
        """GET a v3 endpoint; accepts a v3 API key or a v4 read access token."""
        headers = {}
        if self.api_key.startswith('eyJ'):  # v4 read access token (JWT)
            headers['Authorization'] = f'Bearer {self.api_key}'
        else:
            params['api_key'] = self.api_key
        response = requests.get(f'{BASE_URL}/{path}', params=params,
                                headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()

    def check(self):
        """Raise (HTTPError 401) if the key is invalid."""
        self._get('configuration')

    def _cache_file(self, title):
        digest = hashlib.sha256(f'{self.language}\0{title}'.encode('utf-8')).hexdigest()
        return self.cache_dir / f'{digest}.json'

    def _read_cache(self, title):
        path = self._cache_file(title)
        try:
            if time.time() - path.stat().st_mtime < self.expiry_seconds:
                return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        return None

    def movie_info(self, title):
        """Return {'directors', 'cast', 'producers', 'imdb_id'} or None if not found.

        ``imdb_id`` has no ``tt`` prefix. Raises requests exceptions on API errors.
        """
        cached = self._read_cache(title)
        if cached:
            return cached

        results = self._get('search/movie', query=title, language=self.language)['results']
        if not results:
            return None
        movie = self._get(f"movie/{results[0]['id']}", language=self.language,
                          append_to_response='credits')
        credits = movie.get('credits', {})
        crew = credits.get('crew', [])
        info = {
            'directors': [c['name'] for c in crew if c.get('job') == 'Director'],
            'cast': [a['name'] for a in credits.get('cast', [])[:10]],
            'producers': [c['name'] for c in crew if c.get('job') == 'Producer'],
            'imdb_id': (movie.get('imdb_id') or '').removeprefix('tt') or None,
        }
        self._cache_file(title).write_text(json.dumps(info), encoding='utf-8')
        return info
