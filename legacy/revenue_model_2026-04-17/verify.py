"""Verify the fixes: AE/SDR separation and new KPIs."""
from core.config_schema import load_config
from core.loader import load_csv
from core.processor import Processor

cfg = load_config("config.yaml")
df = load_csv(r"C:\Users\odayo\Downloads\report1772468849062.csv", cfg)
proc = Processor(df, cfg)

print("=== AE/SDR FIX VERIFICATION ===")
print(f"\nAE Scorecard will show ({len(cfg.ae_only_roster)} AEs):")
for ae in cfg.ae_only_roster:
    print(f"  {ae['name']:25s} role={ae.get('role','AE'):10s} seg={ae.get('segment','')}")

print(f"\nExcluded from AE tab ({len(cfg.ae_roster) - len(cfg.ae_only_roster)} non-AEs):")
for ae in cfg.ae_roster:
    if ae.get("role", "AE") != "AE":
        print(f"  {ae['name']:25s} role={ae.get('role','')}")

print(f"\nSDR tab will show ({len(cfg.sdr_roster)} SDRs):")
for s in cfg.sdr_roster:
    print(f"  {s['name']:25s} role={s.get('role','SDR'):15s} seg={s.get('segment','')}")

print("\n=== NEW KPI VERIFICATION ===")
print("\nARPL (Target vs Current):")
for seg in ["SMB", "MM", "Ent", "Blended"]:
    if seg == "Blended":
        target = cfg.blended_targets.get("arpl", 0)
    else:
        target = cfg.segment_target_arpl(seg)
    current = proc.current_arpl.get(seg, 0)
    print(f"  {seg:8s}  Target: ${target:>8,.0f}  Current: ${current:>8,.0f}")

print("\nADS (Target vs Current):")
for seg in ["SMB", "MM", "Ent", "Blended"]:
    if seg == "Blended":
        target = cfg.blended_targets.get("ads", 0)
    else:
        target = cfg.segment_target_ads(seg)
    current = proc.current_ads.get(seg, 0)
    print(f"  {seg:8s}  Target: ${target:>10,.0f}  Current: ${current:>10,.0f}")

print("\nAvg Locations Per Deal (Target vs Current):")
for seg in ["SMB", "MM", "Ent", "Blended"]:
    if seg == "Blended":
        target = cfg.blended_targets.get("lpd", 0)
    else:
        target = cfg.segment_target_lpd(seg)
    current = proc.current_lpd.get(seg, 0)
    print(f"  {seg:8s}  Target: {target:>6.1f}  Current: {current:>6.1f}")

print("\nQ1 Funnel Targets vs Actuals (NB deals):")
for seg in ["SMB", "MM", "Ent"]:
    target = cfg.quarterly_funnel_target("Q1", seg)
    actual = proc.quarterly_funnel_actual.get("Q1", {}).get(seg, 0)
    print(f"  {seg:8s}  Target: {target:>6.1f}  Actual TD: {actual:>4.0f}")
