# FireMon Retrieval Retry Alert

A standalone tool that watches FireMon device retrieval health, automatically
retries devices in a failure or stale state, and once a day emails a digest
of what's still broken, what fixed itself, and what's no longer being tracked.

## Why this exists

FireMon devices occasionally fail to retrieve or normalize their
configuration -- a transient network blip, a management-station hiccup, a
credential that briefly expired. Most of these resolve themselves with a
single manual retrieval, the same "Retrieve" button an operator would click by
hand. Nobody wants to click that button on a schedule, and nobody wants an
email every time a device that self-heals in ten seconds briefly showed an
error either.

This tool does the clicking automatically, on a short cycle (every few
minutes), and checks whether it actually worked (FireMon's manual-retrieval
endpoint is fire-and-forget -- it queues the job and returns immediately, it
does not tell you the outcome). Nothing is emailed per event: every device
that needed a retry gets recorded to a ledger, and once a day that ledger is
compiled into a single digest -- separate from how often the tool itself
checks and retries, so fast self-healing and a quiet inbox aren't a tradeoff
against each other.

## How it decides what to do

Each run:

1. Scans device health (`GET .../device/health`, filtered to whichever device
   types you configure -- management stations by default), excluding devices
   that are orphaned, unlicensed, name-pattern-excluded, or previously learned
   not to support manual retrieval at all (see **Exclusions** below).
2. Any device already in a failure status (`RETRIEVAL_ERROR`,
   `NORMALIZATION_ERROR`, `FAILURE` by default), or whose data is older than
   `stale_after_hours`, gets a manual retrieval triggered.
3. On the *next* run, a device that was retried is re-checked: if its health
   observation has moved on and it's now healthy, nothing happens -- resolved
   silently. If it's moved on and it's *still* unhealthy, that's recorded to
   the ledger. If nothing has changed at all after `grace_period_minutes`,
   that's also recorded ("no confirmation received"). Either way, if it's
   still unhealthy a fresh retry is queued immediately, in the same cycle --
   retries never stop just because something's been recorded.
4. Separately, every run checks whether `digest.interval_hours` has elapsed
   since the last digest. If so, it compiles everything in the ledger since
   then into one email, re-checking each device's *current* live status so
   the digest reflects reality rather than just replaying old events -- see
   **The daily digest** below.

This is a state machine across cron runs, not a script that blocks waiting for
a retrieval to finish -- there is no API to poll for "is this retrieval done
yet," only the same health endpoint you'd already be scanning. See
`config.example.ini` for every threshold, and DEPLOYMENT.md section on caveats
for what this design can and can't detect.

## The daily digest

No alert is ever emailed immediately. Instead, each digest groups everything
recorded since the last one into three sections:

- **Still Broken** -- needs a human look.
- **Resolved** -- it fixed itself since being recorded; informational only.
- **No Longer Tracked** -- the device was deleted, or is now excluded (see
  below). It was never actually fixed; it's just not being watched any more.

The cadence is controlled by `[digest] interval_hours` (default 24), and
there's no separate cron entry for it -- the same run that scans and retries
also checks whether a digest is due. Keep `interval_hours` comfortably larger
than how often cron actually runs, or every run will send one.

`run_retry_alert.py --send-digest` forces a send right now regardless of the
timer (and resets it) -- useful to verify the email actually renders and
delivers without waiting. `setup.sh send-digest` does the same thing through
the wizard's confirmation prompt.

## Exclusions

Four kinds of device are never retried or reported as broken, because
retrying them is either pointless or actively wrong:

| Kind | How it's detected | Config |
|---|---|---|
| Orphaned | Description contains "Orphaned by" | `ignore_orphaned` (default on) |
| Unlicensed | No `SM` (Security Manager) license | `ignore_unlicensed` (default on) |
| Name pattern | Device name contains a configured substring | `exclude_name_contains` (default empty) |
| Retrieved via parent | `retrievalTargetDeviceId` points at another device: cloud assets/VNETs under a cloud-provider device, firewalls under a Firepower Management Center | `ignore_child_devices` (default on) |
| Unsupported | FireMon's own API says so (see below) | always on, no setting |

A parent-retrieved child has no retrieval of its own. FireMon accepts a
manual retrieval POSTed to it, but the job can only fail, and that failure is
the `RETRIEVAL_ERROR` the tool would then report forever. Children are
skipped (the log says how many, per parent) and the parent, which is in the
scan in its own right, is what gets retried when it is failing or stale. This
is read from the device list once per cycle; the health list does not carry
it.

