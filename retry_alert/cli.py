import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import __version__
from .api import ApiError, AuthenticationError, FireMonClient
from .config import ConfigError, load_config, log_warnings
from .digest import build_digest, is_due, time_until_due
from .engine import EXIT_ALERTED, EXIT_FATAL, EXIT_OK, LockHeld, RetryAlertEngine, StateStore
from .mailer import send_digest_email
from .model import DIGEST_STATE_LABELS, DIGEST_STILL_BROKEN

logger = logging.getLogger('retry_alert')


def setup_logging(level_name, log_file=None):
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG)

    level = getattr(logging, level_name.upper(), logging.INFO)
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(level)
    stream.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    root.addHandler(stream)

    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
            root.addHandler(file_handler)
        except OSError as e:
            logging.getLogger(__name__).warning('Could not open log file %s: %s', log_file, e)


def build_parser():
    parser = argparse.ArgumentParser(
        prog='retry_alert',
        description='Scan FireMon device retrieval health, retry failures, and digest what still needs attention.')
    parser.add_argument('--config', default='config.ini', help='Path to config.ini (default: ./config.ini)')
    parser.add_argument('--dry-run', action='store_true',
                         help='Scan and report what would happen; trigger no retrievals, send no email, change no state.')
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    parser.add_argument('--list-pending', action='store_true',
                         help='Print devices currently pending a retry confirmation and the digest ledger, then exit.')
    parser.add_argument('--send-digest', action='store_true',
                         help='Force-send a digest of everything in the ledger right now, regardless of '
                              'the interval timer, then reset the timer. Skips the normal retry cycle.')
    parser.add_argument('--reset-state', action='store_true',
                         help='Clear pending-retry state, the digest ledger, and the digest timer. Requires --yes.')
    parser.add_argument('--yes', action='store_true', help='Confirm a destructive action such as --reset-state.')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    return parser


def _format_remaining(remaining):
    if remaining <= timedelta(0):
        return 'due now'
    total_minutes = int(remaining.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f'{hours}h {minutes}m' if hours else f'{minutes}m'


def _print_state(state, config, now):
    pending = state.load_pending()
    print(f'Pending retries: {len(pending)}')
    for device_id, retry in pending.items():
        print(f'  - {retry.device_name} (ID {device_id}): reason={retry.reason} '
              f'triggered_at={retry.triggered_at.isoformat()} attempts={retry.attempts}')

    ledger = state.read_ledger()
    digest_state = state.load_digest_state()
    last_sent = digest_state.get('last_sent', '(never)')
    print(f'Digest ledger: {len(ledger)} event(s) recorded since the last digest, last sent: {last_sent}')

    remaining = _format_remaining(time_until_due(state, config, now))
    # :g avoids "24.0h" for the common whole-number case while still showing
    # "4.5h" if someone configured a fractional interval.
    print(f'Next digest: {remaining} (every {config.digest.interval_hours:g}h)')


def _run_digest(client, config, state, now, dry_run, force):
    """Sends a digest if due (or unconditionally, when force=True from
    --send-digest). Returns an exit code; never raises -- a failed send is
    reported and turned into EXIT_FATAL, leaving the ledger and timer
    untouched so the same content is retried next cycle."""
    if not force and not is_due(state, config, now):
        logger.debug('Digest not due yet.')
        return EXIT_OK

    summaries, since = build_digest(client, state, config, now)

    if dry_run:
        if summaries:
            logger.info('[dry-run] Would send a digest covering %s to %s: %d device(s) -- %s',
                        since.isoformat(), now.isoformat(), len(summaries),
                        ', '.join(f'{s.device_name} ({DIGEST_STATE_LABELS.get(s.current_state, s.current_state)})'
                                  for s in summaries))
        else:
            logger.info('[dry-run] Digest is due but there is nothing to report since %s.', since.isoformat())
        return EXIT_OK

    try:
        sent = send_digest_email(config, summaries, since, now)
    except Exception:
        logger.critical('Digest email failed to send -- leaving the ledger and timer unchanged; '
                         'the same content will be retried next cycle.')
        return EXIT_FATAL

    state.save_digest_state({'last_sent': now.isoformat()})
    # Prune to `now`, not `since`: everything up to `now` was just reported
    # (the lock held for this whole call rules out a concurrent append landing
    # in between), so clearing through `now` empties the ledger immediately
    # rather than leaving the just-reported window sitting there for one more
    # cycle before `since` catches up to it.
    state.prune_ledger(now)
    if sent:
        logger.info('Digest sent: %d device(s) reported.', len(summaries))
    else:
        logger.info('Digest window closed with nothing to report.')

    if any(s.current_state == DIGEST_STILL_BROKEN for s in summaries):
        return EXIT_ALERTED
    return EXIT_OK


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        logger.critical('Configuration error: %s', e)
        return EXIT_FATAL

    setup_logging(args.log_level, log_file=config.log_file)
    log_warnings(config, logger)

    state = StateStore(config.retry.state_dir)
    now = datetime.now(timezone.utc)

    if args.list_pending:
        _print_state(state, config, now)
        return EXIT_OK

    if args.reset_state:
        if not args.yes:
            logger.error('--reset-state requires --yes to confirm.')
            return EXIT_FATAL
        try:
            state.acquire_lock()
        except LockHeld as e:
            logger.warning(str(e))
            return EXIT_FATAL
        try:
            state.save_pending({})
            state.clear_ledger()
            state.save_digest_state({})
        finally:
            state.release_lock()
        logger.info('State cleared: pending retries, digest ledger, and digest timer reset.')
        return EXIT_OK

    try:
        state.acquire_lock()
    except LockHeld as e:
        logger.warning(str(e))
        return EXIT_OK

    try:
        try:
            client = FireMonClient(config.firemon)
            client.authenticate()

            if args.send_digest:
                return _run_digest(client, config, state, now, args.dry_run, force=True)

            engine = RetryAlertEngine(client, config, state, now=now, dry_run=args.dry_run)
            result = engine.run_cycle()
        except (AuthenticationError, ApiError) as e:
            logger.critical('Cycle failed: %s', e)
            return EXIT_FATAL

        logger.info('Cycle complete: %d triggered, %d resolved, %d recorded for the next digest.',
                    len(result.triggered), len(result.resolved), len(result.recorded))

        digest_rc = _run_digest(client, config, state, now, args.dry_run, force=False)
        if digest_rc == EXIT_FATAL:
            return EXIT_FATAL
        return max(result.exit_code, digest_rc)
    finally:
        state.release_lock()


if __name__ == '__main__':
    sys.exit(main())
