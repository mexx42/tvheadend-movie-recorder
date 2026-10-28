"""File-based persistence in a single data directory.

Everything the application stores lives in one directory (``/data`` in the
container): settings, watchlist, recorded movies, admin credentials, the
session secret and the TMDb cache. All writes are atomic and serialised by a
lock, because the web UI and the scheduler thread share the files.
"""

import contextlib
import copy
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # not available on Windows
    fcntl = None

import yaml

from .traffic import TrafficLog

DEFAULT_CONFIG = {
    'tvheadend': {
        'host': '',
        'port': 9981,
        'username': '',
        'password': '',
        'dvr_config_uuid': '',  # empty = Tvheadend's default DVR profile
        'capture_traffic': False,  # debugging: record the requests/answers exchanged with Tvheadend
    },
    'tmdb': {
        'api_key': '',
        'language': 'de-DE',
        'cache_expiry_days': 7,
    },
    'recorder': {
        'enabled': True,
        'run_at': '03:00',  # time of the daily automatic run (HH:MM, container time zone)
        'days': 7,
        'excluded_channels': [],  # channels that are never searched; empty = all channels
        # Tvheadend EPG genre ids (DVB content codes) that count as movies; picked in Settings.
        # 16 Movie/Drama, 17 Detective/Thriller, 18 Adventure/Western/War,
        # 19 Science fiction/Fantasy/Horror, 23 Serious/historical movie/drama
        'genres': [16, 17, 18, 19, 23],
    },
    'app': {
        'public_url': '',  # address of the web interface, shown in reports (e.g. http://nas:5000)
    },
}

DEFAULT_WATCHLIST = {'names': [], 'blacklist': []}


def _merge(defaults, overrides):
    """Return defaults with overrides applied recursively; unknown keys are kept."""
    result = copy.deepcopy(defaults)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class RunInProgress(Exception):
    """Raised when the run lock could not be acquired in time."""


class DataStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / 'cache'
        self.cache_dir.mkdir(exist_ok=True)
        self.traffic = TrafficLog(self.data_dir / 'debug')
        self.lock = threading.RLock()

    # -- low level -------------------------------------------------------
    def _path(self, name):
        return self.data_dir / name

    def _write_text(self, name, text, private=False):
        path = self._path(name)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=f'.{name}.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
            if private:
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _read_yaml(self, name):
        path = self._path(name)
        if not path.exists():
            return None
        with open(path, encoding='utf-8') as f:
            return yaml.safe_load(f)

    def _write_yaml(self, name, data, private=False):
        self._write_text(name, yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                         private=private)

    # -- settings --------------------------------------------------------
    def load_config(self):
        with self.lock:
            return _merge(DEFAULT_CONFIG, self._read_yaml('config.yaml'))

    def save_config(self, config):
        with self.lock:
            self._write_yaml('config.yaml', config, private=True)  # holds passwords

    def update_config(self, fn):
        """Load the config, apply ``fn(config)`` and save it atomically."""
        with self.lock:
            config = self.load_config()
            fn(config)
            self.save_config(config)

    # -- watchlist -------------------------------------------------------
    def load_watchlist(self):
        with self.lock:
            return _merge(DEFAULT_WATCHLIST, self._read_yaml('watchlist.yaml'))

    def update_watchlist(self, fn):
        with self.lock:
            watchlist = self.load_watchlist()
            fn(watchlist)
            self._write_yaml('watchlist.yaml', watchlist)

    # -- recorded movies -------------------------------------------------
    def load_recorded(self):
        with self.lock:
            data = self._read_yaml('recorded_movies.yaml') or {}
            data.setdefault('movies', [])
            return data

    def add_recorded(self, entry):
        with self.lock:
            data = self.load_recorded()
            data['movies'].append(entry)
            self._write_yaml('recorded_movies.yaml', data)

    # -- last run and run lock -----------------------------------------------
    def load_last_run(self):
        """Summary of the most recent recorder run (any process), or None."""
        with self.lock:
            path = self._path('last_run.json')
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text(encoding='utf-8'))
            except ValueError:
                return None

    def save_last_run(self, summary):
        with self.lock:
            self._write_text('last_run.json', json.dumps(summary))

    @contextlib.contextmanager
    def run_lock(self, timeout):
        """Hold the cross-process run lock; raise RunInProgress if not free within ``timeout`` s.

        The web UI, the built-in scheduler and ``python -m tvheadend_movie_recorder run`` (a separate
        process, e.g. from cron) all go through this, so two runs never overlap.
        """
        if fcntl is None:  # platforms without flock: no cross-process protection
            yield
            return
        with open(self._path('run.lock'), 'a') as handle:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RunInProgress('Another run is already in progress') from None
                    time.sleep(0.2)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # -- authentication --------------------------------------------------
    def load_auth(self):
        """Return {'username', 'password_hash'} or None while no admin exists."""
        with self.lock:
            path = self._path('auth.json')
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding='utf-8'))

    def save_auth(self, username, password_hash):
        with self.lock:
            self._write_text('auth.json',
                             json.dumps({'username': username, 'password_hash': password_hash}),
                             private=True)

    def secret_key(self):
        """Session signing key, generated on first use and persisted."""
        with self.lock:
            path = self._path('secret.key')
            if not path.exists():
                self._write_text('secret.key', secrets.token_hex(32), private=True)
            return path.read_text(encoding='utf-8').strip()