The first four are predictable in advance, from the device's own data. The
fifth isn't: nothing on a device says in advance "manual retrieval will
return 400 Device does not support manual retrieval" -- that's only
discoverable by actually trying it once. The first time a device fails that
way, it's recorded to the digest ledger once (so you find out) and added to
`state/unsupported_devices.json`, permanently excluding it from then on --
retrying something the server has already said will always fail the same way
is pure noise. To let a device be re-evaluated (e.g. after a FireMon upgrade),
remove its entry from that file, or clear it with `--reset-state`.

## Requirements

Designed to run **directly on the FireMon appliance (FMOS, Linux) as an
ordinary user** -- no root, no sudo, nothing installed outside your home
directory. It runs fine from any Linux workstation too, the only difference is
which host reaches the API and the mail relay.

- Python **3.7 or newer**, standard library only -- no `pip install`, no
  virtualenv, nothing to vendor (`requests` is deliberately not used; `pip`
  itself is often unavailable on the appliance). On FMOS the default
  `python3` may be **3.6**, which is too old for the `dataclasses` this tool
  uses; `run_retry_alert.py` and `run_tests.py` detect that and re-execute
  themselves under the newest installed interpreter, so the commands below
  work as written regardless of what `python3` points at.
- Access to the FireMon API. On the appliance that's `https://localhost` -- no
  DNS round trip and no firewall in the path.
- A way to schedule it. On FMOS that's your **user crontab**.
- An SMTP relay. FireMon exposes no mail relay through its API, so this needs
  its own. On the appliance, postfix is typically already listening on
  `127.0.0.1:25`.
- A FireMon account that can view device health and trigger a manual
  retrieval, with its password supplied via `FMRETRY_PASSWORD`.

## Quick start

Run the setup script with no arguments and you get a menu -- the front door:

```bash
cd ~/firemon-retry-alert
bash setup.sh
```

Before it's configured, the menu only shows what's reachable from a blank
slate:

```
  FireMon Retrieval Retry Alert
  /home/you/firemon-retry-alert

  not configured yet -- Configure or Full setup to begin

> 1) Configure       connection, device filter, and mail settings -- start here
  2) Full setup      configure, verify, then schedule in one pass

  3) Status          full configuration and schedule detail
  4) Uninstall       remove the schedule and, optionally, the files

  Up/Down or a number to choose  ·  Enter to run  ·  q to quit
```

Once `configure` has run, the menu grows to show everything else, with a
live dashboard (pending retries, ledger size, time to the next digest) at the
top and the cursor already parked on whatever's the sensible next step --
`Schedule` if nothing's scheduled yet, `Status` once it's live:

```
  FireMon Retrieval Retry Alert
  /home/you/firemon-retry-alert

  configured  ·  scheduled (live)
  pending: 2   ledger: 4 event(s)   next digest: 6h 40m (every 24h)

  1) Configure       connection, device filter, and mail settings
  2) Edit config     open config.ini in $EDITOR, re-check on save

  3) Verify          read-only checks; dry-run scan, sends nothing
  4) Pending/ledger  what's awaiting retry or the next digest
  5) Activity        recent cron.log, retry_alert.log, mail queue

  6) Run now         one real cycle off-schedule (asks first)
  7) Send digest     force-send the ledger now, reset the timer

  8) Schedule        currently live -- reinstall or promote
  9) Unschedule      remove the cron entry, leaving files alone

> 10) Status          full configuration and schedule detail
  11) Full setup      configure, verify, then schedule in one pass
  12) Uninstall       remove the schedule and, optionally, the files

  Up/Down or a number to choose  ·  Enter to run  ·  q to quit
```

`Unschedule` only appears once something is actually scheduled, and the
`Schedule` hint always names the current mode (`starts in --dry-run` /
`currently dry-run -- reinstall or promote` / `currently live -- reinstall or
promote`) -- picking it while already in `--dry-run` offers to promote to
`--live` on the spot instead of making you run `schedule --live` by hand.

Arrow keys (or `j`/`k`, or the number) move the highlight; Enter runs the
selected verb as its own step and returns to the menu when it finishes,
refreshing the dashboard and re-centering the cursor on the next sensible
action. A terminal that can't do the pointer UI (a dumb `TERM`, no real tty)
falls back to a plain numbered prompt with the same adaptive item list and
dashboard.

Every verb also runs directly, which is what cron and any scripted/unattended
use should do -- the menu is purely for humans:

```bash
bash setup.sh configure      # interactive wizard -> config.ini + secrets.env
bash setup.sh verify         # unit tests + a --dry-run scan against the live server
bash setup.sh schedule       # installs the cron entry, in --dry-run mode first
#   ... watch cron.log for a run or two ...
bash setup.sh schedule --live   # promote to real retries + real digest email

bash setup.sh edit-config    # open config.ini in $EDITOR, re-checks it on save
bash setup.sh list-pending   # what's awaiting retry confirmation or the next digest
bash setup.sh activity       # recent cron.log, retry_alert.log, and the mail queue

bash setup.sh update --check # is a newer version on GitHub?
bash setup.sh update         # download it and install it over this copy
bash setup.sh update --from Retrieval-Retry-Alert-main.zip   # no route to GitHub
```

