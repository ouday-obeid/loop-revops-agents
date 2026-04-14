"""Deploy a proposed schema change to sandbox with RunLocalTests.

Runs BEFORE gate approval. The reviewer uses the sandbox test result as
evidence: a clean run is expected before O clicks approve on the gate. If
sandbox fails, we stamp the bundle's change.yaml with the failure payload
and post to the #agent-revops-log channel so the reviewer sees it.

Contract:
  test(slug) → SandboxTestResult with status in {"passed", "failed", "error"}

The `salesforce_mcp.deploy_metadata(intent="sandbox", ...)` helper resolves
`SF_SANDBOX_ORG_ALIAS` and invokes `sf project deploy start`. test_level is
RunLocalTests for CustomField changes — Salesforce's default deploy test
scope when schema is touched.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from shared.mcp import salesforce_mcp

from .change_proposer import _pending_dir, load_proposal  # noqa: F401 (re-exported)

log = logging.getLogger(__name__)

AGENT_NAME = "revops_support"
DEFAULT_TEST_LEVEL = "RunLocalTests"


@dataclass
class SandboxTestResult:
    slug: str
    status: str  # "passed" | "failed" | "error"
    deploy_id: str | None = None
    component_failures: list[dict[str, Any]] = field(default_factory=list)
    test_failures: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None
    tested_at: str = ""

    def to_manifest_entry(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "deploy_id": self.deploy_id,
            "component_failures": self.component_failures,
            "test_failures": self.test_failures,
            "error_message": self.error_message,
            "tested_at": self.tested_at,
        }


def _bundle_dir(slug: str) -> Path:
    return _pending_dir() / slug


def _source_dir(slug: str) -> Path:
    return _bundle_dir(slug) / "force-app"


def _summarize_deploy(raw: dict[str, Any]) -> tuple[str, list[dict], list[dict]]:
    """Extract pass/fail + failure details from `sf project deploy start` JSON."""
    details = raw.get("details") or raw.get("deployDetails") or {}
    comp_failures = details.get("componentFailures") or []
    if isinstance(comp_failures, dict):
        comp_failures = [comp_failures]
    runs = details.get("runTestResult") or {}
    test_failures = runs.get("failures") or []
    if isinstance(test_failures, dict):
        test_failures = [test_failures]

    success = raw.get("success")
    status = raw.get("status")
    # `sf project deploy start` returns success=bool + status string; treat either
    # False-success OR non-Succeeded status as a fail — RunLocalTests failures
    # bubble up as success=false even when components deployed.
    ok = bool(success) and str(status).lower() in ("succeeded", "succeededpartial", "")
    return ("passed" if ok else "failed", comp_failures, test_failures)


def _update_manifest(slug: str, sandbox_block: dict[str, Any]) -> None:
    path = _bundle_dir(slug) / "change.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["sandbox_test"] = sandbox_block
    if sandbox_block.get("status") == "passed":
        manifest["status"] = "sandbox_passed"
    else:
        manifest["status"] = "sandbox_failed"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))


def test(
    slug: str,
    *,
    test_level: str = DEFAULT_TEST_LEVEL,
    deploy_fn: Callable[..., dict[str, Any]] = salesforce_mcp.deploy_metadata,
    now: datetime | None = None,
) -> SandboxTestResult:
    """Deploy to sandbox. Caller pre-checks that the bundle exists."""
    now = now or datetime.now(timezone.utc)
    source = _source_dir(slug)
    if not source.exists():
        raise FileNotFoundError(f"no force-app tree at {source}")

    result = SandboxTestResult(slug=slug, status="error", tested_at=now.isoformat())

    try:
        raw = deploy_fn(
            str(source),
            intent="sandbox",
            check_only=False,
            test_level=test_level,
        )
    except Exception as e:  # noqa: BLE001
        result.error_message = str(e)[:500]
        log.exception("sandbox deploy failed for slug=%s", slug)
        _update_manifest(slug, result.to_manifest_entry())
        return result

    result.deploy_id = raw.get("id") or raw.get("deployId")
    status, comp_failures, test_failures = _summarize_deploy(raw)
    result.status = status
    result.component_failures = comp_failures
    result.test_failures = test_failures

    _update_manifest(slug, result.to_manifest_entry())
    log.info(
        "sandbox test complete slug=%s status=%s deploy_id=%s comp_failures=%d test_failures=%d",
        slug, result.status, result.deploy_id,
        len(result.component_failures), len(result.test_failures),
    )
    return result
