# Deploying Retrieval Retry Alert on FMOS

Written for running this **directly on a FireMon appliance, as an ordinary
user**. FMOS specifics below match the conventions already established by
`PolicyOptimizer-Email-Digest` in this repo -- if that tool is deployed on your
appliance, this one will feel familiar.

> **This is unmanaged software.** It is not part of the workflow pack and is
> not installed or upgraded by FireMon. Re-verify after every appliance
> upgrade -- see [After an FMOS upgrade](#after-an-fmos-upgrade).

---

## What the appliance actually provides

| | What you get | Consequence |
|---|---|---|
| **No root, no sudo** | `/opt`, `/etc` and `/etc/systemd/system` are read-only to you. | **Install into your home directory.** Nothing here needs privilege. |
| **Python** | `/usr/bin/python3` may be **3.6** -- too old (no `dataclasses`). Newer interpreters are usually installed alongside. | Nothing to do. `run_retry_alert.py` and `run_tests.py` re-execute under the newest interpreter automatically. |
| **Scheduling** | System units need root; user systemd units don't survive logout without linger, which FMOS can't enable. | **Use your user crontab.** `setup.sh schedule` manages it. |
| **Mail** | postfix is typically active on `127.0.0.1:25`. | No relay to configure for on-box use -- but prove it actually delivers, below. |
| **API** | FireMon answers on `https://localhost`. | `verify_tls = false` is standard on-box; TLS is self-signed. |

Confirm on your own box before starting:

```bash
python3 --version                # 3.6.x is fine -- the tool re-executes
ls /usr/bin/python3.[0-9]*       # expect a 3.7+ among these
crontab -l                       # "no crontab for <user>" is fine; the binary must exist
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost/securitymanager/api/authentication/login \
     -X POST -H 'Content-Type: application/json' -d '{"username":"USER","password":"PASS"}'
```

The last command should print `200`. `401` means bad credentials; anything
else means the API is not answering locally.

## Before you start

| What | Where it comes from | Notes |
|---|---|---|
| Service account | FireMon admin | needs permission to view device health **and** trigger manual retrieval on the devices `device_filter` matches |
| From address | your mail team | must be one the relay will accept |
| Digest recipients | you | who actually wants to see the daily digest |
| Outbound mail path | your mail team | whether this appliance may send externally **at all** |

That last row is the one that bites -- read
[Prove mail actually leaves the box](#4-prove-mail-actually-leaves-the-box)
before relying on this for anything.

---

## 1. Install

No privilege required.

```bash
mkdir -p ~/firemon-retry-alert
cp -r Retrieval-Retry-Alert/* ~/firemon-retry-alert/
cd ~/firemon-retry-alert
bash setup.sh configure
```

The wizard asks for the FireMon connection, the device filter, and the SMTP
settings, writes `config.ini` (0600) and `secrets.env` (0600, passwords only),
and proves the configuration against the live server (a `--dry-run` scan)
before writing anything for real. Nothing is promoted if that proof fails.

## 2. Verify

```bash
bash setup.sh verify
```

Runs the unit test suite, then a `--dry-run` scan against your real server,
then prints current pending-retry state. A `--dry-run` scan exiting `2`
(devices found that would be retried/alerted) is expected and fine on a box
with pre-existing issues -- `--dry-run` never triggers a retrieval or sends
mail, so nothing has actually happened yet.

## 3. Schedule

```bash
bash setup.sh schedule            # installs with --dry-run, every 10 minutes
#   ... watch cron.log for a few runs ...
bash setup.sh schedule --live     # promote to real retries + real digest mail
```

`--at "M H D M W"` changes the cadence (default `*/10 * * * *`). Keep the
interval comfortably shorter than `[retry] grace_period_minutes` divided by
2-3, so a stuck device is caught within a run or two rather than immediately
timing out on the very next tick.

## 4. Prove mail actually leaves the box

`setup.sh verify` and the tool's own logging only tell you the SMTP relay
*accepted* a message -- the last thing any SMTP client can observe. Confirm it
was actually delivered before trusting alerts to reach anyone:

```bash
mailq                              # should be empty after a real send
postconf -h relayhost myhostname sender_canonical_maps
```

If your `from_address` isn't one the relay will send externally, or postfix
rewrites the sender via `sender_canonical_maps`, the recipient may never see
it even though this tool logged success.

## 5. What this design can and cannot detect

`manualretrieval` is fire-and-forget -- it queues a job and returns
immediately, with no response body indicating the outcome, and there is no
job-status endpoint to poll. The only signal available afterward is the same
device-health endpoint everything else uses: `retrievalLastUpdated` and
`revisionStatus`/`retrievalStatus` moving to a new value.

Consequences worth knowing before you rely on this:

- If a device's own scheduled retrieval happens to land in the same window as
  the manual retry this tool triggered, the tool can't tell those apart --
  it will read whichever observation shows up first as "the result of my
  retry." In practice this is rare (retries are triggered specifically
  *because* the device wasn't retrieving on its own), but it's an inherent
  limitation of the API, not a bug you can fix by tuning thresholds.
- A device that genuinely takes longer than `grace_period_minutes` to
  retrieve and normalize (a very large ruleset, a slow management station)
  will be recorded as "no confirmation received" even though it may still
  succeed a few minutes later. Raise `grace_period_minutes` if this happens
  often for a particular device class.
- Manual retrieval requires the service account to have that permission on
  every device `device_filter` matches. A permission error surfaces as a
  `trigger_error` event ("could not trigger manual retrieval") in the next
  digest, not a silent failure -- but every affected device will show up the
  first time it's tried. Distinct from this: some devices return a specific
  "does not support manual retrieval" error regardless of permissions -- those
  are detected, recorded once, and permanently excluded (see README's
  **Exclusions** section), so they don't repeat in every digest.

## Log rotation and state

`setup.sh schedule` installs a monthly cron block (skip with
`--no-logrotate`) that trims `cron.log` and `retry_alert.log` to their last
2000 lines. It never touches `state/pending.json`, `state/ledger.jsonl`,
`state/digest_state.json`, or `state/unsupported_devices.json` -- those are
live data the tool needs across runs, not a log (the ledger and digest timer
are managed by the digest logic itself: emptied right after each successful
send, not by a cron-based rotation). `bash setup.sh status` shows state file
sizes; `--list-pending` (via `run_retry_alert.py`) shows the pending retries
and ledger count.

## Uninstall

```bash
bash setup.sh uninstall
```

Removes the cron entry, then asks before deleting `secrets.env` (it holds a
plaintext password -- deleted, not archived) and before archiving
`config.ini` to `backups/`. `state/`, `cron.log` and `retry_alert.log` are
left alone, since they're the only record of what was actually retried and
reported on.

**Upgrading to a newer revision of this tool, rather than uninstalling:**

```bash
bash setup.sh update --check     # compare this copy's VERSION with GitHub
bash setup.sh update             # download, install, run the unit tests
bash setup.sh verify             # then the full read-only check
```

`update` replaces only the code files (`retry_alert/`, `templates/`,
`tests/`, `run_retry_alert.py`, `run_tests.py`, `setup.sh`,
`config.example.ini`, `VERSION`, the docs). `config.ini`, `secrets.env`,
`state/`, `backups/` and the logs are left alone, the cron entry is not
touched, and the replaced code is kept under `backups/code-<timestamp>/`, so
rolling back is `cp -Rp backups/code-<timestamp>/* .`. If the appliance has
no route to github.com, download
`https://github.com/adamgunderson/Retrieval-Retry-Alert/archive/refs/heads/main.zip`
elsewhere, copy it over, and run `bash setup.sh update --from main.zip`.

New settings a release adds have defaults, so an older `config.ini` keeps
working. To set one, use `bash setup.sh edit-config` (adds just that key)
rather than re-running `configure`: the wizard rebuilds `config.ini` from
`config.example.ini` and only patches in the values it prompts for, so any
hand-edited keys it does not ask about (`failure_statuses`,
`stale_after_hours`, `grace_period_minutes`, `ignore_*`, `subject_prefix`)
would revert to their defaults. Don't `uninstall` first either; that drops
the cron entry and prompts to delete the password and config you'd just have
to re-enter.

## After an FMOS upgrade

Re-run `bash setup.sh verify`. An upgrade can move or remove the newer Python
interpreters this tool re-execs into, or reset postfix configuration.
`bash setup.sh status` shows what's currently configured and scheduled
without changing anything, which is the fastest way to see what an upgrade
broke.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `configure` fails at "Proving the configuration" | Bad credentials, wrong `base_url`, or the account lacks device-health read access. Check the error text -- it's the tool's own log output. |
| Every device records a `trigger_error` on the first live run | The service account can view health but can't trigger manual retrieval. Check its role/permissions in FireMon. |
| Azure/AWS assets, VNETs or FMC-managed firewalls show `RETRIEVAL_ERROR` after every retry and never recover | They are retrieved by their parent device, not on their own; a retry POSTed to the child can only fail. Versions before 1.2.0 did exactly that. 1.2.0 skips them (`ignore_child_devices`, on by default) and retries the parent. The log says how many were skipped, per parent. |
| `Could not trigger manual retrieval ... 404 ... not assigned to a data collector group` | Either a synthetic router / hand-maintained device (`managedType` MANUAL -- skipped automatically from 1.3.0, `ignore_manual_devices`), or a real device nobody assigned to a collector group. For the latter, assign one in FireMon, or add its name to `exclude_name_contains`. |
| Digest never arrives but `cron.log` says it was sent | Mail left the tool but not the box, or the box but not the recipient -- see [Prove mail actually leaves the box](#4-prove-mail-actually-leaves-the-box). |
| A digest goes out on nearly every cron run | `[digest] interval_hours` is set lower than you intended, or close to the cron cadence -- check `bash setup.sh status`. |
| The same device keeps showing up every digest, never resolving | If the detail text says "does not support manual retrieval", that's expected until the fix picks it up (next cycle after this one) -- after that it should move to "No Longer Tracked" permanently. Otherwise, check whether it's genuinely retrying each cycle (`--list-pending`) or stuck for some other reason. |
| `run_tests.py` fails only under `/usr/bin/python3` | Expected on FMOS's default 3.6 if the re-exec bootstrap can't find a 3.7+ interpreter on `PATH`. Run `ls /usr/bin/python3.[0-9]*` and confirm one is on `PATH`. |
