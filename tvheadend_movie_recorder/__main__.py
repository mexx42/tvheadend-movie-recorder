"""Command line entry point.

``python -m tvheadend_movie_recorder``         starts the web UI and the daily scheduler (default)
``python -m tvheadend_movie_recorder run``     does one pass now and prints a plain-text report; for cron
"""

import argparse
import logging
import os
import sys

from .recorder import Recorder
from .report import format_report
from .store import DataStore


def serve():
    from waitress import serve as waitress_serve

    from .scheduler import Scheduler
    from .web import create_app

    store = DataStore(os.environ.get('DATA_DIR', 'data'))
    scheduler = Scheduler(store, Recorder(store))
    app = create_app(store, scheduler)
    scheduler.start()
    port = int(os.environ.get('PORT', '5000'))
    logging.getLogger(__name__).info('Web interface listening on port %d', port)
    waitress_serve(app, host='0.0.0.0', port=port, threads=4)


def run_once(quiet, lock_timeout):
    """Run once, print the report, return the exit code (0 = ok, 1 = the run reported an error)."""
    store = DataStore(os.environ.get('DATA_DIR', 'data'))
    summary = Recorder(store).run(lock_timeout=lock_timeout)
    config = store.load_config()
    public_url = os.environ.get('PUBLIC_URL') or config['app']['public_url']
    if summary['error']:
        print(format_report(summary, public_url), file=sys.stderr)
        return 1
    if summary['scheduled'] or not quiet:
        print(format_report(summary, public_url))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tvheadend_movie_recorder', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command')
    run = commands.add_parser('run', help='do one pass now and print a report (for cron)')
    run.add_argument('--quiet', action='store_true',
                     help='print nothing unless something was scheduled or an error occurred, '
                          'so cron only sends mail when there is news')
    run.add_argument('--wait', type=int, default=300, metavar='SECONDS',
                     help='how long to wait if another run is in progress (default: 300)')
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        stream=sys.stderr)
    if args.command == 'run':
        if args.quiet:  # keep cron mails free of log lines; errors still reach stderr via the report
            logging.getLogger().setLevel(logging.CRITICAL)
        sys.exit(run_once(args.quiet, args.wait))
    serve()


if __name__ == '__main__':
    main()
