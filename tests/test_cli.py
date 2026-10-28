import json
import multiprocessing
import time
from datetime import datetime

import pytest

from tvheadend_movie_recorder import __main__ as cli
from tvheadend_movie_recorder import recorder as rec
from tvheadend_movie_recorder.report import format_report
from tvheadend_movie_recorder.scheduler import is_due, latest_occurrence
from tvheadend_movie_recorder.store import DataStore, RunInProgress


def at(text):
    return datetime.fromisoformat(text)


def test_daily_run_is_due_only_once_per_day_after_run_at():
    run_at = '03:00'
    assert latest_occurrence(at('2026-10-28 14:00'), run_at) == at('2026-10-28 03:00')
    assert latest_occurrence(at('2026-10-28 02:59'), run_at) == at('2026-10-27 03:00')
    assert is_due(at('2026-10-28 14:00'), run_at, None)                        # never ran
    assert not is_due(at('2026-10-28 14:00'), run_at, '2026-10-28T03:00:05')   # ran today already
    assert is_due(at('2026-10-28 14:00'), run_at, '2026-10-27T03:00:05')       # missed today's run
    assert not is_due(at('2026-10-28 02:00'), run_at, '2026-10-27T03:00:05')   # not yet time today
    # a manual run late in the evening before counts: it is after the latest 03:00 occurrence
    assert not is_due(at('2026-10-28 14:00'), run_at, '2026-10-28T09:30:00')


def summary(**kw):
    base = {'started': '2026-10-28T14:00:03', 'events': 112, 'error': None, 'scheduled': []}
    return {**base, **kw}


def test_report_lists_new_recordings_and_link():
    start = int(datetime(2026, 10, 30, 20, 15).timestamp())
    text = format_report(summary(scheduled=[
        {'title': 'Die Hard', 'channel': 'ORF1', 'start': start, 'matched': 'Bruce Willis'},
        {'title': 'Heat', 'channel': 'ZDF neo', 'start': start, 'matched': 'Al Pacino'}]),
        'http://nas:5000/')
    assert text.splitlines() == [
        'Tvheadend Movie Recorder - 2026-10-28 14:00',
        'Status: OK - 112 movies in the guide',
        '',
        'New recordings (2):',
        '  Fri 30.10. 20:15  ORF1     Die Hard (Bruce Willis)',
        '  Fri 30.10. 20:15  ZDF neo  Heat (Al Pacino)',
        '',
        'Manage: http://nas:5000/']


def test_report_for_errors_and_empty_runs_without_url():
    assert 'No new recordings.' in format_report(summary())
    assert 'Manage' not in format_report(summary())
    assert 'Status: ERROR - Tvheadend refused' in format_report(summary(error='Tvheadend refused'))


class FakeTVH:
    def epg_events(self, days):
        now = int(time.time())
        return [{'title': 'Die Hard', 'genre': [16], 'channelName': 'ORF1', 'eventId': 7,
                 'start': now + 3600, 'stop': now + 7200, 'description': 'mit Bruce Willis'}]

    def dvr_titles(self):
        return set()

    def schedule(self, event_id, comment):
        pass


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    store = DataStore(tmp_path)
    store.update_config(lambda c: (c['tvheadend'].update(host='tvh'),
                                   c['app'].update(public_url='http://nas:5000')))
    store.update_watchlist(lambda w: w['names'].append('Bruce Willis'))
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.delenv('PUBLIC_URL', raising=False)
    monkeypatch.setattr(rec, 'TvheadendClient', lambda settings, traffic=None: FakeTVH())
    return tmp_path


def test_run_command_prints_report_then_stays_quiet_when_nothing_new(data_dir, capsys, monkeypatch):
    assert cli.run_once(quiet=True, lock_timeout=1) == 0
    out = capsys.readouterr().out
    assert 'Die Hard (Bruce Willis)' in out and 'Manage: http://nas:5000/' in out
    # second run: already scheduled by the first -> nothing new; with FakeTVH.dvr_titles empty it
    # would reschedule, so mark it as existing as Tvheadend would
    monkeypatch.setattr(FakeTVH, 'dvr_titles', lambda self: {'die hard'})
    assert cli.run_once(quiet=True, lock_timeout=1) == 0
    assert capsys.readouterr().out == ''                      # --quiet: no mail from cron
    assert cli.run_once(quiet=False, lock_timeout=1) == 0
    assert 'No new recordings.' in capsys.readouterr().out


def test_run_command_reports_errors_on_stderr_with_exit_code_1(data_dir, capsys):
    DataStore(data_dir).update_config(lambda c: c['tvheadend'].update(host=''))
    assert cli.run_once(quiet=True, lock_timeout=1) == 1
    captured = capsys.readouterr()
    assert captured.out == '' and 'Status: ERROR - Tvheadend host is not configured' in captured.err


def test_run_command_records_the_run_for_the_dashboard(data_dir):
    cli.run_once(quiet=True, lock_timeout=1)
    last = DataStore(data_dir).load_last_run()
    assert [e['title'] for e in last['scheduled']] == ['Die Hard']


def _hold_lock(path, ready, release):
    store = DataStore(path)
    with store.run_lock(1):
        ready.set()
        release.wait(10)


def test_run_lock_blocks_a_second_process_and_reports_it(tmp_path):
    ready, release = multiprocessing.Event(), multiprocessing.Event()
    holder = multiprocessing.Process(target=_hold_lock, args=(tmp_path, ready, release))
    holder.start()
    try:
        assert ready.wait(10)
        store = DataStore(tmp_path)
        with pytest.raises(RunInProgress):
            with store.run_lock(0.3):
                pass
        result = rec.Recorder(store).run(lock_timeout=0.3)
        assert 'already in progress' in result['error'] and result['scheduled'] == []
        assert store.load_last_run() is None            # a blocked attempt is not a run
    finally:
        release.set()
        holder.join(10)
    with DataStore(tmp_path).run_lock(1):               # free again once the holder exits
        pass
