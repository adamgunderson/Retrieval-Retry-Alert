import configparser
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class FiremonConfig:
    base_url: str
    domain_id: str
    device_filter: str
    username: str
    password: str
    verify_tls: bool
    timeout: int


@dataclass
class RetryConfig:
    failure_statuses: set
    stale_after_hours: float
    grace_period_minutes: float
    state_dir: Path
    ignore_orphaned: bool
    ignore_unlicensed: bool
    exclude_name_contains: tuple


@dataclass
class DigestConfig:
    interval_hours: float


@dataclass
class SmtpConfig:
    host: str
    port: int
    use_starttls: bool
    username: str
    password: str
    from_address: str
    from_name: str
    recipients: list
    reply_to: str
    subject_prefix: str
    timeout: int


@dataclass
class AppConfig:
    firemon: FiremonConfig
    retry: RetryConfig
    digest: DigestConfig
    smtp: SmtpConfig
    template_dir: Path
    log_file: str
    config_path: Path
    warnings: list = field(default_factory=list)


def _get(parser, section, key, default=None, required=False):
    if parser.has_option(section, key):
        value = parser.get(section, key).strip()
        if value:
            return value
    if required and default is None:
        raise ConfigError(f'[{section}] {key} is required but not set (config or environment).')
    return default


def _get_bool(parser, section, key, default):
    raw = _get(parser, section, key, default=str(default))
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def _get_float(parser, section, key, default):
    raw = _get(parser, section, key, default=str(default))
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f'[{section}] {key} must be a number, got {raw!r}.')


def _get_int(parser, section, key, default):
    return int(_get_float(parser, section, key, default))


def _get_list(parser, section, key, default=''):
    raw = _get(parser, section, key, default=default) or ''
    return [item.strip() for item in raw.split(',') if item.strip()]


def _get_allow_blank(parser, section, key, default):
    """Like _get, but a key present-and-empty means "" on purpose (not "use default")."""
    if parser.has_option(section, key):
        return parser.get(section, key).strip()
    return default


def _resolve_path(base_dir, raw):
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def _warn_if_world_readable(path, warnings):
    if not path.exists():
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        warnings.append(
            f'{path} is readable by group/other (mode {oct(mode)}). '
            f'It may hold secrets -- restrict it with: chmod 600 "{path}"'
        )


def load_config(config_path):
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f'Config file not found: {config_path}')

    base_dir = config_path.parent
    parser = configparser.ConfigParser()
    try:
        with open(config_path, encoding='utf-8') as fh:
            parser.read_file(fh)
    except configparser.Error as e:
        raise ConfigError(f'Could not parse {config_path}: {e}')

    warnings = []
    _warn_if_world_readable(config_path, warnings)

    base_url = _get(parser, 'firemon', 'base_url', required=True).rstrip('/')
    username = _get(parser, 'firemon', 'username', required=True)
    password = os.environ.get('FMRETRY_PASSWORD') or _get(parser, 'firemon', 'password')
    if not password:
        raise ConfigError(
            'FireMon password not set. Put it in the FMRETRY_PASSWORD environment '
            'variable (recommended) or [firemon] password in the config file.'
        )

    firemon = FiremonConfig(
        base_url=base_url,
        domain_id=_get(parser, 'firemon', 'domain_id', default='1'),
        device_filter=_get_allow_blank(parser, 'firemon', 'device_filter',
                                        default="DEVICEPACK.DEVICETYPE = 'MANAGEMENT_STATION'"),
        username=username,
        password=password,
        verify_tls=_get_bool(parser, 'firemon', 'verify_tls', default=True),
        timeout=_get_int(parser, 'firemon', 'timeout', default=30),
    )

    # _get_allow_blank, not _get_list: an explicitly blank failure_statuses
    # ("retry on staleness only, not status") must not be silently replaced by
    # the default -- same reasoning as device_filter above.
    failure_statuses_raw = _get_allow_blank(parser, 'retry', 'failure_statuses',
                                             default='RETRIEVAL_ERROR, NORMALIZATION_ERROR, FAILURE')
    failure_statuses = {item.strip() for item in failure_statuses_raw.split(',') if item.strip()}
    state_dir = _resolve_path(base_dir, _get(parser, 'retry', 'state_dir', default='./state'))

    exclude_name_contains = tuple(_get_list(parser, 'retry', 'exclude_name_contains', default=''))

    retry = RetryConfig(
        failure_statuses=failure_statuses,
        stale_after_hours=_get_float(parser, 'retry', 'stale_after_hours', default=24),
        grace_period_minutes=_get_float(parser, 'retry', 'grace_period_minutes', default=15),
        state_dir=state_dir,
        ignore_orphaned=_get_bool(parser, 'retry', 'ignore_orphaned', default=True),
        ignore_unlicensed=_get_bool(parser, 'retry', 'ignore_unlicensed', default=True),
        exclude_name_contains=exclude_name_contains,
    )

    digest = DigestConfig(
        interval_hours=_get_float(parser, 'digest', 'interval_hours', default=24),
    )

    smtp_password = os.environ.get('FMRETRY_SMTP_PASSWORD') or _get(parser, 'smtp', 'password')
    recipients = _get_list(parser, 'smtp', 'recipients')
    from_address = _get(parser, 'smtp', 'from_address', required=True)

    smtp = SmtpConfig(
        host=_get(parser, 'smtp', 'host', default='localhost'),
        port=_get_int(parser, 'smtp', 'port', default=25),
        use_starttls=_get_bool(parser, 'smtp', 'use_starttls', default=False),
        username=_get(parser, 'smtp', 'username', default=''),
        password=smtp_password or '',
        from_address=from_address,
        from_name=_get(parser, 'smtp', 'from_name', default='FireMon Retrieval Retry Alert'),
        recipients=recipients,
        reply_to=_get(parser, 'smtp', 'reply_to', default=''),
        subject_prefix=_get(parser, 'smtp', 'subject_prefix', default=''),
        timeout=_get_int(parser, 'smtp', 'timeout', default=30),
    )
    if not recipients:
        warnings.append('[smtp] recipients is empty -- alerts will be logged but never emailed.')

    template_dir = _resolve_path(base_dir, _get(parser, 'paths', 'template_dir', default='./templates'))
    log_file_raw = _get(parser, 'paths', 'log_file', default='')
    log_file = str(_resolve_path(base_dir, log_file_raw)) if log_file_raw else None

    state_dir.mkdir(parents=True, exist_ok=True)
    _warn_if_world_readable(state_dir, warnings)

    return AppConfig(
        firemon=firemon, retry=retry, digest=digest, smtp=smtp,
        template_dir=template_dir, log_file=log_file,
        config_path=config_path, warnings=warnings,
    )


def log_warnings(config, logger):
    for warning in config.warnings:
        logger.warning(warning)
