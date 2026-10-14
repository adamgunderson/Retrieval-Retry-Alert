import html
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from .model import (
    ALERT_LABELS, DIGEST_NOT_TRACKED, DIGEST_RESOLVED, DIGEST_STATE_LABELS, DIGEST_STILL_BROKEN,
)

logger = logging.getLogger(__name__)

_BADGE_CLASS = {
    DIGEST_STILL_BROKEN: 'badge-broken',
    DIGEST_RESOLVED: 'badge-resolved',
    DIGEST_NOT_TRACKED: 'badge-not-tracked',
}

# Section order in the email: what needs attention first, then what fixed
# itself, then what's no longer being watched at all.
_SECTION_STATES = (DIGEST_STILL_BROKEN, DIGEST_RESOLVED, DIGEST_NOT_TRACKED)

_DEFAULT_HTML = """<html><body>
<h2>FireMon Retrieval Digest</h2>
<p>{{still_broken_count}} still broken, {{resolved_count}} resolved, {{not_tracked_count}} no longer
tracked -- covering {{since}} to {{generated_at}}.</p>
{{rows}}
</body></html>"""

_DEFAULT_TEXT = """FireMon Retrieval Digest
{{still_broken_count}} still broken, {{resolved_count}} resolved, {{not_tracked_count}} no longer tracked
Covering {{since}} to {{generated_at}}

{{rows}}
"""


def _load_template(template_dir, name, fallback):
    path = Path(template_dir) / name
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        logger.warning('Could not read template %s -- using the built-in default.', path)
        return fallback


def _row_html(summary):
    label = DIGEST_STATE_LABELS.get(summary.current_state, summary.current_state)
    reason_label = ALERT_LABELS.get(summary.last_alert_reason, summary.last_alert_reason)
    badge_class = _BADGE_CLASS.get(summary.current_state, 'badge-not-tracked')
    # Device name/status/description come from FireMon inventory data, not a
    # trusted template -- escape before embedding in the HTML body.
    name = html.escape(str(summary.device_name))
    status = html.escape(str(summary.last_status))
    detail = html.escape(str(summary.last_status_description) or '(none)')
    reason = html.escape(reason_label)
    times = (f'seen once at {summary.last_seen.isoformat()}' if summary.event_count == 1 else
             f'seen {summary.event_count} times, {summary.first_seen.isoformat()} to {summary.last_seen.isoformat()}')
    return f"""
        <div class="device-card">
            <div class="device-info">
                <div class="device-name">{name}</div>
                <div class="device-id">ID: {summary.device_id}</div>
            </div>
            <div class="device-status">
                <span class="badge {badge_class}">{label}</span>
            </div>
            <div class="error-detail">
                <div class="detail-item"><b>Last event:</b> {reason}</div>
                <div class="detail-item"><b>Last status:</b> {status}</div>
                <div class="detail-item"><b>Detail:</b> {detail}</div>
                <div class="detail-item"><b>In this window:</b> {times}</div>
            </div>
        </div>"""


def _row_text(summary):
    label = DIGEST_STATE_LABELS.get(summary.current_state, summary.current_state)
    reason_label = ALERT_LABELS.get(summary.last_alert_reason, summary.last_alert_reason)
    times = (f'seen once at {summary.last_seen.isoformat()}' if summary.event_count == 1 else
             f'seen {summary.event_count} times, {summary.first_seen.isoformat()} to {summary.last_seen.isoformat()}')
    return (f"- {summary.device_name} (ID {summary.device_id}): {label}\n"
            f"  last event={reason_label} status={summary.last_status} "
            f"detail={summary.last_status_description or '(none)'}\n"
            f"  {times}")


def _group_by_state(summaries):
    """Returns [(state, [summary, ...]), ...] for every state that actually
    has at least one entry, in _SECTION_STATES order -- an empty section
    (e.g. "Resolved (0)") is just noise, so it's left out entirely."""
    by_state = {}
    for summary in summaries:
        by_state.setdefault(summary.current_state, []).append(summary)
    return [(state, by_state[state]) for state in _SECTION_STATES if by_state.get(state)]


