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

For reliable emergency access, configure a separate lock-control role on each instance from the instance detail page. It should be able to inspect all sessions and terminate backends, for example (using the permissions supported by your PostgreSQL/RDS setup):

```sql
GRANT pg_monitor TO rds_dashboard_control;
GRANT pg_signal_backend TO rds_dashboard_control;
```

The dashboard uses this control role for activity, lock, and kill operations. If it is not configured, it falls back to the regular DB username. Connections use short statement and lock timeouts so a blocked session does not make the dashboard wait indefinitely.
