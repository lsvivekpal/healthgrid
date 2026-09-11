  source .venv/bin/activate                                                                                                                                            
  export DJANGO_DEBUG=true                                                                                                                                             
  python manage.py createsuperuser                                                                                                                                     
  python manage.py runserver                                                                                                                                           
                                                                                                                                                                       
  Add instance form now asks host/port/db_name/username/password + "Require SSL" checkbox — uncheck it for a plain local Postgres (e.g. target-db in docker-compose,   
  port 5433, user appuser/apppass). Want me to start runserver now in background?                                                                                      
                              

Then open http://127.0.0.1:8000/. Login,

prod use case:
Build Docker image:

  Rebuild docker image to pick these up:    
  cd /<project-dir>/rds-dashboard                                                                                                      
  docker build -t rds-dashboard:latest .                                                                                                                                    
  docker run --rm --env-file .env rds-dashboard:latest python manage.py migrate                                                                                             
  docker stop rds-dashboard && docker rm rds-dashboard                                                                                                                      
  docker run -d --name rds-dashboard -p 8000:8000 --env-file .env rds-dashboard:latest                                                                                      
      

 1. Idle-in-transaction surfacing — Sessions table now includes idle in transaction (+ aborted) states, tagged with an orange "idle-in-txn" badge, not just active.        
  2. Duration/wait highlighting — waiting ≥30s (Locks) or running ≥30s (Sessions) renders bold red.                                                                         
  3. Kill entire chain — one button per lock row terminates both blocking + blocked pid in a single DB connection/action, one audit entry.                                  
  4. Test connection — button on Add/Duplicate form, HTMX-posts current form values to a new endpoint, shows connected-ok/failed inline before you save anything.           
  5. Pause/Resume polling — button next to the refresh spinner halts the 15s HTMX poll (window.dashboardPollingPaused gates the hx-trigger) so rows don't shift             
  mid-investigation.  

Open http://localhost:8000

Lock alert monitor

The web container starts the embedded lock monitor automatically. It uses a database lease so only one process checks locks when the app has multiple web workers or replicas. Run migrations before starting the app:

Open Notification settings in the dashboard to add the shared Teams channel webhook and configure the alert threshold and check interval. Add an optional owner Teams Workflow webhook for each database in the Add instance form. The embedded monitor sends one aggregate summary per database, listing active blocked-to-blocking PID pairs. It sends an update only when the lock set or lock details change, and one final summary when all tracked locks clear. For a one-time local check or troubleshooting, run `python manage.py monitor_locks --once`.

Teams Workflow payloads are envelopes. Set the lock-alert branch to post `triggerBody()?['card']` and test with the `event_type` value `lock_alert`. Weekly reports use `event_type` `weekly_report` and include `file_name`, `csv_content`, `report_rows`, and optionally `report_url`. In the weekly branch, use OneDrive for Business “Upload file from URL” when `report_url` is present; otherwise create the OneDrive file from `csv_content`, then post the card with the OneDrive link. Configure the public dashboard URL in Notification settings for large reports. The report link is signed and expires after seven days.

Enable Weekly CSV report in Notification settings to schedule a weekly report in IST, or use “Send weekly report now” to verify the flow. Reports include active and cleared lock incidents from the previous seven days with full blocked and blocking SQL text.

The monitor performs daily retention cleanup in the same embedded thread. Active incidents are never deleted; resolved incidents are kept for the configured retention period (30 days by default), and temporary report payloads expire after seven days. OneDrive is the long-term archive for weekly CSV reports.

For reliable emergency access, configure a separate lock-control role on each instance from the instance detail page. It should be able to inspect all sessions and terminate backends, for example (using the permissions supported by your PostgreSQL/RDS setup):

```sql
GRANT pg_monitor TO rds_dashboard_control;
GRANT pg_signal_backend TO rds_dashboard_control;
```

The dashboard uses this control role for activity, lock, and kill operations. If it is not configured, it falls back to the regular DB username. Connections use short statement and lock timeouts so a blocked session does not make the dashboard wait indefinitely.

Staff MFA

Staff accounts use Google Authenticator-compatible TOTP MFA. After deploying the MFA migration, each staff member signs in with their password and is sent to `MFA security` to scan the QR code and save the one-time recovery codes. Once enabled, MFA is required at staff login.

MFA is required for every dashboard login, with a separate authenticator enrollment and recovery codes per user. It is also required for live-session kills, all bulk/chain kills (including lock bulk/chain actions), and dropping replication slots. Only an individual lock kill is MFA-free; its dedicated route validates that the selected PID is currently part of an active lock. Keep `DJANGO_SECRET_KEY` and `ENCRYPTION_KEY` stable and secret in production; the TOTP secret is encrypted with `ENCRYPTION_KEY` and recovery codes are stored as hashes.

Superusers can open User management to create Operator or Read-only accounts. Read-only users can view the dashboard and export data but cannot kill sessions, change instances, edit notification settings, or access mutation endpoints. The built-in `/admin/` entry is also routed through dashboard MFA.

Notification schedule

Notification settings can restrict lock-alert delivery to a custom IST window, such as 09:00–21:00. Lock checks and database state tracking continue outside the window, but Teams lock alerts are suppressed; persistent locks receive a fresh summary when the window opens. Weekly reports remain independent and are not suppressed by this setting. Overnight windows such as 21:00–09:00 are supported.
