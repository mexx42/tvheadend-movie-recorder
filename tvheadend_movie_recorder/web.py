"""Flask web UI: first-run admin setup, login, watchlist and settings management."""

import hmac
import os
import re
import secrets
import time
from datetime import datetime
from functools import wraps

import requests
from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .store import DataStore
from .tmdb import MAX_CACHE_DAYS, TMDbClient
from .tvheadend import TvheadendClient, describe_error

LIST_KINDS = {'names': 'names', 'blacklist': 'blacklist'}
# Genre ids 16-31 are the DVB category 'Movie / Drama'; Settings lists them first.
MOVIE_GENRE_IDS = range(16, 32)
UNDATED = 'undated'


def month_of(movie):
    """'YYYY-MM' of the day a movie was scheduled, or UNDATED if the date is missing or invalid."""
    try:
        return datetime.fromisoformat(movie['date_scheduled']).strftime('%Y-%m')
    except (KeyError, TypeError, ValueError):
        return UNDATED


def month_counts(movies):
    """{month: number of movies}, newest month first, UNDATED last."""
    counts = {}
    for movie in movies:
        month = month_of(movie)
        counts[month] = counts.get(month, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (kv[0] != UNDATED, kv[0]), reverse=True))


def month_label(month):
    if month == UNDATED:
        return 'Undated'
    return datetime.strptime(month, '%Y-%m').strftime('%B %Y')


MIN_PASSWORD_LENGTH = 8
MAX_FAILED_LOGINS = 5
LOGIN_LOCKOUT_SECONDS = 300