def _sections_html(summaries):
    parts = []
    for state, items in _group_by_state(summaries):
        title = html.escape(DIGEST_STATE_LABELS.get(state, state))
        parts.append(f'<div class="section-title section-{state}">{title} ({len(items)})</div>')
        parts.extend(_row_html(s) for s in items)
    return ''.join(parts)


def _sections_text(summaries):
    blocks = []
    for state, items in _group_by_state(summaries):
        title = DIGEST_STATE_LABELS.get(state, state).upper()
        header = f'=== {title} ({len(items)}) ==='
        rows = '\n'.join(_row_text(s) for s in items)
        blocks.append(f'{header}\n{rows}')
    return '\n\n'.join(blocks)


def build_digest_email(config, summaries, since, now):
    still_broken = sum(1 for s in summaries if s.current_state == DIGEST_STILL_BROKEN)
    resolved = sum(1 for s in summaries if s.current_state == DIGEST_RESOLVED)
    not_tracked = sum(1 for s in summaries if s.current_state == DIGEST_NOT_TRACKED)
    # UTC and labeled as such: the per-row ISO timestamps are UTC-offset
    # qualified, so an unlabeled local-time header would look inconsistent.
    since_str = since.strftime('%B %d, %Y at %H:%M UTC')
    generated_at = now.strftime('%B %d, %Y at %H:%M UTC')

    rows_html = _sections_html(summaries)
    rows_text = _sections_text(summaries)

    replacements = {
        '{{still_broken_count}}': str(still_broken),
        '{{resolved_count}}': str(resolved),
        '{{not_tracked_count}}': str(not_tracked),
        '{{since}}': since_str,
        '{{generated_at}}': generated_at,
    }

    html_body = _load_template(config.template_dir, 'digest.html', _DEFAULT_HTML)
    for token, value in replacements.items():
        html_body = html_body.replace(token, value)
    html_body = html_body.replace('{{rows}}', rows_html)

    text_body = _load_template(config.template_dir, 'digest.txt', _DEFAULT_TEXT)
    for token, value in replacements.items():
        text_body = text_body.replace(token, value)
    text_body = text_body.replace('{{rows}}', rows_text)

    subject = (f'{config.smtp.subject_prefix}FireMon Retrieval Digest - '
               f'{still_broken} still broken, {resolved} resolved')
    return subject, html_body, text_body


def send_digest_email(config, summaries, since, now):
    """Returns True if an email was actually sent, False if there was nothing
    to report or no recipients configured, and raises if sending failed."""
    if not summaries:
        logger.info('Nothing to report -- no digest sent.')
        return False
    if not config.smtp.recipients:
        logger.warning('%d device(s) to report but [smtp] recipients is empty -- not emailing.', len(summaries))
        return False

    subject, html_body, text_body = build_digest_email(config, summaries, since, now)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f'{config.smtp.from_name} <{config.smtp.from_address}>'
    msg['To'] = ', '.join(config.smtp.recipients)
    if config.smtp.reply_to:
        msg['Reply-To'] = config.smtp.reply_to
    msg.attach(MIMEText(text_body, 'plain'))
    msg.attach(MIMEText(html_body, 'html'))

    try:
        with smtplib.SMTP(config.smtp.host, config.smtp.port, timeout=config.smtp.timeout) as server:
            if config.smtp.use_starttls:
                server.starttls()
            if config.smtp.username:
                server.login(config.smtp.username, config.smtp.password)
            server.sendmail(config.smtp.from_address, config.smtp.recipients, msg.as_string())
        logger.info('Digest email sent to %s (%d device(s)).',
                     ', '.join(config.smtp.recipients), len(summaries))
        return True
    except (smtplib.SMTPException, OSError) as e:
        logger.error('Failed to send digest email: %s', e)
        raise
