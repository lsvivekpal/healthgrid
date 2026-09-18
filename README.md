# HealthGrid

A Django application for PostgreSQL/RDS lock and session monitoring, controlled backend termination, replication-slot management, Teams notifications, and weekly lock-history reports.

**Author:** [lsvivekpal](https://github.com/lsvivekpal) · **Repository:** [github.com/lsvivekpal/healthgrid](https://github.com/lsvivekpal/healthgrid)

Instances are registered manually. The app connects directly to PostgreSQL; it does not discover instances through AWS or delete AWS RDS infrastructure.

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Local setup](#local-setup)
- [Production deployment](#production-deployment)
- [ALB and Nginx HTTPS](#alb-and-nginx-https)
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
- Home dashboard: total/healthy instances, blocked locks, unreachable instances, configurable auto-refresh, pause/resume, and refresh now.
- Live sessions: users, client addresses, application names, state, wait events, query previews, durations, filtering, sorting, and CSV export.
- Locks: blocked/blocking PID pairs, individual termination, role-based selection, bulk termination, and chain termination.
- Database vitals: connection usage, session mix, longest active client query, database size, and ten largest tables.
- Replication slots: status, active PID, retained WAL, and independently authorized backend termination and slot dropping.
- Accounts: Administrator, Operator, and Read-only access; Google Authenticator-compatible MFA and per-user recovery codes.
- Teams: shared-channel and owner webhooks, test messages, deduplicated lock summaries, IST timestamps, and configurable quiet hours.
- Long-running query alerts for non-excluded database users.
- Weekly lock-history CSV reports through the existing Teams Workflow, with OneDrive file creation handled by that workflow.
- Responsive forms/tables, mobile session sorting, light/dark themes, accessible confirmation dialogs, and persistent error messages.
- Centralized **Other dashboards** registry: Administrators maintain approved HTTP(S) links, and authenticated users open them safely in new browser tabs.
- Audit history for control actions, protected-action MFA failures, and operator replication-permission changes.
- Installable Progressive Web App (PWA) with a home-screen/app-launcher shortcut for mobile access.

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

Example production environment; replace the domain, webhook hostname, database URL, and secret placeholders:

```dotenv
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=REPLACE_WITH_A_STRONG_RANDOM_SECRET
ENCRYPTION_KEY=REPLACE_WITH_A_VALID_FERNET_KEY
DJANGO_ALLOWED_HOSTS=dashboard.example.com
CSRF_TRUSTED_ORIGINS=https://dashboard.example.com
DJANGO_USE_HTTPS=true
DJANGO_ALLOW_HTTP=false
DJANGO_MFA_REQUIRED=true
DJANGO_HSTS_SECONDS=31536000
DB_SSL_MODE=require
DB_SSL_ROOT_CERT=
WEBHOOK_ALLOWED_HOSTS=approved-teams-host.example.com
DATABASE_URL=postgres://user:password@db-host:5432/rds_dashboard
ACTIVITY_CACHE_TTL=10
```

`DJANGO_ALLOWED_HOSTS` contains hostnames only. `CSRF_TRUSTED_ORIGINS` includes the scheme and no trailing slash. Set `DJANGO_USE_HTTPS=true` only behind a trusted TLS-terminating proxy that sets `X-Forwarded-Proto` correctly. For a temporary HTTP-only test, explicitly set `DJANGO_USE_HTTPS=false` and `DJANGO_ALLOW_HTTP=true`; never use that combination for a public deployment. Keep the environment file private and out of version control. `.env` is ignored by Git and Docker; use `.env.example` as the variable reference.

Example first deployment behind a reverse proxy on the same host:

```bash
docker build -t healthgrid:release-001 .
docker volume create healthgrid-data
docker run --rm --env-file .env.prod -v healthgrid-data:/data healthgrid:release-001 python manage.py migrate --noinput
docker run --rm -it --env-file .env.prod -e DISABLE_EMBEDDED_LOCK_MONITOR=1 -v healthgrid-data:/data healthgrid:release-001 python manage.py createsuperuser
docker run -d --name healthgrid --restart unless-stopped \
  --env-file .env.prod -v healthgrid-data:/data \
  -p 127.0.0.1:8000:8000 healthgrid:release-001 \
  gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 60
```

The production Dockerfile starts Gunicorn as the non-root `appuser` with one worker. Do not use Gunicorn `--preload` with the current embedded-thread startup. Local Compose intentionally overrides this with Django's development server and root access for bind-mounted development files.

### ALB and Nginx HTTPS

When the AWS Application Load Balancer terminates TLS, Nginx should listen on HTTP inside the private container network. Configure the ALB to redirect listener port 80 to HTTPS, forward HTTPS traffic to Nginx port 80, and send `X-Forwarded-Proto`. Do not expose the application port 8000 publicly.

Use the Docker service name in Nginx; do not reference an undefined upstream alias:

```nginx
server {
    listen 80;
    server_name dashboard.example.com;

    location / {
        proxy_pass http://healthgrid:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
        proxy_set_header X-Forwarded-Port $http_x_forwarded_port;
        proxy_set_header Connection "";
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
    }

    location = /healthz {
        proxy_pass http://healthgrid:8000/healthz;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

The Nginx container and `healthgrid` service must share a Docker network. Test the Nginx configuration before reload. The ALB health check should use `HTTP /healthz`; it is intentionally unauthenticated and returns only `{"status":"ok"}`.

For upgrades, use a new image tag/digest, run `migrate --noinput` with the same database configuration, then update the service through its normal deployment process. Migration `0024_replication_slot_access` is required for operator slot grants. Preserve migration history; do not delete migrations already applied in production.

Do not mount old source over `/app` or stale assets over `/app/staticfiles` in production: those mounts hide files from the new image.

### Multi-platform builds and ECR

If Docker reports that its current driver cannot build multiple platforms, use a `docker-container` builder. If it already exists, run `docker buildx use healthgrid-builder` instead of recreating it. See [Docker's multi-platform guidance](https://docs.docker.com/build/building/multi-platform/).

```bash
docker buildx create --name healthgrid-builder --driver docker-container --use
docker buildx inspect --bootstrap
```

Example using the AWS CLI **default profile**; replace the account ID, repository, and release tag. The ECR repository must exist:

```bash
RDS_ECR_REGISTRY=123456789012.dkr.ecr.ap-south-1.amazonaws.com
RDS_IMAGE_TAG=release-001
aws --profile default ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin "$RDS_ECR_REGISTRY"
docker buildx build --builder healthgrid-builder \
  --platform linux/amd64,linux/arm64 \
  -t "$RDS_ECR_REGISTRY/healthgrid:$RDS_IMAGE_TAG" \
  -t "$RDS_ECR_REGISTRY/healthgrid:latest" --push .
```

Authentication follows [AWS's ECR login instructions](https://docs.aws.amazon.com/cli/latest/reference/ecr/get-login-password.html). Prefer deploying the release tag/digest. A push does not update running services automatically. Multi-platform `--push` does not load a local runnable image; use `docker compose build web` for local testing.

### Production static files

The image build runs `collectstatic`. WhiteNoise serves collected files with debug disabled, and content-hashed stylesheet URLs avoid stale browser caches. See the [WhiteNoise Django integration](https://whitenoise.readthedocs.io/en/stable/django.html).

If a proxy intercepts `/static/`, forward requests to the application or serve assets from the **same release**. Verify `/static/monitor/responsive.<hash>.css` returns HTTP 200 with `Content-Type: text/css`, not HTML or a login redirect. Do not enable debug mode to fix production CSS.

## Environment configuration

Notification options and monitored-database credentials are managed in the UI. Infrastructure settings remain environment variables:

| Variable | Default / purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Required when debug is false; stable strong secret for sessions and signed report links |
| `ENCRYPTION_KEY` | Required when debug is false; stable Fernet key for stored DB/MFA secrets |
| `DJANGO_DEBUG` | `false`; development Compose explicitly sets `true` |
| `DJANGO_ALLOWED_HOSTS` | Required in production; comma-separated hostnames without scheme |
| `DATABASE_URL` | Required in production; use durable PostgreSQL rather than ephemeral SQLite |
| `DJANGO_USE_HTTPS` | Required in public production; enables secure cookies, redirect, HSTS, and forwarded-protocol handling |
| `DJANGO_ALLOW_HTTP` | `false`; must be explicitly `true` to permit temporary non-TLS production mode |
| `DJANGO_MFA_REQUIRED` | `true`; blocks dashboard use until authenticator enrollment is complete |
| `DJANGO_HSTS_SECONDS` | `31536000` when HTTPS is enabled |
| `CSRF_TRUSTED_ORIGINS` | Required for proxy/origin POSTs; comma-separated origins including scheme |
| `DB_SSL_MODE` | `require` in production; use `verify-full` with `DB_SSL_ROOT_CERT` for CA/hostname verification |
| `DB_SSL_ROOT_CERT` | Optional CA bundle path, only needed for `verify-full` |
| `WEBHOOK_ALLOWED_HOSTS` | Required in production; comma-separated approved Teams/Power Automate hostnames only |
| `ACTIVITY_CACHE_TTL` | `10` seconds; per-process cache of live dashboard reads |
| `REPORT_BASE_URL` | Not an environment variable; configure the public HTTPS report URL in Notification Settings for large reports |
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

Control connections use application name `healthgrid-control`, with a 5-second connect timeout, 8-second statement timeout, 2-second lock timeout, and 10-second idle-in-transaction timeout.

A blocked connection belonging to `db_lock_admin` does not necessarily block every new connection using that username. The dashboard opens independent connections; access still depends on network reachability, connection capacity, and DB privileges. It does not automatically kill its own PIDs to recover access.

Require SSL selects `sslmode=require`; unchecked selects `prefer`. Neither is hostname-verifying `verify-full`.

## Dashboard and lock control

### Live views

- Home auto-refresh defaults to **30 seconds**. The interval can be changed from 5 to 3600 seconds, disabled, or overridden with **Refresh now**. The preference is saved in the current browser.
- Home polling pauses automatically when its browser tab is hidden and resumes when the tab becomes active. This browser setting does not affect background monitoring or Teams notifications.
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
| View dashboard/locks/sessions/slots/audits and export CSV | All instances | Assigned instances | Assigned instances |
| Add/duplicate/rename instances; edit credentials/webhooks | Yes | Yes, assigned instances | No |
| Configure notifications; send tests/manual reports | Yes | Yes | No |
| Individual lock kill | Yes | Yes, assigned instances | No |
| Live-session and bulk/chain kills | Yes, MFA | Yes, MFA, assigned instances | No |
| Terminate slot backend | Yes | Explicit terminate grant | No |
| Drop slot | Fresh MFA | Explicit drop grant + fresh MFA | No |
| Remove instance | Fresh MFA | No | No |
| Create users; grant/revoke operator slot permissions | Yes | No | No |

Application authorization never overrides the connected PostgreSQL role's privileges.

### User management

As Administrator, open account menu → **User management**:

1. Create an Operator or Read-only account with a temporary password. Optional email is contact information, not a Teams destination.
2. In **Instance access**, choose **Read-only** or **Operator** for each instance. **No access** leaves that instance hidden from the user.
3. Optionally grant an operator **Terminate slot backend** and/or **Drop slot**.
4. For existing users, use the **Instance access** controls in the user table and select **Save access**. The selected role applies only to those instances.

Both grants default to off and are independent. Drop permission does not include termination; an active slot may require an independently authorized termination first. Revocation is enforced on subsequent requests. Grant changes are audited. Read-only/inactive users cannot exercise grants even if records exist.

The custom user page creates accounts and manages slot grants plus per-instance access. Administrators can view all active instances. Operator and Read-only users see only explicitly assigned instances; only Operator assignments expose instance controls. Accounts created before per-instance access was introduced retain access to all active instances until an Administrator saves an explicit instance-access assignment for them. There is no shared multi-device Administrator MFA configuration: use separate accounts/devices.

### Authenticator enrollment and action checks

First password login directs users to **Authenticator security**. Scan the QR code or enter the setup key in Google Authenticator or another compatible TOTP app, save the recovery codes, and confirm enrollment. After the first successful enrollment, the user is returned to the instance dashboard. Once enabled, subsequent normal logins require password plus authenticator/recovery verification.

The normal reset UI requests a current code before enrolling a replacement device. Recovery codes are one-time and hashed; MFA secrets and stored database passwords are encrypted with `ENCRYPTION_KEY`.

Current action rules:

- Instance removal and slot drop require a code submitted for **that action**, even just after MFA login. API removal follows the same rule.
- Live-session and bulk/chain dialogs ask for MFA. Their server helper also accepts a verification from the last **300 seconds** if no code is submitted.
- Individual lock kills do not require step-up MFA.
- Dedicated slot-backend termination requires its role/grant but currently has no step-up MFA requirement. Generic live-session termination retains its own MFA rule.
- Django admin instance deletion, including bulk deletion, is disabled to prevent bypassing removal MFA.

Production MFA onboarding is enforced by middleware when `DJANGO_MFA_REQUIRED=true`. Login and MFA failures are throttled for 15 minutes after repeated failures; use a shared cache such as Redis if running multiple web workers/replicas.

## Mobile app / PWA

When served over HTTPS, authenticated users see **Install app** in the header. Select it to add HealthGrid to the phone or desktop app launcher. On iOS Safari, use the browser share menu → **Add to Home Screen**. The PWA service worker caches only static assets; dashboard HTML, live lock/session responses, APIs, and control actions are never cached.

## Teams notifications

### Configuration and defaults

Open account menu → **Notification settings**. These settings are global, not per-instance overrides; the optional owner webhook is per instance.

| Setting | Default / meaning |
| --- | --- |
| Shared channel webhook | Blank; Workflow destination for alerts and reports |
| Alert after | `120` seconds; lock eligibility threshold |
| Check every | `30` seconds; background sleep between cycles |
| Lock-storm threshold | `100` active PID pairs; one compact alert per storm, `0` disables |
| Notification schedule | Disabled; uses IST when enabled |
| Schedule start/end | `09:00` / `21:00` |
| Keep resolved records | `30` days; UI range 7–3650 |
| Long-running query alerts | Disabled |
| Long-query threshold | `60` seconds |
| Excluded query users | `applms,applos` |
| Weekly report | Disabled; Monday at 09:00 IST when enabled |
| Weekly report database retention | `7` days; selectable as 2, 3, or 7 days |
| Public dashboard URL | Blank; enables signed report downloads |

Save settings before **Send test to shared channel**. Add/edit/test an owner webhook on its instance page or while adding an instance. The workflow determines whether the owner destination is a chat or channel; the app does not resolve owner emails.

The app can send alerts to both destinations, deduplicating identical webhook URLs for alert messages. No separate CSV-upload webhook is required.

Each instance has an **Exclude from real-time global Teams alerts** option on
the Add instance form and detail page. When enabled, real-time lock and
long-query cards for that instance skip the shared/global channel but still go
to its owner webhook. The instance remains included in the all-databases weekly
report, and its instance-specific weekly report still goes to the owner
webhook. Monitoring, dashboard history, and local CSV exports are not disabled.
New instances default to included.

### Timing and lock summaries

The monitor checks registrations sequentially, then sleeps at least 5 seconds, normally the configured interval. Database/network work adds to the cycle duration.

Eligibility uses `waiting_seconds`, calculated from the blocked query's `query_start`, **not an independently measured lock-wait start**. With 30-second checks and a 70-second threshold, a query starting at zero and sampled at 30/60/90 seconds normally first qualifies around 90 seconds. An already-old query can qualify immediately when observed blocked. This is not a delivery SLA; incidents between polls can be missed.

Notifications are aggregated per registration:

1. The first eligible locks produce an initial summary.
2. A new or cleared eligible PID pair produces an update; query-text/user changes for the same active PID pair do not.
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

This checks active queries from any non-excluded user, **not only DBeaver/TablePlus**. Application names do not prove human activity. Username exclusions are case-insensitive; `healthgrid-control` application connections are excluded automatically.

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

The supplied [workflow exports](flow/) use OneDrive for Business and default folder `/HealthGrid/Weekly Reports/`. Create/select the folder and rebind connections, Teams destinations, and sharing settings during import. Exports are environment-specific examples, not portable credentials. Verify connector availability/licensing in your Microsoft tenant; this repository does not guarantee a premium-free flow.

### Inline CSV versus signed URL

- **Blank public URL:** CSV travels in the webhook, so Power Automate need not reach the app. The current inline limit is **20 KiB of UTF-8 CSV content**.
- **Public URL configured:** The app sends a signed `report_url` and empty `csv_content`. Power Automate must reach the URL; use publicly reachable HTTPS. `localhost`, private-only hosts, and proxy login challenges will not work from Microsoft's cloud service.
- **Large CSV without a public URL:** Sending fails and logs the size issue. Automatic splitting or a secondary upload service is not implemented.

Signed links expire no later than seven days and are also limited by the configured 2, 3, or 7-day report retention. They do not require dashboard login. Anyone holding a valid link can retrieve the CSV until expiry; treat it as a sensitive bearer link.

### Delivery verification

“Sent” means at least one webhook accepted the HTTP request. It does **not** prove all destinations succeeded, the Teams card appeared, or OneDrive stored the file. Check Flow run history, the destination folder, and the final Teams message. There is no upload-confirmation callback or per-destination retry ledger.

## Storage, retention, and backups

The application stores incident snapshots/SQL for reporting; it does not copy PostgreSQL business data or continuously archive all queries.

| Data | Retention |
| --- | --- |
| Active lock / long-query incidents | Never deleted by automatic retention cleanup |
| Resolved lock / long-query incidents | Deleted after configured days since resolution |
| Temporary `LockReport` CSV payloads | Expire after the configured 2, 3, or 7 days; expired records are cleaned |
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
| `/settings/dashboards/` | Authenticated dashboard-link directory; Administrator-only editing |
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
- With `DJANGO_MFA_REQUIRED=true`, users who have not completed authenticator enrollment are redirected to setup and cannot use the dashboard or API. Enrollment still depends on the initial account password; use controlled account provisioning and rotate temporary passwords.
- Login and MFA failures are throttled using the configured Django cache. The default local-memory cache is suitable only for the single-worker deployment; use a shared authenticated cache for multiple replicas.
- Sessions use signed cookies: integrity-protected, not encrypted or centrally stored for per-session revocation. A sessions table does not mean server-side session revocation is active.
- Read-only means no mutation, not redacted data. SQL remains visible. Owner webhook URLs are write-only in the API, but staff who can configure notifications can still use those bearer destinations; restrict staff access accordingly.
- Treat webhooks, SQL literals, CSV payloads, share links, and exported Flow connection/destination identifiers as sensitive. The current `.dockerignore` is not a comprehensive sensitive-artifact filter; keep local DBs and unsanitized exports out of production builds/public repositories.
- Signed report links are bearer links until expiry. Cleanup is not conditional on confirmed archival; use upload verification and backups.
- Monitored-DB SSL is controlled by `DB_SSL_MODE`. Private RDS deployments normally use `require` (encrypted without a local CA file); use `verify-full` with a mounted `DB_SSL_ROOT_CERT` for certificate and hostname verification.
- Webhook delivery requires HTTPS, rejects embedded credentials and redirects, blocks private/reserved destinations, and can be restricted to approved hostnames with `WEBHOOK_ALLOWED_HOSTS`.
- Polling is sampled, not a complete event audit; query age approximates wait duration. Some lock/slot catalog data is server-wide, so registrations on the same PostgreSQL server can overlap.
- Long-query classification uses active state and username exclusions, not reliable identification of a human/client tool.
- Teams uses new summary messages, not one updated thread. Delivery success means any webhook accepted, not durable acknowledgement from every destination.
- The UI currently loads HTMX and fonts externally. Restricted networks may need an appropriate self-hosted asset policy.

### Public HTTPS verification

Before release, perform a read-only check of the deployed hostname:

```bash
curl -I http://dashboard.example.com/
curl -I https://dashboard.example.com/
curl https://dashboard.example.com/healthz
```

Expected results are an HTTP-to-HTTPS redirect, a login redirect over HTTPS, and `{"status":"ok"}` from `/healthz`. Confirm TLS certificate validity, HSTS, secure cookies, unauthenticated API `403` responses, and that `.env`, source files, and invalid report tokens are not accessible. This is a deployment smoke test, not a substitute for an authorized authenticated penetration test.

## Code map and tests

| File/directory | Responsibility |
| --- | --- |
| [config/settings.py](config/settings.py) | Environment, database, session, security, and static settings |
| [monitor/models.py](monitor/models.py) | Registry, audits/incidents, settings, MFA, grants, reports, lease, dashboard links |
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
docker build -t healthgrid:test .
docker run --rm -e DJANGO_DEBUG=false -e DISABLE_EMBEDDED_LOCK_MONITOR=1 \
  healthgrid:test python manage.py test monitor --noinput
```

Tests cover connection/parsing behaviour, lock actions, MFA, Administrator/API restrictions, operator grants/revocation, guarded generic termination, notifications/schedules, reports/retention, and static files with debug disabled. Termination calls in automated tests are mocked; a passing suite does not authorize destructive production testing.

Before release, manually check mobile/desktop layout, the actual proxy/static configuration, authenticator enrollment/login, role-specific controls, an actual Teams test, and the resulting OneDrive file. Use a dedicated test database for controlled lock/slot scenarios.
