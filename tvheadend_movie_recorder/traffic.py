"""Optional capture of the HTTP traffic with Tvheadend, for debugging.

Entries are JSON lines. Credentials are never recorded, fields that identify people or files
are blanked and long answers are shortened. The log is rotated (one old copy is kept), so the
option can stay enabled without filling the disk.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

MAX_FILE_BYTES = 2 * 1024 * 1024  # per file; with the rotated copy at most twice this
MAX_LIST_ITEMS = 100
MAX_STRING = 1000
# Blanked wherever they appear in a request or answer. Genre/channel data stays readable.
SENSITIVE_KEYS = frozenset({'password', 'passwd', 'secret', 'token', 'api_key', 'apikey', 'auth',
                            'username', 'user', 'owner', 'creator', 'filename'})
REDACTED = '<redacted>'


def sanitize(value):
    """Copy of ``value`` with sensitive fields blanked and long lists/strings shortened."""
    if isinstance(value, dict):
        return {key: REDACTED if str(key).lower() in SENSITIVE_KEYS else sanitize(item)
                for key, item in value.items()}
    if isinstance(value, list):
        items = [sanitize(item) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            items.append(f'<{len(value) - MAX_LIST_ITEMS} more items omitted>')
        return items
    if isinstance(value, str) and len(value) > MAX_STRING:
        return value[:MAX_STRING] + f'...<{len(value) - MAX_STRING} more characters>'
    return value


class TrafficLog:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'tvheadend-traffic.jsonl'
        self.old_path = self.directory / 'tvheadend-traffic.1.jsonl'
        self._lock = threading.Lock()

    def record(self, entry):
        line = json.dumps({'time': datetime.now().isoformat(timespec='milliseconds'),
                           **sanitize(entry)}, ensure_ascii=False, default=str) + '\n'
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size + len(line.encode()) > MAX_FILE_BYTES:
                os.replace(self.path, self.old_path)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as f:
                f.write(line)

    def read(self):
        """The recorded traffic, oldest entry first, as bytes (empty if nothing was recorded)."""
        with self._lock:
            return b''.join(p.read_bytes() for p in (self.old_path, self.path) if p.exists())

    def size(self):
        with self._lock:
            return sum(p.stat().st_size for p in (self.old_path, self.path) if p.exists())

    def clear(self):
        with self._lock:
            for p in (self.old_path, self.path):
                p.unlink(missing_ok=True)
