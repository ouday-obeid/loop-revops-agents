"""Daily digest formatter — builds Slack Block Kit message from Processor data."""

from __future__ import annotations

import calendar
from datetime import datetime

import pandas as pd

from core.config_schema import Config
from core.processor import Processor
from slack_bot.movers import DealMove


def _fc(v: float) -> str:
    return f"${v:,.0f}"


def _fp(v: float) -> str:
    return f"{v * 100:.1f}%"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _code(lines: list[str]) -> str:
    return "```\n" + "\n".join(lines) + "\n```"


def _current_quarter() -> tuple[str, int, list[int]]:
    m = datetime.now().month
    q = (m - 1) // 3 + 1
    return f"Q{q}", q, list(range((q - 1) * 3 + 1, q * 3 + 1))


def generate_digest(
    proc: Processor,
    cfg: Config,
    movers: list[DealMove] | None = None,
    workbook_link: str | None = None,
    forecast_note: str | None = None,
) -> list[dict]:
    today = datetime.now().strftime("%b %d, %Y").replace(" 0", " ")
    now = datetime.now()
    current_q, q_num, q_months = _current_quarter()
    current_month = now.month
    ae_names = cfg.ae_only_names
    sdr_names = cfg.sdr_names
    segs = ["SMB", "MM", "Ent"]
    blocks: list[dict] = []

    all_won = proc.closed_won
    won_mtd = all_won[all_won["close_month"] == current_month]
    won_qtd = all_won[all_won["close_month"].isin(q_months)]
    won_ytd = all_won

    # Segment helper functions
    def _acv(df, seg=None):
        d = df if seg is None else df[df["segment"] == seg]
        return float(d["acv"].sum())

    def _locs(df, seg=None):
        d = df if seg is None else df[df["segment"] == seg]
        return int(d["locations"].sum()) if "locations" in d.columns else 0

    def _deals(df, seg=None):
        return len(df) if seg is None else len(df[df["segment"] == seg])

    def _arpl(df, seg=None):
        a, l = _acv(df, seg), _locs(df, seg)
        return a / l if l > 0 else 0.0

    def _ads(df, seg=None):
        a, d = _acv(df, seg), _deals(df, seg)
        return a / d if d > 0 else 0.0

    def _lpd(df, seg=None):
        l, d = _locs(df, seg), _deals(df, seg)
        return l / d if d > 0 else 0.0

    # Quarterly segment targets (from funnel targets * segment ADS/LPD)
    seg_q_acv_tgt = {s: cfg.quarterly_funnel_target(current_q, s) * cfg.segment_target_ads(s) for s in segs}
    seg_q_loc_tgt = {s: cfg.quarterly_funnel_target(current_q, s) * cfg.segments.get(s, {}).get("target_lpd", 0) for s in segs}
    seg_q_deal_tgt = {s: cfg.quarterly_funnel_target(current_q, s) for s in segs}

    q_len = len(q_months)
    seg_m_acv_tgt = {s: seg_q_acv_tgt[s] / q_len for s in segs}
    seg_m_loc_tgt = {s: seg_q_loc_tgt[s] / q_len for s in segs}
    seg_m_deal_tgt = {s: seg_q_deal_tgt[s] / q_len for s in segs}

    # YTD segment targets — accumulate quarterly targets for all quarters up to current
    seg_y_acv_tgt = {}
    seg_y_loc_tgt = {}
    seg_y_deal_tgt = {}
    for s in segs:
        y_acv = 0.0
        y_loc = 0.0
        y_deal = 0.0
        for qi in range(1, q_num + 1):
            ql = f"Q{qi}"
            y_deal += cfg.quarterly_funnel_target(ql, s)
            y_acv += cfg.quarterly_funnel_target(ql, s) * cfg.segment_target_ads(s)
            y_loc += cfg.quarterly_funnel_target(ql, s) * cfg.segments.get(s, {}).get("target_lpd", 0)
        seg_y_acv_tgt[s] = y_acv
        seg_y_loc_tgt[s] = y_loc
        seg_y_deal_tgt[s] = y_deal

    # Overall monthly targets
    mtd_total_tgt = cfg.monthly_target(current_month, "new_biz") + cfg.monthly_target(current_month, "expansion")
    qtd_total_tgt = sum(cfg.monthly_target(m, "new_biz") + cfg.monthly_target(m, "expansion") for m in q_months)
    ytd_total_tgt = sum(cfg.monthly_target(m, "new_biz") + cfg.monthly_target(m, "expansion") for m in range(1, current_month + 1))

    C = 12  # column width

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # HEADER
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"\U0001f4ca Pipeline Update \u2014 {today}"},
    })
    blocks.append({"type": "divider"})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 1. KPI SUMMARY
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    annual_target = cfg.targets.get("net_new_arr", 0) + cfg.targets.get("expansion_arr", 0)
    attainment = proc.ytd_closed_won_acv / annual_target if annual_target > 0 else 0

    blocks.append(_section("\n".join([
        f"*YTD Closed Won*      {_fc(proc.ytd_closed_won_acv)}  ({_fp(attainment)} to plan)",
        f"*Open Pipeline*        {_fc(float(proc.forecastable_pipeline['acv'].sum()))}",
        f"*Weighted Pipeline*  {_fc(proc.total_weighted_pipeline)}",
        f"*Win Rate*                  {_fp(proc.overall_win_rate)}",
        f"*Pipeline Coverage*  {proc.pipeline_coverage:.1f}x",
    ])))
    blocks.append({"type": "divider"})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 2. UNIT ECONOMICS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    blocks.append(_section("\U0001f4cf *Unit Economics*"))

    hdr = f"{'Metric':<16} {'SMB':>{C}} {'MM':>{C}} {'Ent':>{C}} {'Total':>{C}}"
    sep = f"{'-'*16} {'-'*C} {'-'*C} {'-'*C} {'-'*C}"

    ue = [hdr, sep]

    periods = [
        ("MTD", won_mtd, seg_m_acv_tgt, seg_m_loc_tgt, mtd_total_tgt),
        ("QTD", won_qtd, seg_q_acv_tgt, seg_q_loc_tgt, qtd_total_tgt),
        ("YTD", won_ytd, seg_y_acv_tgt, seg_y_loc_tgt, ytd_total_tgt),
    ]

    for plabel, pdf, acv_tgt, loc_tgt, total_tgt in periods:
        # ACV
        ue.append(f"{'ACV '+plabel+' Tgt':<16} {_fc(acv_tgt['SMB']):>{C}} {_fc(acv_tgt['MM']):>{C}} {_fc(acv_tgt['Ent']):>{C}} {_fc(total_tgt):>{C}}")
        ue.append(f"{'ACV '+plabel+' Act':<16} {_fc(_acv(pdf,'SMB')):>{C}} {_fc(_acv(pdf,'MM')):>{C}} {_fc(_acv(pdf,'Ent')):>{C}} {_fc(_acv(pdf)):>{C}}")
        # Locs
        ue.append(f"{'Loc '+plabel+' Tgt':<16} {int(loc_tgt['SMB']):>{C},} {int(loc_tgt['MM']):>{C},} {int(loc_tgt['Ent']):>{C},} {int(sum(loc_tgt.values())):>{C},}")
        ue.append(f"{'Loc '+plabel+' Act':<16} {_locs(pdf,'SMB'):>{C},} {_locs(pdf,'MM'):>{C},} {_locs(pdf,'Ent'):>{C},} {_locs(pdf):>{C},}")
        # ARPL
        arpl_tgts = {s: cfg.segments.get(s, {}).get("target_arpl", 0) for s in segs}
        ue.append(f"{'ARPL '+plabel+' Tgt':<16} {_fc(arpl_tgts['SMB']):>{C}} {_fc(arpl_tgts['MM']):>{C}} {_fc(arpl_tgts['Ent']):>{C}} {_fc(cfg.blended_targets.get('arpl',0)):>{C}}")
        ue.append(f"{'ARPL '+plabel+' Act':<16} {_fc(_arpl(pdf,'SMB')):>{C}} {_fc(_arpl(pdf,'MM')):>{C}} {_fc(_arpl(pdf,'Ent')):>{C}} {_fc(_arpl(pdf)):>{C}}")
        # ADS
        ue.append(f"{'ADS '+plabel+' Tgt':<16} {_fc(cfg.segment_target_ads('SMB')):>{C}} {_fc(cfg.segment_target_ads('MM')):>{C}} {_fc(cfg.segment_target_ads('Ent')):>{C}} {_fc(cfg.blended_targets.get('ads',0)):>{C}}")
        ue.append(f"{'ADS '+plabel+' Act':<16} {_fc(_ads(pdf,'SMB')):>{C}} {_fc(_ads(pdf,'MM')):>{C}} {_fc(_ads(pdf,'Ent')):>{C}} {_fc(_ads(pdf)):>{C}}")
        # LPD
        lpd_tgts = {s: cfg.segments.get(s, {}).get("target_lpd", 0) for s in segs}
        ue.append(f"{'LPD '+plabel+' Tgt':<16} {lpd_tgts['SMB']:>{C}.1f} {lpd_tgts['MM']:>{C}.1f} {lpd_tgts['Ent']:>{C}.1f} {cfg.blended_targets.get('lpd',0):>{C}.1f}")
        ue.append(f"{'LPD '+plabel+' Act':<16} {_lpd(pdf,'SMB'):>{C}.1f} {_lpd(pdf,'MM'):>{C}.1f} {_lpd(pdf,'Ent'):>{C}.1f} {_lpd(pdf):>{C}.1f}")

        if plabel != "YTD":
            ue.append(sep)

    blocks.append(_section(_code(ue)))
    blocks.append({"type": "divider"})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 3. Q PIPELINE — Demo / Biz Case / Proposal by segment
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    pipeline_stages = ["Demo", "Business Case", "Proposal"]
    slabels = {"Demo": "Demo", "Business Case": "Biz Case", "Proposal": "Proposal"}

    blocks.append(_section(f"\U0001f3af *{current_q} Pipeline \u2014 by Stage & Segment*"))

    pipe = proc.open_pipeline

    p_rows = [
        f"{'Stage':<14} {'SMB':>6} {'MM':>6} {'Ent':>6} {'Total':>6} {'ACV':>12} {'Locs':>6} {'Wtd ACV':>12}",
        f"{'-'*14} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*12} {'-'*6} {'-'*12}",
    ]
    gt = {"s": 0, "m": 0, "e": 0, "t": 0, "a": 0.0, "l": 0, "w": 0.0}

    for stage in pipeline_stages:
        sdf = pipe[pipe["stage"] == stage]
        cs = {s: _deals(sdf, s) for s in segs}
        t = len(sdf); a = _acv(sdf); l = _locs(sdf); w = float(sdf["weighted_acv"].sum())
        p_rows.append(f"{slabels[stage]:<14} {cs['SMB']:>6} {cs['MM']:>6} {cs['Ent']:>6} {t:>6} {_fc(a):>12} {l:>6} {_fc(w):>12}")
        gt["s"] += cs["SMB"]; gt["m"] += cs["MM"]; gt["e"] += cs["Ent"]
        gt["t"] += t; gt["a"] += a; gt["l"] += l; gt["w"] += w

    p_rows.append(f"{'-'*14} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*12} {'-'*6} {'-'*12}")
    p_rows.append(f"{'Total':<14} {gt['s']:>6} {gt['m']:>6} {gt['e']:>6} {gt['t']:>6} {_fc(gt['a']):>12} {gt['l']:>6} {_fc(gt['w']):>12}")

    # Team breakdown
    p_rows.append("")
    p_rows.append(f"{'Team':<14} {'Deals':>6} {'ACV':>12} {'Locs':>6} {'Wtd ACV':>12}")
    p_rows.append(f"{'-'*14} {'-'*6} {'-'*12} {'-'*6} {'-'*12}")
    pipe_stages_df = pipe[pipe["stage"].isin(pipeline_stages)]
    for mgr, members in cfg.manager_groups.items():
        tdf = pipe_stages_df[pipe_stages_df["owner"].isin(members)]
        p_rows.append(f"{mgr:<14} {len(tdf):>6} {_fc(_acv(tdf)):>12} {_locs(tdf):>6} {_fc(float(tdf['weighted_acv'].sum())):>12}")

    blocks.append(_section(_code(p_rows)))
    blocks.append({"type": "divider"})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 4. TOP OF FUNNEL — Meeting Set by segment with targets
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    blocks.append(_section("\U0001f6a9 *Top of Funnel \u2014 Meetings Set*"))

    meeting_set = pipe[pipe["stage"] == "New - Meeting Set"]
    funnel_actual = proc.quarterly_funnel_actual.get(current_q, {})

    tof = [
        f"{'Segment':<10} {'Deals':>6} {'ACV':>12} {'Locs':>6}",
        f"{'-'*10} {'-'*6} {'-'*12} {'-'*6}",
    ]
    for s in segs:
        sdf = meeting_set[meeting_set["segment"] == s]
        tof.append(f"{s:<10} {len(sdf):>6} {_fc(_acv(sdf)):>12} {_locs(sdf):>6}")
    tof.append(f"{'-'*10} {'-'*6} {'-'*12} {'-'*6}")
    tof.append(f"{'Total':<10} {len(meeting_set):>6} {_fc(_acv(meeting_set)):>12} {_locs(meeting_set):>6}")

    # Funnel pacing — target vs actual closed won deals (Q target)
    tof.append("")
    tof.append(f"{'Pacing':<10} {'Q Tgt':>8} {'Q Act':>8} {'%':>6} {'MTD':>6} {'QTD':>6} {'YTD':>6}")
    tof.append(f"{'-'*10} {'-'*8} {'-'*8} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")

    all_df = proc.df
    for s in segs:
        target = cfg.quarterly_funnel_target(current_q, s)
        actual = funnel_actual.get(s, 0)
        pct = f"{actual / target * 100:.0f}%" if target > 0 else "-"
        seg_df = all_df[all_df["segment"] == s]
        mtd_c = len(seg_df[seg_df["created_month"] == current_month])
        qtd_c = len(seg_df[seg_df["created_month"].isin(q_months)])
        ytd_c = len(seg_df)
        tof.append(f"{s:<10} {target:>8.0f} {actual:>8.0f} {pct:>6} {mtd_c:>6} {qtd_c:>6} {ytd_c:>6}")

    # Team breakdown for meeting set
    tof.append("")
    tof.append(f"{'Team':<14} {'Deals':>6} {'ACV':>12} {'Locs':>6}")
    tof.append(f"{'-'*14} {'-'*6} {'-'*12} {'-'*6}")
    for mgr, members in cfg.manager_groups.items():
        tdf = meeting_set[meeting_set["owner"].isin(members)]
        tof.append(f"{mgr:<14} {len(tdf):>6} {_fc(_acv(tdf)):>12} {_locs(tdf):>6}")

    blocks.append(_section(_code(tof)))
    blocks.append({"type": "divider"})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 5. AE PERFORMANCE — MTD / QTD / YTD + Team rollups
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ae_hdr = f"{'Rep':<22} {'ACV':>12} {'Locs':>6} {'Deals':>6} {'ARPL':>10}"
    ae_sep = f"{'-'*22} {'-'*12} {'-'*6} {'-'*6} {'-'*10}"

    def _ae_table(label, pdf):
        rows = [ae_hdr, ae_sep]
        data = []
        for name in ae_names:
            aw = pdf[pdf["owner"] == name]
            a = _acv(aw); l = _locs(aw); d = len(aw); ar = a / l if l > 0 else 0.0
            data.append((name, a, l, d, ar))
        data.sort(key=lambda r: r[1], reverse=True)
        for name, a, l, d, ar in data:
            rows.append(f"{name[:21]:<22} {_fc(a):>12} {l:>6} {d:>6} {_fc(ar):>10}")

        # Team rollup
        rows.append("")
        rows.append(f"{'Team':<22} {'ACV':>12} {'Locs':>6} {'Deals':>6} {'ARPL':>10}")
        rows.append(f"{'-'*22} {'-'*12} {'-'*6} {'-'*6} {'-'*10}")
        for mgr, members in cfg.manager_groups.items():
            tdf = pdf[pdf["owner"].isin(members)]
            a = _acv(tdf); l = _locs(tdf); d = len(tdf); ar = a / l if l > 0 else 0.0
            rows.append(f"{mgr:<22} {_fc(a):>12} {l:>6} {d:>6} {_fc(ar):>10}")
        return rows

    for plabel, pdf in [("MTD", won_mtd), ("QTD", won_qtd), ("YTD", won_ytd)]:
        blocks.append(_section(f"\U0001f4c8 *AE Performance \u2014 {plabel}*"))

        if plabel == "YTD":
            # YTD gets extra attainment column
            rows = [
                f"{'Rep':<22} {'ACV':>12} {'Locs':>6} {'Deals':>6} {'ARPL':>10} {'Attain':>8}",
                f"{'-'*22} {'-'*12} {'-'*6} {'-'*6} {'-'*10} {'-'*8}",
            ]
            data = []
            for name in ae_names:
                aw = pdf[pdf["owner"] == name]
                a = _acv(aw); l = _locs(aw); d = len(aw); ar = a / l if l > 0 else 0.0
                q = proc.ae_quota(name); att = a / q if q > 0 else 0.0
                data.append((name, a, l, d, ar, att, q))
            data.sort(key=lambda r: r[1], reverse=True)
            for name, a, l, d, ar, att, q in data:
                rows.append(f"{name[:21]:<22} {_fc(a):>12} {l:>6} {d:>6} {_fc(ar):>10} {_fp(att) if q > 0 else '-':>8}")

            rows.append("")
            rows.append(f"{'Team':<22} {'ACV':>12} {'Locs':>6} {'Deals':>6} {'ARPL':>10} {'Attain':>8}")
            rows.append(f"{'-'*22} {'-'*12} {'-'*6} {'-'*6} {'-'*10} {'-'*8}")
            for mgr, members in cfg.manager_groups.items():
                tdf = pdf[pdf["owner"].isin(members)]
                a = _acv(tdf); l = _locs(tdf); d = len(tdf); ar = a / l if l > 0 else 0.0
                tq = sum(proc.ae_quota(n) for n in members)
                att = a / tq if tq > 0 else 0.0
                rows.append(f"{mgr:<22} {_fc(a):>12} {l:>6} {d:>6} {_fc(ar):>10} {_fp(att):>8}")

            blocks.append(_section(_code(rows)))
        else:
            blocks.append(_section(_code(_ae_table(plabel, pdf))))

    blocks.append({"type": "divider"})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 5b. FORECAST SUMMARY (only when forecast doc is loaded)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if proc.rep_forecast is not None:
        from core.forecast_loader import RepForecastData

        forecast_data: RepForecastData = proc.rep_forecast["data"]
        COMMIT_W, HC_W, LS_W = 0.90, 0.75, 0.50

        blocks.append(_section("\U0001f4cb *Forecast Summary \u2014 SLT vs Rep*"))

        f_rows = [
            f"{'Rep':<22} {'Closed':>10} {'Commit':>10} {'HC':>10} {'Longshot':>10} {'Rep Wtd':>10} {'SLT':>10} {'Delta':>10}",
            f"{'-'*22} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10}",
        ]

        rep_data = []
        for mgr, members in cfg.manager_groups.items():
            for name in members:
                if name not in cfg.ae_only_names:
                    continue
                rf = forecast_data.reps.get(name)
                closed = proc.ae_closed_won.get(name, 0)
                slt = proc.ae_slt_forecast.get(name, 0)
                commit = rf.commit_total if rf else 0.0
                hc = rf.hc_total if rf else 0.0
                ls = rf.longshot_total if rf else 0.0
                rep_wtd = closed + commit * COMMIT_W + hc * HC_W + ls * LS_W
                delta = slt - rep_wtd
                rep_data.append((name, mgr, closed, commit, hc, ls, rep_wtd, slt, delta))

        for name, mgr, closed, commit, hc, ls, rep_wtd, slt, delta in rep_data:
            f_rows.append(
                f"{name[:21]:<22} {_fc(closed):>10} {_fc(commit):>10} {_fc(hc):>10} "
                f"{_fc(ls):>10} {_fc(rep_wtd):>10} {_fc(slt):>10} {_fc(delta):>10}"
            )

        # Team rollup
        f_rows.append(f"{'-'*22} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
        for mgr in cfg.manager_groups:
            mgr_reps = [r for r in rep_data if r[1] == mgr]
            if not mgr_reps:
                continue
            tc = sum(r[2] for r in mgr_reps)
            tco = sum(r[3] for r in mgr_reps)
            thc = sum(r[4] for r in mgr_reps)
            tls = sum(r[5] for r in mgr_reps)
            tw = sum(r[6] for r in mgr_reps)
            ts = sum(r[7] for r in mgr_reps)
            td = sum(r[8] for r in mgr_reps)
            f_rows.append(
                f"{mgr:<22} {_fc(tc):>10} {_fc(tco):>10} {_fc(thc):>10} "
                f"{_fc(tls):>10} {_fc(tw):>10} {_fc(ts):>10} {_fc(td):>10}"
            )

        # Grand total
        gc = sum(r[2] for r in rep_data)
        gco = sum(r[3] for r in rep_data)
        ghc = sum(r[4] for r in rep_data)
        gls = sum(r[5] for r in rep_data)
        gw = sum(r[6] for r in rep_data)
        gs = sum(r[7] for r in rep_data)
        gd = sum(r[8] for r in rep_data)
        f_rows.append(f"{'-'*22} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
        f_rows.append(
            f"{'TOTAL':<22} {_fc(gc):>10} {_fc(gco):>10} {_fc(ghc):>10} "
            f"{_fc(gls):>10} {_fc(gw):>10} {_fc(gs):>10} {_fc(gd):>10}"
        )

        blocks.append(_section(_code(f_rows)))
        blocks.append({"type": "divider"})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 6. SDR PERFORMANCE — MTD / QTD / YTD
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    sdr_sourced = proc.df[proc.df["lead_source"] == "SDR"]
    no_hold_stages = {"New - Meeting Set", "No Show"}

    def _sdr_stats(name, months=None):
        opps = sdr_sourced[sdr_sourced["created_by"] == name]
        won_all = opps[opps["is_closed_won"]]
        if months is not None:
            opps = opps[opps["created_month"].isin(months)]
            won_all = won_all[won_all["close_month"].isin(months)]
        sets = len(opps)
        holds = len(opps[~opps["stage"].isin(no_hold_stages)])
        hr = holds / sets if sets > 0 else 0.0
        acv = float(won_all["acv"].sum())
        return sets, holds, hr, acv

    for plabel, pmonths in [("MTD", [current_month]), ("QTD", q_months), ("YTD", None)]:
        blocks.append(_section(f"\U0001f4de *SDR Performance \u2014 {plabel}*"))

        sdr_period = []
        t_sets = t_holds = 0; t_acv = 0.0
        for name in sdr_names:
            sets, holds, hr, acv = _sdr_stats(name, pmonths)
            sdr_period.append((name, sets, holds, hr, acv))
            t_sets += sets; t_holds += holds; t_acv += acv
        sdr_period.sort(key=lambda r: r[1], reverse=True)

        s_rows = [
            f"{'SDR':<22} {'Sets':>6} {'Holds':>6} {'Hold%':>7} {'ACV Won':>12}",
            f"{'-'*22} {'-'*6} {'-'*6} {'-'*7} {'-'*12}",
        ]
        for name, sets, holds, hr, acv in sdr_period:
            s_rows.append(f"{name[:21]:<22} {sets:>6} {holds:>6} {_fp(hr):>7} {_fc(acv):>12}")

        t_hr = t_holds / t_sets if t_sets > 0 else 0.0
        s_rows.append(f"{'-'*22} {'-'*6} {'-'*6} {'-'*7} {'-'*12}")
        s_rows.append(f"{'Team Total':<22} {t_sets:>6} {t_holds:>6} {_fp(t_hr):>7} {_fc(t_acv):>12}")

        if plabel == "YTD":
            target_hr = cfg.rates.get("hold_rate", 0.5)
            s_rows.append("")
            s_rows.append(f"Target Hold Rate: {_fp(target_hr)}  |  Actual: {_fp(t_hr)}")

        blocks.append(_section(_code(s_rows)))

    blocks.append({"type": "divider"})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 7. DEAL SPOTLIGHT
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    blocks.append(_section("\U0001f52d *Deal Spotlight*"))

    pipe_all = proc.forecastable_pipeline.copy()
    if not pipe_all.empty and "close_date" in pipe_all.columns:
        today_ts = pd.Timestamp(now.date())
        end_of_week = today_ts + pd.Timedelta(days=6 - now.weekday())
        end_of_month = pd.Timestamp(now.replace(day=calendar.monthrange(now.year, now.month)[1]).date())
        q_end = q_months[-1]
        end_of_quarter = pd.Timestamp(datetime(now.year, q_end, calendar.monthrange(now.year, q_end)[1]).date())

        fp = pipe_all[pipe_all["close_date"] >= today_ts]
        tw = fp[fp["close_date"] <= end_of_week]
        rm = fp[(fp["close_date"] > end_of_week) & (fp["close_date"] <= end_of_month)]
        rq = fp[(fp["close_date"] > end_of_month) & (fp["close_date"] <= end_of_quarter)]
        pd_deals = pipe_all[pipe_all["close_date"] < today_ts]

        def _spot(label, df, n=8):
            if df.empty:
                return [f"{label}: None", ""]
            lines = [f"{label} ({len(df)} deals | {_fc(float(df['acv'].sum()))})", ""]
            sdf = df.sort_values("acv", ascending=False)
            tbl = [
                f"  {'Deal':<30} {'ACV':>10} {'Stage':<16} {'Owner':<20}",
                f"  {'-'*30} {'-'*10} {'-'*16} {'-'*20}",
            ]
            for _, row in sdf.head(n).iterrows():
                tbl.append(f"  {str(row.get('opp_name',row.get('organization','?')))[:29]:<30} {_fc(float(row['acv'])):>10} {str(row['stage'])[:15]:<16} {str(row['owner'])[:19]:<20}")
            if len(df) > n:
                tbl.append(f"  ... {len(df) - n} more")
            lines.extend(tbl); lines.append("")
            return lines

        spot = []
        spot.extend(_spot("This Week", tw))
        spot.extend(_spot("Rest of Month", rm))
        spot.extend(_spot("Rest of Quarter", rq))
        if not pd_deals.empty:
            spot.extend(_spot("Past Due", pd_deals, 5))
        blocks.append(_section(_code(spot)))

    blocks.append({"type": "divider"})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 8. DEAL MOVERS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if movers:
        blocks.append(_section("\U0001f504 *Deal Movers* (vs yesterday)"))
        icons = {"new_win": "\u2705", "new_loss": "\u274c", "advance": "\u2b06\ufe0f",
                 "regression": "\u2b07\ufe0f", "new_deal": "\U0001f195"}
        ml = []
        for m in movers[:10]:
            ml.append(f"{icons.get(m.move_type, chr(8226))} {m.opp_name} \u2192 {m.new_stage} ({_fc(m.acv)}) \u2014 {m.owner}")
        if len(movers) > 10:
            ml.append(f"    _... {len(movers) - 10} more_")
        blocks.append(_section("\n".join(ml)))

    # ━━ Footer ━━
    blocks.append({"type": "divider"})
    footer_text = f"Generated from Salesforce export \u2022 {today}"
    if forecast_note:
        footer_text += f" \u2022 {forecast_note}"
    if workbook_link:
        footer_text += f"\n\U0001f4c1 <{workbook_link}|Revenue Model Workbook (Excel)>"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer_text}]})

    return blocks
