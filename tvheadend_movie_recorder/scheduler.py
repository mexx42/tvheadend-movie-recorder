"""Background thread that runs the recorder once a day, or on demand."""

import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30  # how often the thread looks at the clock and the settings


def latest_occurrence(now, run_at):
    """The most recent moment at or before ``now`` whose time of day is ``run_at`` ('HH:MM')."""
    hour, minute = (int(part) for part in run_at.split(':'))
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return moment if moment <= now else moment - timedelta(days=1)


def is_due(now, run_at, last_started):
    """True if the daily run at ``run_at`` has not happened yet since its latest occurrence.

    Also covers a container that was down at ``run_at``: it runs on the next start.
    """
    if last_started is None:
        return True
    return datetime.fromisoformat(last_started) < latest_occurrence(now, run_at)


class Scheduler:
    def __init__(self, store, recorder):
        self.store = store
        self.recorder = recorder
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name='scheduler', daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def run_now(self):
        """Ask the scheduler thread to run immediately (non-blocking)."""
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            requested = self._wake.is_set()
            self._wake.clear()  # a run_now() during the run below triggers another run
            if requested or self._daily_run_due():
                self._run_once()
            self._wake.wait(timeout=CHECK_INTERVAL_SECONDS)

    def _daily_run_due(self):
        config = self.store.load_config()
        if not config['recorder']['enabled'] or not config['tvheadend']['host']:
            return False  # nothing to run yet; "Run now" still works
        last = self.store.load_last_run()
        return is_due(datetime.now(), config['recorder']['run_at'],
                      last['started'] if last else None)

    def _run_once(self):
        summary = self.recorder.run()
        if summary['error']:
            logger.error('Run finished with error: %s', summary['error'])
        else:
            logger.info('Run finished: %d events, %d scheduled',
                        summary['events'], len(summary['scheduled']))
