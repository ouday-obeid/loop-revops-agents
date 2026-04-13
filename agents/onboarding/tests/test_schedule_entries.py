"""Verify the 4 onboarding schedule entries are wired and importable."""
from __future__ import annotations

from shared.runtime.schedule import SCHEDULE, by_name


ONBOARDING_JOBS = (
    "onboarding-closed-won-poller",
    "onboarding-milestone-monitor",
    "onboarding-location-sweep",
    "onboarding-jackie-digest",
)


def test_all_four_onboarding_jobs_present():
    names = {j.name for j in SCHEDULE}
    for expected in ONBOARDING_JOBS:
        assert expected in names, f"missing schedule entry: {expected}"


def test_poller_callable_path_resolves():
    job = by_name("onboarding-closed-won-poller")
    assert job is not None
    module, func = job.callable_path.split(":")
    mod = __import__(module, fromlist=[func])
    assert callable(getattr(mod, func))


def test_monitor_callable_path_resolves():
    job = by_name("onboarding-milestone-monitor")
    module, func = job.callable_path.split(":")
    mod = __import__(module, fromlist=[func])
    assert callable(getattr(mod, func))


def test_sweep_callable_path_resolves():
    job = by_name("onboarding-location-sweep")
    module, func = job.callable_path.split(":")
    mod = __import__(module, fromlist=[func])
    assert callable(getattr(mod, func))


def test_digest_callable_path_resolves():
    job = by_name("onboarding-jackie-digest")
    module, func = job.callable_path.split(":")
    mod = __import__(module, fromlist=[func])
    assert callable(getattr(mod, func))


def test_crons_are_five_field():
    for job in SCHEDULE:
        if job.name.startswith("onboarding-"):
            parts = job.cron.split()
            assert len(parts) == 5, f"{job.name}: expected 5 cron fields, got {parts}"
