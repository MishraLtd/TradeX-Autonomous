"""python -m execution.paper_trial --config ... --db ... COMMAND"""
import argparse
from dataclasses import asdict
import os
from pathlib import Path
import sys

from .config import TrialConfig, dumps
from .ledger import read_state, summarize
from .report import report, snapshot, restore, write_private
from .upstox import DataClient, DataError, analytics_token, prepare_config


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only Upstox data with local paper execution only')
    parser.add_argument('--config', default='/var/lib/tradex/upstox-trial/config.json')
    parser.add_argument('--db', default='/var/lib/tradex/upstox-trial/trial.sqlite')
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare', help='Check credentials, resolve stocks and freeze the next five sessions')
    prep.add_argument('--symbols', nargs=3, default=['SBIN', 'ITC', 'HDFCBANK'])
    sub.add_parser('run')
    sub.add_parser('health')
    sub.add_parser('status')
    sub.add_parser('report')
    snap = sub.add_parser('backup')
    snap.add_argument('destination')
    res = sub.add_parser('restore')
    res.add_argument('source')
    migration = sub.add_parser('upgrade-greedy', help='Offline flat-account upgrade preserving cash, fills and dates')
    migration.add_argument('--backup-directory', required=True)
    migration.add_argument('--expected-implementation', required=True)
    migration.add_argument('--release', required=True)
    migration.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    # This entry point has no --live option and refuses a live worker env even
    # for prepare. It cannot instantiate the legacy Kite execution path.
    if os.environ.get('TRADEX_MODE', 'upstox-paper') != 'upstox-paper':
        parser.error('TRADEX_MODE must be upstox-paper')
    if args.command == 'prepare':
        if Path(args.config).exists() or Path(args.db).exists():
            parser.error('Preparation is create-only; existing trial/config must be preserved')
        cfg = prepare_config(DataClient(analytics_token()), symbols=args.symbols)
        write_private(args.config, asdict(cfg), exclusive=True)
        print(dumps(dict(status='PREPARED_NOT_YET_MARKET_VERIFIED', config_hash=cfg.fingerprint,
                         sessions=cfg.sessions, instruments=cfg.instruments)))
        return 0
    if args.command == 'upgrade-greedy':
        from .upgrade import upgrade
        print(dumps(upgrade(args.config, args.db, args.backup_directory,
                            args.expected_implementation, args.release, check=args.check)))
        return 0
    cfg = TrialConfig.load(args.config)
    if args.command == 'run':
        from .runtime import run
        run(cfg, args.db, DataClient(analytics_token()), Path(args.db).parent / 'reports')
    elif args.command in ('health', 'status'):
        result = summarize(read_state(args.db))
        if result['config_hash'] != cfg.fingerprint:
            raise ValueError('Config/ledger mismatch')
        if args.command == 'health':
            result = {k: result[k] for k in ('checked_at', 'status', 'codes', 'completed', 'mode')}
        print(dumps(result))
    elif args.command == 'report':
        print(dumps(report(args.db)))
    elif args.command == 'backup':
        print(dumps(snapshot(args.db, args.destination)))
    elif args.command == 'restore':
        print(dumps(restore(args.source, args.db, cfg)))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except DataError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
