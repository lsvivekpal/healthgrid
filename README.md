# RDS Dashboard

A Django application for PostgreSQL/RDS lock and session monitoring, controlled backend termination, replication-slot management, Teams notifications, and weekly lock-history reports.

Instances are registered manually. The app connects directly to PostgreSQL; it does not discover instances through AWS or delete AWS RDS infrastructure.

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Local setup](#local-setup)
- [Production deployment](#production-deployment)
- [Environment configuration](#environment-configuration)
- [Registering databases](#registering-databases)
- [Dashboard and lock control](#dashboard-and-lock-control)
- [Users, roles, and MFA](#users-roles-and-mfa)
- [Teams notifications](#teams-notifications)
- [Long-running query alerts](#long-running-query-alerts)
- [Weekly reports and OneDrive](#weekly-reports-and-onedrive)
- [Storage, retention, and backups](#storage-retention-and-backups)
- [Routes and API](#routes-and-api)
- [Operations and troubleshooting](#operations-and-troubleshooting)
- [Security and current limitations](#security-and-current-limitations)
- [Code map and tests](#code-map-and-tests)

## Features

- Instance registry: add, duplicate, rename, test connections, and remove registrations with authorization and MFA.
- Home dashboard: total/healthy instances, blocked locks, unreachable instances, 10-second refresh, pause/resume, and refresh now.
- Live sessions: users, client addresses, application names, state, wait events, query previews, durations, filtering, sorting, and CSV export.
- Locks: blocked/blocking PID pairs, individual termination, role-based selection, bulk termination, and chain termination.
- Database vitals: connection usage, session mix, longest active client query, database size, and ten largest tables.
- Replication slots: status, active PID, retained WAL, and independently authorized backend termination and slot dropping.
- Accounts: Administrator, Operator, and Read-only access; Google Authenticator-compatible MFA and per-user recovery codes.
- Teams: shared-channel and owner webhooks, test messages, deduplicated lock summaries, IST timestamps, and configurable quiet hours.
- Long-running query alerts for non-excluded database users.
- Weekly lock-history CSV reports through the existing Teams Workflow, with OneDrive file creation handled by that workflow.
- Responsive forms/tables, mobile session sorting, light/dark themes, accessible confirmation dialogs, and persistent error messages.
- Audit history for control actions, protected-action MFA failures, and operator replication-permission changes.

## Architecture

The web application and embedded monitor run in the same application container. No separate worker container or logged-in browser is needed for automatic notifications.

| Component | Responsibility |
| --- | --- |
| Application database (`DATABASE_URL`) | Users, encrypted connection credentials, MFA, grants, notification settings, incident history, temporary reports, and audits |
| Monitored PostgreSQL databases | Live catalog queries and explicitly authorized backend/slot operations |
| Embedded monitor | Checks active registrations, updates history, sends notifications/reports, and runs retention cleanup |
| Teams Workflow / Power Automate | Receives webhook payloads, posts cards, and creates CSV files/share links in OneDrive |

The monitor uses a database-backed lease to coordinate processes. Replicas must share the same application database and encryption/signing keys. This is not an exactly-once delivery guarantee: long cycles and external failures still matter. Start with one application worker and monitor cycle duration.

Closing the browser or pausing UI refresh does **not** stop monitoring. Stopping the app does.

## Local setup

### Docker Compose

Requirements: Docker with Compose; available ports 8000 and 5433. Run from the repository root:

```bash
docker compose build web
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose run --rm --no-deps -e DISABLE_EMBEDDED_LOCK_MONITOR=1 web python manage.py createsuperuser
docker compose up -d
```

Open [http://localhost:8000](http://localhost:8000), sign in, and complete authenticator enrollment.

The supplied Compose configuration is development-only:

- `web` mounts the repository at `/app`, enables debug mode, and uses Django's development server.
- The application's default SQLite database is the repository's `db.sqlite3`.
- `target-db` is disposable PostgreSQL 16 to **monitor**, not the application database.
- Development credentials and encryption/signing defaults must not be reused in production.

Register the local test database from the containerized dashboard using:

| Field | Value |
| --- | --- |
| Host | `target-db` |
| Port | `5432` |
| Database | `appdb` |
| Username | `appuser` |
| Password | `apppass` |
| Require SSL | Unchecked |

For Django running directly on your host, use `localhost:5433` instead. `localhost` inside the web container refers to that container itself.

```bash
docker compose logs --tail=100 web
docker compose ps
```

The Compose file does not configure a durable named volume for `target-db`; do not rely on it for persistent test data after container replacement.

### Python development

Use Python 3.12, matching the Docker image:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DJANGO_DEBUG=true
python manage.py migrate --noinput
DISABLE_EMBEDDED_LOCK_MONITOR=1 python manage.py createsuperuser
python manage.py collectstatic --noinput
python manage.py runserver
```

The app reads process environment variables; it does not automatically load `.env`. Docker's `--env-file` loads one explicitly. Compose `.env` interpolation alone does not inject every variable into the web service.

## Production deployment

### Preparation and persistent storage

1. Back up the application database and preserve its existing encryption/signing keys.
2. Build from a clean checkout without local database copies, secrets, or sensitive workflow exports. The Dockerfile copies the build context into the image.
3. Configure durable application storage, explicit allowed hosts, HTTPS, and least-privilege database/network access.
4. Build/publish the image, apply migrations against the intended application database, then replace the running service with the new image.
5. Verify login/MFA, CSS, role-specific actions, monitoring logs, and a real Teams/OneDrive test.

Prefer an external PostgreSQL application database for shared/multi-replica deployments. Never keep the only application database inside an ephemeral container filesystem. A single-instance SQLite deployment needs a persistent volume and backups.

Generate keys only for a **new installation**, using the installed Python dependencies:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(64))'
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Save the first value as `DJANGO_SECRET_KEY` and the second as `ENCRYPTION_KEY` in your secret store. Changing the encryption key makes existing encrypted DB passwords and MFA secrets unreadable; changing the signing key invalidates sessions and signed report links.

Example private `.env.prod` for persistent single-instance SQLite; replace both secret placeholders:

```dotenv
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=REPLACE_WITH_A_STRONG_RANDOM_SECRET
ENCRYPTION_KEY=REPLACE_WITH_A_VALID_FERNET_KEY
DJANGO_ALLOWED_HOSTS=dashboard.example.com,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://dashboard.example.com
DJANGO_USE_HTTPS=true
DATABASE_URL=sqlite:////data/dashboard.sqlite3
ACTIVITY_CACHE_TTL=10
```

Set `DJANGO_USE_HTTPS=true` only behind a trusted TLS-terminating proxy that sets `X-Forwarded-Proto` correctly. Keep the environment file private and out of version control.

Example first deployment behind a reverse proxy on the same host:

```bash
docker build -t rds-dashboard:release-001 .
docker volume create rds-dashboard-data
docker run --rm --env-file .env.prod -v rds-dashboard-data:/data rds-dashboard:release-001 python manage.py migrate --noinput
docker run --rm -it --env-file .env.prod -e DISABLE_EMBEDDED_LOCK_MONITOR=1 -v rds-dashboard-data:/data rds-dashboard:release-001 python manage.py createsuperuser
docker run -d --name rds-dashboard --restart unless-stopped \
  --env-file .env.prod -v rds-dashboard-data:/data \
  -p 127.0.0.1:8000:8000 rds-dashboard:release-001 \
  gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 60
```

The Dockerfile's default command is still `runserver`; override it with Gunicorn in production, including ECS task definitions. Do not use Gunicorn `--preload` with the current embedded-thread startup. Adapt networking when using an external load balancer rather than a host-local proxy.

For upgrades, use a new image tag/digest, run `migrate --noinput` with the same database configuration, then update the service through its normal deployment process. Migration `0024_replication_slot_access` is required for operator slot grants. Preserve migration history; do not delete migrations already applied in production.

Do not mount old source over `/app` or stale assets over `/app/staticfiles` in production: those mounts hide files from the new image.

### Multi-platform builds and ECR

If Docker reports that its current driver cannot build multiple platforms, use a `docker-container` builder. If it already exists, run `docker buildx use rds-dashboard-builder` instead of recreating it. See [Docker's multi-platform guidance](https://docs.docker.com/build/building/multi-platform/).

```bash
docker buildx create --name rds-dashboard-builder --driver docker-container --use
docker buildx inspect --bootstrap
```

Example using the AWS CLI **default profile**; replace the account ID, repository, and release tag. The ECR repository must exist:

```bash
RDS_ECR_REGISTRY=123456789012.dkr.ecr.ap-south-1.amazonaws.com
RDS_IMAGE_TAG=release-001
aws --profile default ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin "$RDS_ECR_REGISTRY"
docker buildx build --builder rds-dashboard-builder \
  --platform linux/amd64,linux/arm64 \
  -t "$RDS_ECR_REGISTRY/awsdashboard:$RDS_IMAGE_TAG" \
  -t "$RDS_ECR_REGISTRY/awsdashboard:latest" --push .
```

Authentication follows [AWS's ECR login instructions](https://docs.aws.amazon.com/cli/latest/reference/ecr/get-login-password.html). Prefer deploying the release tag/digest. A push does not update running services automatically. Multi-platform `--push` does not load a local runnable image; use `docker compose build web` for local testing.

### Production static files

The image build runs `collectstatic`. WhiteNoise serves collected files with debug disabled, and content-hashed stylesheet URLs avoid stale browser caches. See the [WhiteNoise Django integration](https://whitenoise.readthedocs.io/en/stable/django.html).

If a proxy intercepts `/static/`, forward requests to the application or serve assets from the **same release**. Verify `/static/monitor/responsive.<hash>.css` returns HTTP 200 with `Content-Type: text/css`, not HTML or a login redirect. Do not enable debug mode to fix production CSS.

## Environment configuration

Notification options and monitored-database credentials are managed in the UI. Infrastructure settings remain environment variables:

| Variable | Default / purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Insecure development fallback; configure a stable strong secret |
| `ENCRYPTION_KEY` | Development Fernet key; configure/preserve your own before storing production credentials |
| `DJANGO_DEBUG` | `false`; development Compose explicitly sets `true` |
| `DJANGO_ALLOWED_HOSTS` | `*`; replace with comma-separated production hostnames |
| `DATABASE_URL` | SQLite at `/app/db.sqlite3` in the image; configure durable storage |
| `DJANGO_USE_HTTPS` | `false`; enables secure cookies in non-debug mode and trusts forwarded protocol when `true` |
| `CSRF_TRUSTED_ORIGINS` | Empty; comma-separated trusted origins including scheme |
| `ACTIVITY_CACHE_TTL` | `10` seconds; per-process cache of live dashboard reads |
| `DISABLE_EMBEDDED_LOCK_MONITOR` | `1` disables embedded polling, automatic reports, and automatic cleanup in that process |

Notification cards, weekly report timestamps, and schedules use IST (`Asia/Kolkata`). Django's configured timezone is UTC; not all logs or ad-hoc CSV timestamps are IST.

## Registering databases

Operators and Administrators can use **Add instance** to configure a friendly name, database identifier, region, endpoint, port, database name, credentials, SSL preference, optional control credentials, and optional owner Teams webhook.

Use **Test connection** before saving. Existing instance pages support rename, duplicate, owner-webhook update/test, and lock-control credential update/test. A configured control username needs its password; clearing that username restores fallback to the regular DB credentials.

### PostgreSQL privileges and control connections

Have a DBA provision an appropriate login role. A starting point for an existing control role is:

```sql
GRANT pg_read_all_stats TO db_lock_admin;
GRANT pg_signal_backend TO db_lock_admin;
```

`pg_monitor` is a broader monitoring-role alternative. Signal privileges do not permit terminating superuser backends. Review [PostgreSQL's predefined roles](https://www.postgresql.org/docs/current/predefined-roles.html) for your server version.

Slot management needs separate PostgreSQL/RDS privileges; a dashboard grant does not grant database-level replication rights. Have your DBA validate [replication-management permissions](https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-REPLICATION). Do not broadly grant superuser rights simply to make a UI action work.

Control connections use application name `rds-dashboard-control`, with a 5-second connect timeout, 8-second statement timeout, 2-second lock timeout, and 10-second idle-in-transaction timeout.

A blocked connection belonging to `db_lock_admin` does not necessarily block every new connection using that username. The dashboard opens independent connections; access still depends on network reachability, connection capacity, and DB privileges. It does not automatically kill its own PIDs to recover access.

Require SSL selects `sslmode=require`; unchecked selects `prefer`. Neither is hostname-verifying `verify-full`.

## Dashboard and lock control

### Live views

- Home refreshes every **10 seconds** with pause/resume and manual refresh.
- KPI cards keep the normal background; blocked/unreachable counts use healthy colouring when zero.
- Instance detail defaults to **15 seconds**, with 5/10/15/30/60-second choices saved in the browser.
- Browser refresh is independent of background **Check every**. Cached data can remain unchanged until its TTL expires.
- Sessions include `active`, `idle in transaction`, and `idle in transaction (aborted)`. Plain idle connections are omitted from the table, but included in applicable vitals.
- Sort sessions by PID, user, client/application, state, wait event, duration, or query. Mobile has a sort selector. Filtering/sorting is retained after refresh.
- Vitals show connection usage, active/idle counts, longest active client query, DB size, and the ten largest tables.
- Locks/sessions offer on-demand CSV exports. Previews are shortened; exports contain SQL returned by PostgreSQL, which can itself be limited by server activity-query settings.

### Selecting and terminating PIDs

- An individual lock kill verifies that the PID is currently part of an observed lock and targets that PID only.
- Blocked-only/blocking-only quick selection excludes PIDs having both roles in a chain. Select mixed-role PIDs explicitly if needed.
- Bulk actions target selected PIDs; chain actions may include blocked and blocking PIDs. Review the confirmation before proceeding.
- **Kill by PID** supports sessions not currently visible in the table and uses the live-session MFA check.
- Generic PID/bulk/chain kills protect replication-slot backends unless the operator has termination access.

Actions use `pg_terminate_backend`, not query-only cancellation. Sessions can end and open transactions can roll back. An immediate success can mean the signal was sent; verify the result on refresh. See [PostgreSQL server signalling](https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADMIN-SIGNAL).

The confirmation dialog pauses live swaps while open. There is no automatic lock-killing policy. Removing a dashboard registration does not delete the RDS database, but does remove its associated application incident history.

## Users, roles, and MFA

### Permission matrix

Administrator means an active Django superuser with staff access—not a username or a group merely named Administrator.

| Capability | Administrator | Operator | Read-only |
| --- | --- | --- | --- |
| View dashboard/locks/sessions/slots/audits and export CSV | Yes | Yes | Yes |
| Add/duplicate/rename instances; edit credentials/webhooks | Yes | Yes | No |
| Configure notifications; send tests/manual reports | Yes | Yes | No |
| Individual lock kill | Yes | Yes | No |
| Live-session and bulk/chain kills | Yes, MFA | Yes, MFA | No |
| Terminate slot backend | Yes | Explicit terminate grant | No |
| Drop slot | Fresh MFA | Explicit drop grant + fresh MFA | No |
| Remove instance | Fresh MFA | No | No |
| Create users; grant/revoke operator slot permissions | Yes | No | No |

Application authorization never overrides the connected PostgreSQL role's privileges.

### User management

As Administrator, open account menu → **User management**:

1. Create an Operator or Read-only account with a temporary password. Optional email is contact information, not a Teams destination.
2. Optionally grant an operator **Terminate slot backend** and/or **Drop slot**.
3. For existing operators, change the checkboxes in the user table and select **Save permissions**.

Both grants default to off, are independent, and apply to **all registered instances**. Drop permission does not include termination; an active slot may require an independently authorized termination first. Revocation is enforced on subsequent requests. Grant changes are audited. Read-only/inactive users cannot exercise grants even if records exist.

The custom user page creates accounts and manages slot grants; it is not a complete account-lifecycle or per-database-access console. There is no shared multi-device Administrator MFA configuration: use separate accounts/devices.

### Authenticator enrollment and action checks

First password login directs users to **Authenticator security**. Scan the QR code or enter the setup key in Google Authenticator or another compatible TOTP app, save the recovery codes, and confirm enrollment. Once enabled, subsequent normal logins require password plus authenticator/recovery verification.

The normal reset UI requests a current code before enrolling a replacement device. Recovery codes are one-time and hashed; MFA secrets and stored database passwords are encrypted with `ENCRYPTION_KEY`.

Current action rules:

- Instance removal and slot drop require a code submitted for **that action**, even just after MFA login. API removal follows the same rule.
- Live-session and bulk/chain dialogs ask for MFA. Their server helper also accepts a verification from the last **300 seconds** if no code is submitted.
- Individual lock kills do not require step-up MFA.
- Dedicated slot-backend termination requires its role/grant but currently has no step-up MFA requirement. Generic live-session termination retains its own MFA rule.
- Django admin instance deletion, including bulk deletion, is disabled to prevent bypassing removal MFA.

See [Security and current limitations](#security-and-current-limitations) for onboarding/reset enforcement gaps before public exposure.

## Teams notifications

### Configuration and defaults

Open account menu → **Notification settings**. These settings are global, not per-instance overrides; the optional owner webhook is per instance.

| Setting | Default / meaning |
| --- | --- |
| Shared channel webhook | Blank; Workflow destination for alerts and reports |
| Alert after | `120` seconds; lock eligibility threshold |
| Check every | `30` seconds; background sleep between cycles |
| Notification schedule | Disabled; uses IST when enabled |
| Schedule start/end | `09:00` / `21:00` |
| Keep resolved records | `30` days; UI range 7–3650 |
| Long-running query alerts | Disabled |
| Long-query threshold | `60` seconds |
| Excluded query users | `applms,applos` |
| Weekly report | Disabled; Monday at 09:00 IST when enabled |
| Public dashboard URL | Blank; enables signed report downloads |

Save settings before **Send test to shared channel**. Add/edit/test an owner webhook on its instance page or while adding an instance. The workflow determines whether the owner destination is a chat or channel; the app does not resolve owner emails.

The app can send alerts to both destinations, deduplicating identical webhook URLs for alert messages. No separate CSV-upload webhook is required.

### Timing and lock summaries

The monitor checks registrations sequentially, then sleeps at least 5 seconds, normally the configured interval. Database/network work adds to the cycle duration.

Eligibility uses `waiting_seconds`, calculated from the blocked query's `query_start`, **not an independently measured lock-wait start**. With 30-second checks and a 70-second threshold, a query starting at zero and sampled at 30/60/90 seconds normally first qualifies around 90 seconds. An already-old query can qualify immediately when observed blocked. This is not a delivery SLA; incidents between polls can be missed.

Notifications are aggregated per registration:

1. The first eligible locks produce an initial summary.
2. Changes in eligible PID pairs, usernames, or SQL produce an update.
3. If 30 of 100 eligible locks clear, the next update describes the remaining 70 and cleared pairs—not 30 individual cleared cards.
4. A final clear is sent when no alert-eligible tracked pairs remain. Below-threshold locks can still exist outside the summary.

Unchanged locks do not generate periodic duplicate cards. The app does not update/reply to an existing Teams thread. Cards include status, PID pairs, query previews, clear details, and observed/sent times in IST. Elapsed-time changes alone do not send new cards; use the dashboard for live status.

Successful dashboard kills mark affected incidents with the dashboard username for subsequent clear reporting. External kills cannot reliably be attributed to the external user; disappearance without dashboard attribution is recorded as an automatic clear. A failed DB check is not treated as proof of clearance.

### Notification schedule

Quiet hours suppress lock and long-query notifications while history collection continues. The start time is inclusive; end time exclusive. Overnight windows such as 21:00–09:00 work; equal times are rejected by the UI.

Eligible locks surviving until the window opens get a fresh summary. Quiet-hour events are not replayed individually. Weekly reports and manual test/report actions are not suppressed by the schedule.

There is no current “repeat every” or “checks observed” setting; older migrations may refer to previous notification designs.

## Long-running query alerts

Enable **Long-running manual queries**, choose a duration, and enter comma-separated application usernames to exclude.

This checks active queries from any non-excluded user, **not only DBeaver/TablePlus**. Application names do not prove human activity. Username exclusions are case-insensitive; `rds-dashboard-control` application connections are excluded automatically.

Cards include database, user, PID, application/client details, SQL, duration, and IST time. PID/user/query-start/SQL identifies an execution: unchanged executions alert once, while a new execution can alert again. Finished/changed executions are marked resolved; there is no separate long-query cleared card.

Messages reuse `event_type=lock_alert` with `alert_type=long_query`. Long-query history is **not currently included in the weekly lock CSV**.

## Weekly reports and OneDrive

### Schedule and contents

Enable weekly reporting, select weekday/hour in IST, or choose **Send weekly report now**. Automatic sending occurs on the selected weekday at or after the selected hour, tracked once per scheduled ISO week. There is no arbitrary missed-day catch-up; manual sends do not advance that scheduled-week marker.

The shared webhook receives an all-instance report. Each active instance with an owner webhook receives its own report. CSVs include lock incidents observed, updated, or cleared in the last seven days, including older active incidents seen during that period.

Filename: `rds-lock-report-YYYYMMDD-HHMMSS.csv`, using IST. Columns:

```text
database, db_identifier, region, status,
blocked_pid, blocked_user, blocking_pid, blocking_user,
first_observed_ist, last_observed_ist, cleared_at_ist,
clear_reason, cleared_by, waiting_seconds, blocked_query, blocking_query
```

Channel and owner reports generated in the same run use the same timestamp-based filename. Configure separate destination folders or unique names in each workflow if they share OneDrive storage, to prevent file-name collisions.

This is recorded lock history, not every SQL execution. Cards show limited query pairs/previews; CSVs contain stored SQL. Archive reports before removing a registration if you need its history afterward.

### Workflow payload contract

Branch on top-level `triggerBody()?['event_type']`:

| Event | Workflow behaviour |
| --- | --- |
| `lock_alert` | Post `triggerBody()?['card']` as the Teams Adaptive Card; includes tests and long-query alerts |
| `weekly_report` | Read `file_name`, `csv_content`, `report_url`, `report_rows`, and `card`; create/download the CSV, generate its OneDrive link, then post the card/link |

`card` is the Adaptive Card object with `type: AdaptiveCard`. Do not pass the entire outer envelope to the Teams card field.

For weekly reports:

1. If `report_url` is non-empty, download it using the configured file-from-URL action.
2. Otherwise use **Create file**, with name `triggerBody()?['file_name']` and content `triggerBody()?['csv_content']`.
3. Create a suitably restricted share link, then post the card and link to Teams.

The supplied [workflow exports](flow/) use OneDrive for Business and default folder `/RDS Dashboard/Weekly Reports/`. Create/select the folder and rebind connections, Teams destinations, and sharing settings during import. Exports are environment-specific examples, not portable credentials. Verify connector availability/licensing in your Microsoft tenant; this repository does not guarantee a premium-free flow.

### Inline CSV versus signed URL

- **Blank public URL:** CSV travels in the webhook, so Power Automate need not reach the app. The current inline limit is **20 KiB of UTF-8 CSV content**.
- **Public URL configured:** The app sends a signed `report_url` and empty `csv_content`. Power Automate must reach the URL; use publicly reachable HTTPS. `localhost`, private-only hosts, and proxy login challenges will not work from Microsoft's cloud service.
- **Large CSV without a public URL:** Sending fails and logs the size issue. Automatic splitting or a secondary upload service is not implemented.

Signed links expire after seven days and do not require dashboard login. Anyone holding a valid link can retrieve the CSV until expiry; treat it as a sensitive bearer link.

### Delivery verification

“Sent” means at least one webhook accepted the HTTP request. It does **not** prove all destinations succeeded, the Teams card appeared, or OneDrive stored the file. Check Flow run history, the destination folder, and the final Teams message. There is no upload-confirmation callback or per-destination retry ledger.

## Storage, retention, and backups

The application stores incident snapshots/SQL for reporting; it does not copy PostgreSQL business data or continuously archive all queries.

| Data | Retention |
| --- | --- |
| Active lock / long-query incidents | Never deleted by automatic retention cleanup |
| Resolved lock / long-query incidents | Deleted after configured days since resolution |
| Temporary `LockReport` CSV payloads | Expire after seven days; expired records are cleaned |
| Audit logs, users, settings, MFA, grants | No general automatic audit/account retention policy |
| OneDrive CSV files | Governed by your workflow/OneDrive policies |

Cleanup runs approximately daily through the embedded monitor, **not immediately after an upload**. Sending reports also removes expired report payloads. Stopping/disabling the embedded monitor stops its scheduled cleanup. Active records can remain if their database is unreachable or no longer monitored.

Verify archival before relying on OneDrive, keep sufficient retention for recovery, and back up the application database and encryption/signing keys. SQL/report content can contain sensitive literals; treat imported query text as untrusted data rather than spreadsheet formulas.

Row deletion does not necessarily shrink allocated database files. Use a DB-specific maintenance plan; the app does not automatically run SQLite `VACUUM` or equivalent reclamation. Monitor audit growth and temporary reports created by repeated failed sends too.

## Routes and API

The browser is the primary administration interface. REST uses Django session authentication, not API keys. Authenticated unsafe requests need CSRF protection; normal login/MFA happens through the web flow.

| Route | Function |
| --- | --- |
| `/`, `/instances/<id>/` | Instance list/detail |
| `/login/`, `/logout/`, `/mfa/verify/` | Login, POST logout, MFA verification |
| `/settings/security/mfa/` | Enrollment/recovery/reset UI |
| `/settings/users/` | Administrator-only account creation and operator slot grants |
| `/settings/notifications/` | Thresholds, schedule, exclusions, reporting, retention |
| `/settings/notifications/test/`, `/settings/notifications/weekly-report/` | POST test / manual report |
| `/instances/add/`, `/instances/test-connection/` | Add/duplicate and connection test |
| `/instances/<id>/card/`, `/instances/<id>/activity/`, `/instances/<id>/replication-slots/` | Live partials |
| `/instances/<id>/rename/`, `/instances/<id>/owner-webhook/` | POST rename/webhook update |
| `/instances/<id>/control-credentials/`, `/instances/<id>/test-control-connection/` | POST control credential update/test |
| `/instances/<id>/locks/download/`, `/instances/<id>/sessions/download/` | Live CSV exports |
| `/instances/<id>/kill/`, `/instances/<id>/kill-chain/` | POST session / bulk-chain termination |
| `/instances/<id>/locks/kill/`, `/instances/<id>/locks/kill-chain/` | POST validated lock-only termination |
| `/instances/<id>/replication-slots/kill/`, `/instances/<id>/replication-slots/drop/` | POST authorized slot actions |
| `/instances/<id>/remove/` | POST Administrator + fresh-MFA removal |
| `/reports/weekly/<signed-token>/` | Expiring CSV download without login |
| `/api/instances/`, `/api/instances/<id>/` | Registry list/detail/create/update/delete |
| `/api/audit-logs/`, `/api/audit-logs/<id>/` | Read-only audit API; list filter `?instance=<id>` |
| `/admin/` | Django admin with MFA routing; instance deletion disabled |
| `/healthz` | HTTP 200 `{"status":"ok"}`; liveness only, not DB/monitor/Teams readiness |

Staff can create/update API instances; only Administrators can DELETE. Removal requires a JSON DELETE body containing `mfa_code`. `password` and `control_password` are write-only serializer inputs. Setting `is_active=false` stops normal home-list/background monitoring without deleting registration/history.

UI action failures commonly redirect with a message; role violations return 403. A 302 alone does not prove success—check the message/audit entry. API deletion rejects missing/invalid MFA with 403.

## Operations and troubleshooting

```bash
docker compose logs --tail=100 web
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py showmigrations monitor
docker compose exec -T web python manage.py migrate --noinput
curl -f http://localhost:8000/healthz
```

Run production migrations with the intended production database configuration, not against an accidental SQLite database in a disposable container.

Diagnostic monitor pass:

```bash
python manage.py monitor_locks --once
```

This is **not a dry run**: it updates incident records and can send configured notifications. The optional command supports `--interval SECONDS`, but is unnecessary alongside the embedded monitor and does not run the embedded report/cleanup tasks. Avoid competing pollers.

| Symptom | Checks |
| --- | --- |
| Unstyled menus/settings/admin in production | Hashed CSS must return 200 `text/css`; check WhiteNoise, collected assets, stale mounts, and proxy `/static/` rules |
| Multi-platform Docker-driver error | Use the named `docker-container` builder above |
| HTTP 500 after deployment | Read the traceback; check migrations, `DATABASE_URL`, persistent keys, and actual running image |
| Missing slot-grant controls or permission-table error | Apply migration `0024_replication_slot_access`; use Administrator → User management |
| Operator cannot drop/terminate slots | Check independent grant, active Operator role, drop MFA, and PostgreSQL privileges |
| DB still exists after removing instance | Removal affects the dashboard registration, not the RDS resource |
| Database unreachable/slow | Check container-to-DB network/DNS, endpoint/database, credentials, SSL, connection capacity, and control permissions |
| Webhook accepted but no card/file | Inspect Flow run history and OneDrive; HTTP acceptance is not completion |
| Adaptive Card `type` error | Post `triggerBody()?['card']`, not the outer envelope |
| Workflow cannot reach localhost/private URL | Use blank URL for small inline CSV, or reachable HTTPS for signed downloads |
| Weekly report fails with blank public URL | Check webhook configuration and 20-KiB inline limit; inspect logs |
| Duplicate or missing alerts | Check meaningful query/PID changes, multiple pollers, lease/cycle duration, schedule, and destination failures |
| No recurring unchanged-lock card | Expected: use the live dashboard; new summaries need a change or reopening quiet hours |
| Invalid MFA | Check clocks, correct account enrollment, current/recovery code, and stable encryption key |
| Login/CSRF problems behind proxy | Check allowed hosts, trusted origins, HTTPS/forwarded protocol, and cookies |
| DB file large after cleanup | Retention removes eligible rows, not necessarily disk allocation; active incidents/audits remain |

## Security and current limitations

Before Internet exposure, review this as a privileged database-control application, not merely an HTTPS-enabled website.

- Replace development secrets, restrict allowed hosts and network access, configure trusted TLS termination, use least-privilege DB roles, and maintain dependencies.
- Enrollment is not a global MFA access gate: password-only onboarding establishes a session before enrollment. The current `regenerate` MFA action also rotates enrollment without a separate action-code check. Review/harden these flows before relying on mandatory MFA everywhere.
- There is no configured application-level login/OTP rate limiter. Add appropriate deployment controls and review authentication endpoints.
- Sessions use signed cookies: integrity-protected, not encrypted or centrally stored for per-session revocation. A sessions table does not mean server-side session revocation is active.
- Read-only means no mutation, not redacted data. SQL remains visible, and the instance API includes configured owner-webhook URLs. Restrict dashboard/API access accordingly.
- Treat webhooks, SQL literals, CSV payloads, share links, and exported Flow connection/destination identifiers as sensitive. The current `.dockerignore` is not a comprehensive sensitive-artifact filter; keep local DBs and unsanitized exports out of production builds/public repositories.
- Signed report links are bearer links until expiry. Cleanup is not conditional on confirmed archival; use upload verification and backups.
- Monitored-DB SSL uses `require`/`prefer`, not CA/hostname-verifying `verify-full`; there is no UI CA configuration.
- Polling is sampled, not a complete event audit; query age approximates wait duration. Some lock/slot catalog data is server-wide, so registrations on the same PostgreSQL server can overlap.
- Long-query classification uses active state and username exclusions, not reliable identification of a human/client tool.
- Teams uses new summary messages, not one updated thread. Delivery success means any webhook accepted, not durable acknowledgement from every destination.
- The UI currently loads HTMX and fonts externally. Restricted networks may need an appropriate self-hosted asset policy.

## Code map and tests

| File/directory | Responsibility |
| --- | --- |
| [config/settings.py](config/settings.py) | Environment, database, session, security, and static settings |
| [monitor/models.py](monitor/models.py) | Registry, audits/incidents, settings, MFA, grants, reports, lease |
| [monitor/db.py](monitor/db.py) | PostgreSQL queries, connections/timeouts, guarded PID kills, slot operations |
| [monitor/views.py](monitor/views.py), [monitor/urls.py](monitor/urls.py) | UI/actions, exports, configuration, API viewsets, routes |
| [monitor/permissions.py](monitor/permissions.py) | Registry API permissions and operator slot grants |
| [monitor/mfa.py](monitor/mfa.py), [monitor/auth_views.py](monitor/auth_views.py), [monitor/middleware.py](monitor/middleware.py) | TOTP/recovery, login/onboarding, admin MFA routing |
| [monitor/background.py](monitor/background.py), [monitor/apps.py](monitor/apps.py) | Embedded thread, lease, report schedule, cleanup |
| [monitor/management/commands/monitor_locks.py](monitor/management/commands/monitor_locks.py) | Incident tracking, deduplication, eligibility, monitor command |
| [monitor/notifications.py](monitor/notifications.py) | Cards, webhooks, IST formatting, CSV generation/signing |
| [monitor/templates/monitor/](monitor/templates/monitor/), [responsive.css](monitor/static/monitor/responsive.css) | UI, polling/confirmation scripts, responsive styles |
| [monitor/migrations/](monitor/migrations/) | Schema history; preserve and apply in order |
| [flow/](flow/), [deploy/](deploy/) | Workflow/AWS examples; customize and sanitize before reuse |

Run checks after installing dependencies and collecting static assets:

```bash
docker compose exec -T web python manage.py collectstatic --noinput
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py makemigrations --check --dry-run
docker compose exec -T web python manage.py test monitor --noinput
```

Isolated production-mode tests against a built image without local data mounts:

```bash
docker build -t rds-dashboard:test .
docker run --rm -e DJANGO_DEBUG=false -e DISABLE_EMBEDDED_LOCK_MONITOR=1 \
  rds-dashboard:test python manage.py test monitor --noinput
```

Tests cover connection/parsing behaviour, lock actions, MFA, Administrator/API restrictions, operator grants/revocation, guarded generic termination, notifications/schedules, reports/retention, and static files with debug disabled. Termination calls in automated tests are mocked; a passing suite does not authorize destructive production testing.

Before release, manually check mobile/desktop layout, the actual proxy/static configuration, authenticator enrollment/login, role-specific controls, an actual Teams test, and the resulting OneDrive file. Use a dedicated test database for controlled lock/slot scenarios.
