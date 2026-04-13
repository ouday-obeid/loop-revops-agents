# OO Agent Runbook

## Deploy
```bash
cd $REVOPS_REPO_ROOT
bash infra/bootstrap.sh
bash scripts/run_migrations.sh
bash scripts/load_seed_data.sh   # optional — seeds sf_admin knowledge corpus
bash infra/install_launchd.sh
```

## Pause
```bash
launchctl bootout gui/$UID/com.loop-revops.oo-daemon
launchctl bootout gui/$UID/com.loop-revops.oo-board-monitor
launchctl bootout gui/$UID/com.loop-revops.oo-integration-health
launchctl bootout gui/$UID/com.loop-revops.oo-briefing-daily
launchctl bootout gui/$UID/com.loop-revops.oo-briefing-weekly
```

## Rollback
All OO actions are read-only or alert-only. To fully disable:
1. Pause (above)
2. Remove plists: `rm ~/Library/LaunchAgents/com.loop-revops.*.plist`
3. Optional: `rm $REVOPS_REPO_ROOT/shared/db/loop_revops.db` to wipe state (destroys task board + audit)

## Logs
- Per-job stdout/stderr: `$REVOPS_REPO_ROOT/var/log/<job>.out.log` / `.err.log`
- DB-backed: `agent_runs` and `audit_log` tables

## Force a briefing now
```bash
cd $REVOPS_REPO_ROOT && source .venv/bin/activate
python -c "import asyncio; from agents.oo.briefings import send_daily_briefing; print(asyncio.run(send_daily_briefing()))"
```

## Health check
```bash
bash scripts/health_check.sh
```

## Known risks
- Slack Socket Mode token rotation: if `SLACK_APP_TOKEN` rotates, daemon must restart.
- Single-host Slack daemon: if mirrored to Mac Mini, only one host may run `oo-daemon` — see `infra/tailscale_setup.md`.
