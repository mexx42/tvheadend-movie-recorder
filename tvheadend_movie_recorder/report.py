"""Plain-text report of one recorder run, meant for the terminal and for cron mails."""

from datetime import datetime


def format_report(summary, public_url=''):
    """Render a run summary (see Recorder.run) as ASCII text, without a trailing newline."""
    lines = [f"Tvheadend Movie Recorder - {summary['started'].replace('T', ' ')[:16]}"]
    if summary['error']:
        lines.append(f"Status: ERROR - {summary['error']}")
    else:
        lines.append(f"Status: OK - {summary['events']} movies in the guide")
    lines.append('')

    scheduled = summary['scheduled']
    if scheduled:
        lines.append(f'New recordings ({len(scheduled)}):')
        rows = [(datetime.fromtimestamp(s['start']).strftime('%a %d.%m. %H:%M') if s['start'] else '?',
                 s['channel'], s['title'], s['matched']) for s in scheduled]
        width = max(len(row[1]) for row in rows)
        for when, channel, title, matched in rows:
            lines.append(f'  {when}  {channel:<{width}}  {title} ({matched})')
    elif not summary['error']:
        lines.append('No new recordings.')

    if public_url:
        lines += ['', f"Manage: {public_url.rstrip('/')}/"]
    return '\n'.join(lines)
