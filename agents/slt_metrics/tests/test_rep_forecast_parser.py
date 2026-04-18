"""CSV parser for rep-submitted forecasts."""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.slt_metrics.pipeline import rep_forecast_parser


_GOOD_CSV = """rep_name,quarter,commit_acv,best_case_acv,notes
Sarra Herlich,FY2026-Q2,250000,400000,ramping fast
Alex Reyes,FY2026-Q2,350000,500000,
"""


@pytest.fixture
def good_csv(tmp_path: Path) -> Path:
    p = tmp_path / "good.csv"
    p.write_text(_GOOD_CSV, encoding="utf-8")
    return p


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_parse_happy_path(good_csv):
    entries, errors = rep_forecast_parser.parse_rep_forecast_csv(good_csv)
    assert errors == []
    assert [e.owner_name for e in entries] == ["Sarra Herlich", "Alex Reyes"]
    assert entries[0].commit_acv == 250_000.0
    assert entries[0].best_case_acv == 400_000.0
    assert entries[0].notes == "ramping fast"
    assert entries[1].notes is None


def test_parse_accepts_money_formatting(tmp_path):
    csv = _write(tmp_path, "fmt.csv",
        "rep_name,quarter,commit_acv,best_case_acv,notes\n"
        'Sarra Herlich,FY2026-Q2,"$250,000","$400,000",'
    )
    entries, errors = rep_forecast_parser.parse_rep_forecast_csv(csv)
    assert errors == []
    assert entries[0].commit_acv == 250_000.0
    assert entries[0].best_case_acv == 400_000.0


def test_parse_rejects_empty_rep_name(tmp_path):
    csv = _write(tmp_path, "noname.csv",
        "rep_name,quarter,commit_acv,best_case_acv,notes\n"
        ",FY2026-Q2,100000,200000,"
    )
    entries, errors = rep_forecast_parser.parse_rep_forecast_csv(csv)
    assert entries == []
    assert len(errors) == 1
    assert "empty rep_name" in errors[0]["reason"]


def test_parse_rejects_unknown_rep_name(tmp_path):
    csv = _write(tmp_path, "stranger.csv",
        "rep_name,quarter,commit_acv,best_case_acv,notes\n"
        "Ghost Rep,FY2026-Q2,100000,200000,"
    )
    entries, errors = rep_forecast_parser.parse_rep_forecast_csv(csv)
    assert entries == []
    assert len(errors) == 1
    assert "not in AE/SDR roster" in errors[0]["reason"]


def test_parse_rejects_bad_quarter_format(tmp_path):
    csv = _write(tmp_path, "q.csv",
        "rep_name,quarter,commit_acv,best_case_acv,notes\n"
        "Sarra Herlich,2026-Q2,100000,200000,"
    )
    entries, errors = rep_forecast_parser.parse_rep_forecast_csv(csv)
    assert entries == []
    assert "must match FY####-Q#" in errors[0]["reason"]


def test_parse_rejects_bad_numeric_value(tmp_path):
    csv = _write(tmp_path, "nan.csv",
        "rep_name,quarter,commit_acv,best_case_acv,notes\n"
        "Sarra Herlich,FY2026-Q2,not-a-number,200000,"
    )
    entries, errors = rep_forecast_parser.parse_rep_forecast_csv(csv)
    assert entries == []
    assert "invalid numeric value" in errors[0]["reason"]


def test_parse_partial_failure_keeps_good_rows(tmp_path):
    csv = _write(tmp_path, "mixed.csv",
        "rep_name,quarter,commit_acv,best_case_acv,notes\n"
        "Sarra Herlich,FY2026-Q2,250000,400000,ok\n"
        "Ghost Rep,FY2026-Q2,100000,200000,\n"
        "Alex Reyes,not-a-quarter,350000,500000,\n"
    )
    entries, errors = rep_forecast_parser.parse_rep_forecast_csv(csv)
    assert len(entries) == 1
    assert entries[0].owner_name == "Sarra Herlich"
    assert len(errors) == 2


def test_parse_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        rep_forecast_parser.parse_rep_forecast_csv("/tmp/does-not-exist-abc123.csv")


def test_parse_missing_required_columns_raises(tmp_path):
    csv = _write(tmp_path, "bad_header.csv",
        "name,q,commit\n"
        "Sarra,FY2026-Q2,100000\n"
    )
    with pytest.raises(ValueError) as exc:
        rep_forecast_parser.parse_rep_forecast_csv(csv)
    assert "missing required columns" in str(exc.value)


def test_parse_honors_explicit_known_owners(tmp_path):
    csv = _write(tmp_path, "custom.csv",
        "rep_name,quarter,commit_acv,best_case_acv,notes\n"
        "Custom Rep,FY2026-Q2,100000,200000,\n"
    )
    entries, errors = rep_forecast_parser.parse_rep_forecast_csv(
        csv, known_owners={"Custom Rep"},
    )
    assert len(entries) == 1
    assert errors == []


def test_parse_blank_numeric_becomes_none(tmp_path):
    csv = _write(tmp_path, "blanks.csv",
        "rep_name,quarter,commit_acv,best_case_acv,notes\n"
        "Sarra Herlich,FY2026-Q2,,,\n"
    )
    entries, errors = rep_forecast_parser.parse_rep_forecast_csv(csv)
    assert errors == []
    assert entries[0].commit_acv is None
    assert entries[0].best_case_acv is None
