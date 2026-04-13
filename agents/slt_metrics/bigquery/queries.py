"""SQL constants for Loop Pulse (BigQuery) queries.

Ported 2026-04-13 from LUCID `backend/app/integrations/bigquery_int.py`.
Parametrized by `@run_date` / `@window_days` so the client can bind variables
without string-templating SQL (prevents the classic injection pitfall).
"""
from __future__ import annotations

from typing import Final


# Signal log — account-level engagement events.
SIGNAL_LOG: Final[str] = """
SELECT account_id,
       signal_type,
       signal_date,
       signal_value
FROM `arboreal-vision-339901.account_health.signal_log`
WHERE signal_date >= DATE_SUB(@run_date, INTERVAL @window_days DAY)
  AND signal_date <= @run_date
ORDER BY signal_date DESC
"""


# Accounts master — current state of every Loop customer account.
ACCOUNTS_MASTER: Final[str] = """
SELECT account_id,
       account_name,
       segment,
       arr_current,
       arr_start_of_period,
       logo_status,
       first_close_date,
       churn_date
FROM `arboreal-vision-339901.account_health.accounts_master`
"""


# Stage-4 usage — deepest-funnel engagement snapshot for CAC payback math.
STAGE4_USAGE: Final[str] = """
SELECT account_id,
       stage4_first_hit_date,
       dau_avg_30d,
       product_adoption_score
FROM `arboreal-vision-339901.account_health.stage4_usage`
WHERE stage4_first_hit_date IS NOT NULL
  AND stage4_first_hit_date <= @run_date
"""


# Unit economics composite view (NRR, GRR, logo retention, expansion, CAC payback, LTV:CAC).
UNIT_ECONOMICS: Final[str] = """
SELECT
    SAFE_DIVIDE(SUM(arr_current), SUM(arr_start_of_period)) AS net_revenue_retention,
    SAFE_DIVIDE(
        SUM(CASE WHEN logo_status != 'churned' THEN arr_current ELSE 0 END),
        SUM(arr_start_of_period)
    ) AS gross_revenue_retention,
    SAFE_DIVIDE(
        COUNTIF(logo_status != 'churned'),
        COUNT(*)
    ) AS logo_retention,
    SAFE_DIVIDE(
        SUM(GREATEST(arr_current - arr_start_of_period, 0)),
        SUM(arr_start_of_period)
    ) AS expansion_rate,
    AVG(NULLIF(cac_payback_months, 0)) AS cac_payback_months,
    AVG(NULLIF(ltv_cac_ratio, 0)) AS ltv_cac_ratio
FROM `arboreal-vision-339901.account_health.accounts_master`
WHERE arr_start_of_period > 0
"""
