from shared.runtime.schedule import SCHEDULE, by_name


def test_schedule_has_core_jobs():
    names = {j.name for j in SCHEDULE}
    assert {"oo-daemon", "oo-briefing-daily", "oo-briefing-weekly",
            "oo-board-monitor", "oo-integration-health"}.issubset(names)


def test_by_name():
    assert by_name("oo-daemon") is not None
    assert by_name("nonexistent") is None


def test_launchd_plist_renders():
    from shared.runtime.launchd.generate import render
    job = by_name("oo-board-monitor")
    xml = render(job, "/tmp/repo", "/tmp/repo/.venv/bin/python", "/tmp/repo/var/log")
    assert "StartCalendarInterval" in xml
    assert "com.loop-revops.oo-board-monitor" in xml


def test_lint_plugin_importable():
    # Plugin must load without error for pylint to consume it
    from shared.lint import import_rules
    assert hasattr(import_rules, "register")
    assert hasattr(import_rules, "CrossAgentImportChecker")
