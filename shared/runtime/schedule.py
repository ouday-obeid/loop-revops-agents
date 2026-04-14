"""Cron job registry — SOURCE OF TRUTH.

Adding a scheduled job = add one row. launchd plists and Cloud Scheduler jobs
are generated from this list — never hand-written.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    name: str
    cron: str
    callable_path: str  # module:function
    description: str = ""


# cron expressions: "m h dom mon dow" (standard 5-field).
SCHEDULE: list[Job] = [
    Job(
        name="oo-daemon",
        cron="@reboot",
        callable_path="agents.oo.main:run_daemon",
        description="OO Agent Slack Socket Mode daemon (always-on)",
    ),
    Job(
        name="oo-briefing-daily",
        cron="30 8 * * 1-5",
        callable_path="agents.oo.briefings:send_daily_briefing",
        description="Daily 8:30 AM briefing to O",
    ),
    Job(
        name="oo-briefing-weekly",
        cron="0 16 * * 5",
        callable_path="agents.oo.briefings:send_weekly_review",
        description="Friday 4 PM weekly review",
    ),
    Job(
        name="oo-board-monitor",
        cron="*/15 * * * *",
        callable_path="agents.oo.board_monitor:scan",
        description="Passive Slack + Fireflies scan (every 15 min)",
    ),
    Job(
        name="oo-integration-health",
        cron="*/30 * * * *",
        callable_path="agents.oo.integration_health:poll",
        description="Integration health poll (every 30 min)",
    ),
    # Phase 1 — Agent 3 (Onboarding). See agents/onboarding/RUNBOOK.md.
    Job(
        name="onboarding-closed-won-poller",
        cron="*/5 * * * *",
        callable_path="agents.onboarding.closed_won_poller:poll",
        description="Create Onboarding__c for new Closed Won opps (idempotent)",
    ),
    Job(
        name="onboarding-milestone-monitor",
        cron="0 */6 * * *",
        callable_path="agents.onboarding.milestone_monitor:scan",
        description="Stall detection across JK_Onboarding_Stage__c + Overall status",
    ),
    Job(
        name="onboarding-location-sweep",
        cron="0 9 * * *",
        callable_path="agents.onboarding.location_activation:sweep",
        description="Daily stuck-location classifier",
    ),
    Job(
        name="onboarding-jackie-digest",
        cron="0 9 * * 5",
        callable_path="agents.onboarding.dispatcher:send_jackie_weekly_digest",
        description="Friday 9 AM ET weekly CS digest to Jackie",
    ),
    # Phase 1 — Agent 1 (Top of Funnel). See agents/top_of_funnel/RUNBOOK.md.
    Job(
        name="top-of-funnel-enrichment-pipeline",
        cron="0 2 * * 1-5",
        callable_path="agents.top_of_funnel.enrichment.pipeline:run_pipeline",
        description="Nightly Apollo+Clay enrichment + ICP scoring + SF Lead create",
    ),
    Job(
        name="top-of-funnel-daily-briefing",
        cron="55 7 * * 1-5",
        callable_path="agents.top_of_funnel.daily_briefing:send_daily_briefing",
        description="07:55 Mon-Fri SDR lead-list DMs + Hutch summary",
    ),
    # Phase 1 — Agent 4 (CS). See agents/cs/RUNBOOK.md.
    Job(
        name="cs-integration-health",
        cron="*/30 * * * *",
        callable_path="agents.cs.integration_health:poll",
        description="CS-specific integration health probes (every 30 min)",
    ),
    Job(
        name="cs-health-poll",
        cron="0 */2 * * *",
        callable_path="agents.cs.health.health_monitor:poll",
        description="Vitally sync + drop detection (every 2h)",
    ),
    Job(
        name="cs-churn-sweep",
        cron="0 10 * * *",
        callable_path="agents.cs.risk.churn_risk:run_sweep",
        description="Daily 06:00 ET churn-risk scoring + tier routing",
    ),
    Job(
        name="cs-renewal-pipeline",
        cron="0 11 * * *",
        callable_path="agents.cs.renewal.pipeline:run_sweep",
        description="Daily 07:00 ET T-120 renewal-opp sweep",
    ),
    Job(
        name="cs-renewal-stall",
        cron="0 12 * * *",
        callable_path="agents.cs.renewal.stall_monitor:run_sweep",
        description="Daily 08:00 ET stall check on open Renewal opps",
    ),
    Job(
        name="cs-expansion-scan",
        cron="0 13 * * *",
        callable_path="agents.cs.expansion.expansion_detector:run_sweep",
        description="Daily 09:00 ET expansion-signal sweep (Fireflies + SF)",
    ),
    Job(
        name="cs-weekly-report",
        cron="0 11 * * 1",
        callable_path="agents.cs.reports.weekly:send",
        description="Monday 07:00 ET weekly CS digest to Jackie + #agent-cs-log",
    ),
    # Phase 1 — Agent 6 (SLT Revenue Metrics). See agents/slt_metrics/RUNBOOK.md.
    Job(
        name="slt-morning-snapshot",
        cron="30 6 * * 1-5",
        callable_path="agents.slt_metrics.jobs:run_morning_snapshot",
        description="06:30 M-F fetch open pipeline + write daily pipeline_snapshots",
    ),
    Job(
        name="slt-daily-briefing",
        cron="0 8 * * 1-5",
        callable_path="agents.slt_metrics.jobs:run_daily_briefing",
        description="08:00 M-F compose daily briefing → slt_draft_review gate in O's DM",
    ),
    Job(
        name="slt-friday-review",
        cron="30 15 * * 5",
        callable_path="agents.slt_metrics.jobs:run_friday_review",
        description="Friday 15:30 weekly review → slt_draft_review gate in O's DM",
    ),
    # Phase 1 — Agent 2 (Sales Reps). See agents/sales_reps/RUNBOOK.md.
    Job(
        name="sales-reps-grader-poll",
        cron="*/15 * * * *",
        callable_path="agents.sales_reps.scheduler.jobs:grader_poll",
        description="Every 15 min: grade new Fireflies transcripts (idempotent via storage)",
    ),
    Job(
        name="sales-reps-brief-scan",
        cron="*/15 * * * *",
        callable_path="agents.sales_reps.scheduler.jobs:brief_scan",
        description="Every 15 min: GCal demos in 90-120 min → pre-demo brief per Opp",
    ),
    Job(
        name="sales-reps-sync-check",
        cron="*/30 * * * *",
        callable_path="agents.sales_reps.scheduler.jobs:sync_check",
        description="Every 30 min: Momentum↔SF ActivityHistory diff (rate-gated alerts)",
    ),
    Job(
        name="sales-reps-risk-sweep",
        cron="0 */2 * * *",
        callable_path="agents.sales_reps.scheduler.jobs:risk_sweep",
        description="Every 2h: deal-risk sweep (pushed close / amount drop / competitor)",
    ),
    Job(
        name="sales-reps-hygiene-daily",
        cron="0 7 * * 1-5",
        callable_path="agents.sales_reps.scheduler.jobs:hygiene_daily",
        description="07:00 ET Mon-Fri: pipeline hygiene report (org-wide)",
    ),
    Job(
        name="sales-reps-leaderboard-weekly",
        cron="0 16 * * 5",
        callable_path="agents.sales_reps.scheduler.jobs:leaderboard_weekly",
        description="Friday 16:00 ET: AE + SDR leaderboard snapshot (Hutch-gated post)",
    ),
    Job(
        name="sales-reps-scorecards-weekly",
        cron="0 17 * * 5",
        callable_path="agents.sales_reps.scheduler.jobs:scorecards_weekly",
        description="Friday 17:00 ET: per-rep scorecards (Hutch-gated DMs first 4 weeks)",
    ),
]


def by_name(name: str) -> Job | None:
    return next((j for j in SCHEDULE if j.name == name), None)
