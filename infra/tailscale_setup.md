# Tailscale Setup (Optional Mac Mini Mirror)

Phase 0 runs on the MBP by default. If O wants 24/7 coverage, mirror to the Mac Mini.

## Prereqs
- Tailscale installed on both hosts, signed in to the same tailnet
- Mac Mini: `100.109.12.68` (user `jarvis`), SSH alias `mini` in `~/.ssh/config`

## Mirror procedure
1. On Mac Mini:
   ```
   ssh mini
   git clone <repo-url> /Users/jarvis/loop-revops-agents
   cd /Users/jarvis/loop-revops-agents
   bash infra/bootstrap.sh
   ```
2. Copy `.env` (Mac Mini gets its own — do NOT rsync `.env` from MBP):
   ```
   scp ~/loop-revops-agents/.env mini:/Users/jarvis/loop-revops-agents/.env
   ```
   Then on Mac Mini edit `.env` to set `REVOPS_REPO_ROOT=/Users/jarvis/loop-revops-agents`.
3. Install launchd: `ssh mini 'cd ~/loop-revops-agents && bash infra/install_launchd.sh'`

## Split-brain prevention
Only ONE host should run `oo-daemon` (Slack Socket Mode) at a time. If mirroring, disable the daemon on MBP:
```
launchctl bootout gui/$UID/com.loop-revops.oo-daemon
```
Leave scheduled jobs (briefings, monitors) on whichever host should own them.