`update` replaces only the code (`retry_alert/`, `templates/`, `tests/`,
`setup.sh`, `run_*.py`, `config.example.ini`, the docs). `config.ini`,
`secrets.env`, `state/`, `backups/` and the logs are never touched, the cron
entry is left alone, and the code it replaced is kept under
`backups/code-<timestamp>/` for a one-line rollback. It runs the unit tests
and a `--dry-run` load of your config afterwards.

`bash setup.sh help` lists every verb and option, including the environment
variables that let `configure` run unattended.

### Running it directly, without setup.sh

```bash
cp config.example.ini config.ini && chmod 600 config.ini
# edit config.ini, or export FMRETRY_PASSWORD instead of putting it in the file
python3 run_retry_alert.py --config config.ini --dry-run --log-level INFO
```

Useful flags: `--dry-run` (scan and log only, triggers nothing, emails
nothing, changes no state), `--send-digest` (force-send the ledger now,
regardless of the timer), `--list-pending` (show devices currently awaiting
retry confirmation and the digest ledger), `--reset-state --yes` (clear
pending retries, the ledger, and the digest timer), `--log-level DEBUG`.

Exit codes, meaningful for cron/monitoring: `0` clean run, `1` fatal error
(couldn't authenticate or scan, or the digest failed to send), `2` something
is recorded for the next digest and/or a digest was sent with a device still
broken.

## Configuration

See `config.example.ini` -- every key is commented there. The essentials:

| Section | Key | Meaning |
|---|---|---|
| `[firemon]` | `device_filter` | SIQL filter for which devices to watch. Blank = every device. |
| `[retry]` | `failure_statuses` | Which `revisionStatus` values count as "needs retrying." |
| `[retry]` | `stale_after_hours` | Retry a healthy-looking device whose data is this old. |
| `[retry]` | `grace_period_minutes` | How long to wait for a retry to show a new observation before recording "no confirmation." |
| `[retry]` | `ignore_orphaned` | Skip devices FireMon has marked orphaned (description contains "Orphaned by") -- on by default. |
| `[retry]` | `ignore_unlicensed` | Skip devices without the `SM` license -- on by default. |
| `[retry]` | `exclude_name_contains` | Skip devices whose name contains any of these substrings (comma-separated, case-insensitive). |
| `[retry]` | `ignore_child_devices` | Skip devices whose retrieval is done by a parent device (cloud assets, FMC-managed firewalls); the parent is retried instead -- on by default. |
| `[digest]` | `interval_hours` | How often the ledger is compiled into one summary email. |
| `[smtp]` | `recipients` | Who gets the digest email. Empty = recorded but never sent. |

`FMRETRY_PASSWORD` and `FMRETRY_SMTP_PASSWORD` (environment) always win over
`[firemon] password` / `[smtp] password` in the file -- `setup.sh` never
writes a password into `config.ini`, only into `secrets.env` (mode 600).

## Testing

```bash
python3 run_tests.py -v
```

No network calls -- the engine tests drive the state machine against a fake
FireMon client, and the config tests exercise `config.ini` parsing against
temp files.

## Files

```
run_retry_alert.py     thin entrypoint
retry_alert/
  _bootstrap.py         interpreter re-exec (old FMOS python3 -> a newer one)
  config.py              config.ini + env-var secrets -> AppConfig
  api.py                  FireMonClient: two-step auth, health scan, manual retrieval
  model.py                DeviceHealth / PendingRetry / AlertRecord / DigestDeviceSummary
  exclusions.py            shared "should we even consider this device" logic
  engine.py                the retry state machine + on-disk state store
  digest.py                compiles the ledger into a digest, live-rechecking each device
  mailer.py                builds and sends the digest email, in sections
  cli.py                    argparse entrypoint
templates/               digest.html / digest.txt -- edit these to restyle the email
config.example.ini       reference config, copy to config.ini
setup.sh                  configure / edit-config / verify / list-pending / activity / run-now /
                            send-digest / schedule / unschedule / status / update / uninstall / menu
VERSION                  what `setup.sh status` reports and `setup.sh update --check` compares
tests/                    unit tests, no network
```

The original single-file `retrieval_health_retry.py` is superseded by this
package -- it hardcoded credentials and treated the manual-retrieval POST
being *accepted* as the retrieval having *succeeded*, which it does not
verify. It's left in place for reference and can be removed once this tool is
validated in your environment.
