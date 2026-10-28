# Contributing

Bug reports, ideas and pull requests are welcome.

- **Bug reports:** include your Tvheadend version, the relevant log lines (`docker compose logs`, set
  `LOG_LEVEL=DEBUG` if needed) and what you expected. Remove passwords and API keys.
- **Genres and channels** differ between providers. If the defaults don't find movies for your country,
  please open an issue with a screenshot of *Settings > Recording*, or the genre list your Tvheadend reports
  (`/api/epg/content_type/list?full=1`).
- **Pull requests:** keep them focused, run `python -m pytest` and add a test for behaviour changes.

## Layout

| File | Purpose |
|---|---|
| `tvheadend_movie_recorder/recorder.py` | Event filtering, matching and scheduling logic |
| `tvheadend_movie_recorder/tvheadend.py`, `tmdb.py` | API clients |
| `tvheadend_movie_recorder/store.py` | All persistence (one data directory) |
| `tvheadend_movie_recorder/scheduler.py` | Periodic / on-demand runs |
| `tvheadend_movie_recorder/web.py`, `templates/` | Web interface and authentication |

By contributing you agree that your work is licensed under the GPL-3.0-or-later, like the rest of the project.