def create_app(store: DataStore, scheduler):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=store.secret_key(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        # Off by default: the typical home setup runs on plain http.
        SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes'),
    )
    failed_logins = {}  # ip -> (count, first failure time)

    # -- helpers -----------------------------------------------------------
    def csrf_token():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_hex(16)
        return session['csrf']

    def format_date(iso_date):
        try:
            return datetime.fromisoformat(iso_date).strftime('%d.%m.%Y %H:%M')
        except (TypeError, ValueError):
            return iso_date

    def format_timestamp(ts):
        return datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M') if ts else ''

    app.jinja_env.globals.update(csrf_token=csrf_token, format_date=format_date)

    @app.before_request
    def protect():
        if request.endpoint == 'static':
            return None
        if request.method == 'POST':
            sent = request.form.get('csrf_token', '')
            expected = session.get('csrf', '')
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, 'Invalid or missing CSRF token (reload the page and try again)')
        auth = store.load_auth()
        if auth is None:
            if request.endpoint != 'setup':
                return redirect(url_for('setup'))
            return None
        g.user = session.get('user') == auth['username']
        if request.endpoint in ('login', 'setup'):
            return None
        if not g.user:
            return redirect(url_for('login'))
        return None

    @app.after_request
    def security_headers(response):
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    def back():
        """Redirect to the dashboard; row actions stay on their month of recorded movies."""
        month = request.form.get('month')
        if month:
            return redirect(url_for('index', month=month, _anchor='recorded'))
        return redirect(url_for('index'))

    # -- authentication ------------------------------------------------------
    @app.route('/setup', methods=['GET', 'POST'])
    def setup():
        if store.load_auth() is not None:
            return redirect(url_for('index'))
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            if not username:
                flash('Please choose a username.', 'error')
            elif len(password) < MIN_PASSWORD_LENGTH:
                flash(f'Password must have at least {MIN_PASSWORD_LENGTH} characters.', 'error')
            elif password != request.form.get('password2', ''):
                flash('The passwords do not match.', 'error')
            else:
                store.save_auth(username, generate_password_hash(password))
                session.clear()
                session['user'] = username
                flash('Account created. Next, configure your Tvheadend connection.', 'ok')
                return redirect(url_for('settings'))
        return render_template('setup.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if g.user:
            return back()
        if request.method == 'POST':
            ip = request.remote_addr
            count, since = failed_logins.get(ip, (0, 0))
            if count >= MAX_FAILED_LOGINS and time.time() - since < LOGIN_LOCKOUT_SECONDS:
                flash('Too many failed attempts. Try again in a few minutes.', 'error')
                return render_template('login.html'), 429
            auth = store.load_auth()
            ok = (hmac.compare_digest(request.form.get('username', ''), auth['username'])
                  & check_password_hash(auth['password_hash'], request.form.get('password', '')))
            if ok:
                failed_logins.pop(ip, None)
                session.clear()
                session['user'] = auth['username']
                return back()
            failed_logins[ip] = (count + 1, since if count else time.time())
            flash('Wrong username or password.', 'error')
        return render_template('login.html')

    @app.route('/logout', methods=['POST'])
    def logout():
        session.clear()
        return redirect(url_for('login'))

    # -- dashboard -----------------------------------------------------------
    @app.route('/')
    def index():
        watchlist = store.load_watchlist()
        movies = store.load_recorded()['movies']
        months = month_counts(movies)  # newest first
        month = request.args.get('month')
        if month not in months:
            month = next(iter(months), None)  # newest month with recordings
        recorded = sorted((m for m in movies if month_of(m) == month),
                          key=lambda m: m.get('date_scheduled') or '', reverse=True)
        keys = list(months)
        position = keys.index(month) if month else 0
        config = store.load_config()
        return render_template(
            'index.html',
            names=sorted(watchlist['names'], key=str.lower),
            blacklist=sorted(watchlist['blacklist'], key=str.lower),
            recorded_movies=recorded,
            months=months,
            month=month,
            newer_month=keys[position - 1] if month and position > 0 else None,
            older_month=keys[position + 1] if month and position + 1 < len(keys) else None,
            summary=store.load_last_run(),
            configured=bool(config['tvheadend']['host']),
            auto_run=config['recorder']['enabled'],
            run_at=config['recorder']['run_at'],
            month_label=month_label,
            format_timestamp=format_timestamp)

    @app.route('/run', methods=['POST'])
    def run_now():
        scheduler.run_now()
        flash('Run started. Reload in a moment to see the result.', 'ok')
        return back()

    # -- watchlist editing -----------------------------------------------------
    @app.route('/list/<kind>/add', methods=['POST'])
    def list_add(kind):
        key = LIST_KINDS.get(kind) or abort(404)
        value = request.form.get('value', '').strip()
        if value:
            def add(watchlist):
                if value.lower() not in {v.lower() for v in watchlist[key]}:
                    watchlist[key].append(value)
            store.update_watchlist(add)
        return back()

    @app.route('/list/<kind>/remove', methods=['POST'])
    def list_remove(kind):
        key = LIST_KINDS.get(kind) or abort(404)
        value = request.form.get('value', '')
        store.update_watchlist(lambda w: w.update({key: [v for v in w[key] if v != value]}))
        return back()

    @app.route('/recorded/blacklist-title', methods=['POST'])
    def blacklist_title():
        title = request.form.get('value', '').strip()
        if title:
            store.update_watchlist(lambda w: w['blacklist'].append(title)
                                   if title not in w['blacklist'] else None)
            flash(f'"{title}" will no longer be recorded.', 'ok')
        return back()

    @app.route('/recorded/exclude-channel', methods=['POST'])
    def exclude_channel():
        channel = request.form.get('value', '').strip()
        if channel:
            def exclude(config):
                excluded = config['recorder']['excluded_channels']
                if channel.lower() not in {c.lower() for c in excluded}:
                    excluded.append(channel)
            store.update_config(exclude)
            flash(f'Channel "{channel}" removed from the channel list. '
                  'You can enable it again in Settings.', 'ok')
        return back()

    # -- settings --------------------------------------------------------------
    def apply_form(config):
        """Copy submitted settings into ``config``; blank secret fields keep their value."""
        f = request.form

        def integer(name, default, minimum):
            try:
                return max(minimum, int(f.get(name, default)))
            except ValueError:
                return default

        tvh, tmdb, rec = config['tvheadend'], config['tmdb'], config['recorder']
        tvh['host'] = f.get('tvh_host', '').strip()
        tvh['port'] = integer('tvh_port', tvh['port'], 1)
        tvh['username'] = f.get('tvh_username', '').strip()
        if f.get('tvh_password'):
            tvh['password'] = f['tvh_password']
        tvh['dvr_config_uuid'] = f.get('tvh_dvr_config_uuid', '').strip()
        tvh['capture_traffic'] = 'tvh_capture_traffic' in f
        if f.get('tmdb_api_key', '').strip():
            tmdb['api_key'] = f['tmdb_api_key'].strip()
        tmdb['language'] = f.get('tmdb_language', '').strip() or tmdb['language']
        tmdb['cache_expiry_days'] = integer('tmdb_cache_expiry_days', tmdb['cache_expiry_days'], 1)
        tmdb['cache_expiry_days'] = min(tmdb['cache_expiry_days'], MAX_CACHE_DAYS)
        rec['enabled'] = 'rec_enabled' in f
        run_at = f.get('rec_run_at', '').strip()
        if re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', run_at):
            rec['run_at'] = run_at
        config['app']['public_url'] = f.get('app_public_url', '').strip().rstrip('/')
        rec['days'] = integer('rec_days', rec['days'], 1)
        # Genres not listed in the form (e.g. Tvheadend unreachable) keep their state.
        def ids(name):
            return {int(i) for value in f.getlist(name) for i in value.split(',') if i.isdigit()}
        rec['genres'] = sorted((set(rec['genres']) - ids('rec_genres_shown')) | ids('rec_genres'))
        # Channels not listed in the form (e.g. Tvheadend unreachable) keep their state.
        shown = set(f.getlist('rec_channels_shown'))
        enabled = set(f.getlist('rec_channels'))
        shown_lower = {c.lower() for c in shown}
        rec['excluded_channels'] = ([c for c in rec['excluded_channels']
                                     if c.lower() not in shown_lower]
                                    + sorted(shown - enabled, key=str.lower))

    def traffic_log(config):
        """The traffic log if capturing is enabled in ``config``, else None."""
        return store.traffic if config['tvheadend']['capture_traffic'] else None

    def run_tests(config):
        try:
            if not config['tvheadend']['host']:
                raise ValueError('host is empty')
            version = TvheadendClient(config['tvheadend'], traffic_log(config)).check()
            flash(f'Tvheadend OK (version {version}).', 'ok')
        except (requests.exceptions.RequestException, ValueError) as e:
            flash(f'Tvheadend connection failed: {describe_error(e)}', 'error')
        try:
            if not config['tmdb']['api_key']:
                raise ValueError('no API key set')
            TMDbClient(config['tmdb'], store.cache_dir).check()
            flash('TMDb OK (API key accepted).', 'ok')
        except (requests.exceptions.RequestException, ValueError) as e:
            flash(f'TMDb check failed: {e}', 'error')

    @app.route('/settings', methods=['GET', 'POST'])
    def settings():
        config = store.load_config()
        if request.method == 'POST':
            apply_form(config)
            if request.form.get('action') == 'test':
                run_tests(config)  # tests the submitted values without saving them
            else:
                store.save_config(config)
                flash('Settings saved.', 'ok')
                return redirect(url_for('settings'))

        channels, genres, tvh_error = [], [], None
        if config['tvheadend']['host']:
            client = TvheadendClient(config['tvheadend'], traffic_log(config))
            try:
                channels = client.channel_names()
                genres = client.genres()
            except requests.exceptions.RequestException as e:
                tvh_error = f'Could not load channels and genres from Tvheadend: {describe_error(e)}'
        # keep excluded channels that the server no longer lists, so they stay visible and
        # can be re-enabled; saving then never silently drops them
        known = {c.lower() for c in channels}
        missing = [c for c in config['recorder']['excluded_channels'] if c.lower() not in known]
        selected = set(config['recorder']['genres'])
        for group in genres:
            group['selected'] = all(i in selected for i in group['ids'])
        return render_template(
            'settings.html', config=config, channels=channels + missing, tvh_error=tvh_error,
            movie_genres=[g for g in genres if g['ids'][0] in MOVIE_GENRE_IDS],
            other_genres=[g for g in genres if g['ids'][0] not in MOVIE_GENRE_IDS],
            traffic_size=store.traffic.size())

    @app.route('/settings/traffic.jsonl')
    def download_traffic():
        data = store.traffic.read()
        if not data:
            flash('No Tvheadend traffic has been recorded yet.', 'error')
            return redirect(url_for('settings'))
        return Response(data, mimetype='application/x-ndjson', headers={
            'Content-Disposition': 'attachment; filename=tvheadend-traffic.jsonl'})

    @app.route('/settings/traffic/clear', methods=['POST'])
    def clear_traffic():
        store.traffic.clear()
        flash('Recorded Tvheadend traffic deleted.', 'ok')
        return redirect(url_for('settings'))

    @app.route('/settings/password', methods=['POST'])
    def change_password():
        auth = store.load_auth()
        new = request.form.get('new_password', '')
        if not check_password_hash(auth['password_hash'], request.form.get('old_password', '')):
            flash('Current password is wrong.', 'error')
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(f'Password must have at least {MIN_PASSWORD_LENGTH} characters.', 'error')
        elif new != request.form.get('new_password2', ''):
            flash('The new passwords do not match.', 'error')
        else:
            store.save_auth(auth['username'], generate_password_hash(new))
            flash('Password changed.', 'ok')
        return redirect(url_for('settings'))

    return app
