"""Core logic: pick movies from the Tvheadend EPG and schedule matching ones."""

import logging
from datetime import datetime

import requests

from .store import RunInProgress
from .tmdb import TMDbClient, purge_cache
from .tvheadend import TvheadendClient, describe_error

logger = logging.getLogger(__name__)


def filter_events(events, genre_ids, excluded_channels):
    """Keep events whose genres are all movie genres, on a channel that is not excluded."""
    excluded = {c.lower() for c in excluded_channels}
    return [e for e in events
            if e.get('genre') and set(e['genre']) <= genre_ids
            and e.get('channelName', '').lower() not in excluded]


def is_blacklisted(event, watchlist):
    """Return the reason string if the event's title is blacklisted, else None."""
    title = event.get('title', '').lower()
    for blocked in watchlist['blacklist']:
        if blocked.lower() in title:
            return f'title blacklisted ({blocked})'
    return None


def match_description(event, names):
    """First watchlist name occurring in the event description (case-insensitive)."""
    text = ' '.join(filter(None, [event.get('description'), event.get('summary'),
                                  event.get('subtitle')])).lower()
    return next((n for n in names if n.lower() in text), None)


def match_people(info, names):
    """First watchlist name among directors, cast and producers (case-insensitive)."""
    people = {p.lower() for p in info['directors'] + info['cast'] + info['producers']}
    return next((n for n in names if n.lower() in people), None)


class Recorder:
    def __init__(self, store):
        self.store = store

    def run(self, lock_timeout=10):
        """One pass over the EPG. Returns a summary dict; never raises on API errors.

        Holds the cross-process run lock and stores the summary as the last run. If another
        run holds the lock for longer than ``lock_timeout`` seconds, the summary says so.
        """
        try:
            with self.store.run_lock(lock_timeout):
                summary = self._run()
        except RunInProgress as e:
            return {'started': datetime.now().isoformat(timespec='seconds'), 'events': 0,
                    'scheduled': [], 'error': str(e)}
        except Exception as e:  # a bug must still produce a readable result, not a traceback
            logger.exception('Recorder run failed')
            summary = {'started': datetime.now().isoformat(timespec='seconds'), 'events': 0,
                       'scheduled': [], 'error': f'Unexpected error: {e}'}
        self.store.save_last_run(summary)
        return summary

    def _run(self):
        config = self.store.load_config()
        summary = {'started': datetime.now().isoformat(timespec='seconds'),
                   'events': 0, 'scheduled': [], 'error': None}

        if not config['tvheadend']['host']:
            summary['error'] = 'Tvheadend host is not configured'
            return summary

        try:
            traffic = self.store.traffic if config['tvheadend']['capture_traffic'] else None
            tvh = TvheadendClient(config['tvheadend'], traffic)
            purge_cache(self.store.cache_dir, config['tmdb']['cache_expiry_days'])
            rec = config['recorder']
            genre_ids = set(rec['genres'])
            if not genre_ids:
                summary['error'] = 'No movie genres are selected in Settings'
                return summary
            events = filter_events(tvh.epg_events(rec['days']), genre_ids,
                                   rec['excluded_channels'])
            summary['events'] = len(events)
            logger.info('Found %d movie events in EPG', len(events))

            tmdb = None
            if config['tmdb']['api_key']:
                tmdb = TMDbClient(config['tmdb'], self.store.cache_dir)
            else:
                logger.warning('No TMDb API key configured; matching on EPG descriptions only')

            scheduled_titles = tvh.dvr_titles()
            watchlist = self.store.load_watchlist()
            for event in events:
                self._process(event, tvh, tmdb, watchlist, scheduled_titles, summary)
        except requests.exceptions.RequestException as e:
            message = describe_error(e)
            logger.error('Tvheadend request failed: %s', message)
            summary['error'] = message
        return summary

    def _process(self, event, tvh, tmdb, watchlist, scheduled_titles, summary):
        title = event.get('title', '')
        logger.info('Processing: %s', title)
        if title.lower() in scheduled_titles:
            logger.info('Recording already scheduled for: %s', title)
            return
        reason = is_blacklisted(event, watchlist)
        if reason:
            logger.info('Skipping %s: %s', title, reason)
            return

        names = watchlist['names']
        matched = match_description(event, names)
        info = self._lookup(tmdb, title) if tmdb else None
        if matched is None and info:
            matched = match_people(info, names)
        if matched is None:
            return

        logger.info('Match found for %s (%s), scheduling recording', title, matched)
        try:
            tvh.schedule(event.get('eventId', 0), f'[{matched}]')
        except requests.exceptions.RequestException as e:
            logger.error('Failed to schedule %s: %s', title, e)
            return
        scheduled_titles.add(title.lower())
        self.store.add_recorded({
            'title': title,
            'matched_name': matched,
            'date_scheduled': datetime.now().isoformat(),
            'event_id': event.get('eventId'),
            'channel_name': event.get('channelName', ''),
            'start_time': event.get('start', 0),
            'imdb_id': info['imdb_id'] if info else None,
        })
        summary['scheduled'].append({'title': title, 'channel': event.get('channelName', ''),
                                     'start': event.get('start', 0), 'matched': matched})

    @staticmethod
    def _lookup(tmdb, title):
        try:
            return tmdb.movie_info(title)
        except requests.exceptions.RequestException as e:
            logger.error('Failed to fetch TMDb data for %s: %s', title, e)
            return None
