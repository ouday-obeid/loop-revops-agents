# Cloud Scheduler (Phase 4)

Generated from the same `shared/runtime/schedule.py` registry. Placeholder — build the generator in Phase 4 migration alongside the Cloud Run deploy.

Target: emit `gcloud scheduler jobs create` commands, one per `Job`, hitting Cloud Run HTTP triggers for the daemons (oo-daemon becomes a long-lived Cloud Run service, others become scheduled Cloud Run Jobs).
