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

The Docker Compose `monitor-worker` service checks registered databases every 30 seconds. Run migrations before starting it, then configure the Teams Workflow webhook and SMTP settings in the environment:

```env
TEAMS_LOCK_WEBHOOK_URL=https://...
LOCK_ALERT_THRESHOLD_SECONDS=120
LOCK_MONITOR_INTERVAL_SECONDS=30
```

Add the shared Teams channel webhook in `TEAMS_LOCK_WEBHOOK_URL`, then add an optional owner Teams Workflow webhook for each database in the Add instance form. The worker posts the same alert to both destinations; no email is sent. It sends one alert after a lock has lasted two minutes and a resolved notification when that alerted lock disappears. For a one-time local check, run `python manage.py monitor_locks --once`; for continuous monitoring, run `python manage.py monitor_locks --interval 30`.
