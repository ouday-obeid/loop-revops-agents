# Tailscale Setup (Mac Mini Mirror + MacBook Air Peer)

Phase 0 runs on the MBP by default. Two optional peers exist:
- **Mac Mini** — always-on, runs the OO Slack Socket-Mode daemon + scheduled jobs.
- **MacBook Air** — Loop AI company property; interactive Claude Code, long agent test runs, and remote dispatch target from the MBP. **Does NOT run launchd services** (no daemon, no briefings) — all Loop launchd work lives on the Mini to keep the split-brain invariant.

## Hosts
| Host | Role | Tailscale | SSH alias | User | Runs daemon? |
|---|---|---|---|---|---|
| MBP | Primary dev | (MBP's tailnet IP) | n/a | `ottimate` | No (Mini owns it) |
| Mac Mini | Always-on | `100.109.12.68` | `mini` | `jarvis` | **Yes — primary** |
| Gaming PC | GPU compute | `100.114.136.70` | `pc1` | `odayo` | No |
| MacBook Air | Loop dev peer | (assigned at Tailscale enrollment — tag `tag:loop-air`) | `air` | `ottimate` | **No — never** |

## Prereqs
- Tailscale installed on every host, signed in to the same tailnet
- SSH aliases for `mini`, `pc1`, `air` defined in `~/.ssh/config` on every host that dispatches to them

## Mac Mini mirror procedure
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

## MacBook Air enrollment procedure
1. On the Air:
   ```
   ssh air     # from the MBP, once the air host is in ~/.ssh/config
   git clone <repo-url> /Users/ottimate/loop-revops-agents
   cd /Users/ottimate/loop-revops-agents
   bash infra/bootstrap.sh
   ```
2. `.env` via GCP Secret Manager — do NOT rsync `.env` from the MBP:
   ```
   cd /Users/ottimate/loop-revops-agents
   loop-secrets-bootstrap      # reads .secrets-manifest, writes .env
   ```
3. **Do NOT run `install_launchd.sh` on the Air.** The Air runs no daemons and no scheduled jobs; the Mini owns them.
4. Recreate worktrees as needed (same pattern as the MBP):
   ```
   git worktree add ../loop-revops-agents-slt-metrics slt-metrics/d2-d15-build
   # ... etc per active workstream
   ```

## Split-brain prevention
Only ONE host should run `oo-daemon` (Slack Socket Mode). Canonical host is the **Mac Mini**. The MBP daemon is disabled while the Mini is the primary:
```
launchctl bootout gui/$UID/com.loop-revops.oo-daemon
```
The **MacBook Air never runs `oo-daemon` or any `com.loop-revops.*` / `com.outbounder.*` launchd service** regardless of the Mini's state. If the Mini goes offline, fail over to the MBP, not the Air.

Scheduled jobs (briefings, board monitors, integration health) run on the Mini only.

## Cross-machine dev flow
GitHub (`ouday-obeid/loop-revops-agents`) is the single source of truth. Both the MBP and Air pull/push through it:

```
session start  →  cd ~/loop-revops-agents && loop-sync
work           →  commit as normal
session end    →  git push
```

`loop-sync` (at `~/bin/loop-sync`) refuses to run with a dirty tree and only fast-forwards — never merges or force-resets. Safe by design.

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
