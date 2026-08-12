Build image:

  Rebuild docker image to pick these up:    
  cd /Users/vivekpal/Documents/learning/rds-dashboard                                                                                                      
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
