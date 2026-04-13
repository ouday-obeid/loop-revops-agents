"""Self-serve coordinator — STUB.

Deferred per O (2026-04-13). Sundar's in-flight self-serve onboarding system
doesn't have a defined interface yet. This module exists so the agent's
import surface is stable; the real implementation lands in a future phase
once Sundar's spec is available.

Expected future interface (when Sundar publishes):
  coordinate(onboarding_id: str) -> dict
      Orchestrate handoff between the Onboarding agent and the self-serve
      product flow. Likely responsibilities:
        - Check Onboarding__c eligibility for self-serve (plan tier,
          integration requirements).
        - Create / link the self-serve account provisioning record.
        - Track activation milestones and reflect them back into
          Onboarding__c.JK_Onboarding_Stage__c.

For now this is a one-liner that returns a deferred marker — callers should
treat that status as "not yet coordinated" and fall back to the standard
human-CSM path.
"""
from __future__ import annotations

from typing import Any


def coordinate(onboarding_id: str) -> dict[str, Any]:
    """No-op until Sundar's self-serve interface lands."""
    return {
        "status": "deferred",
        "reason": "awaiting spec from sundar",
        "onboarding_id": onboarding_id,
    }
