"""Fireflies MCP — GraphQL client wrapping Fireflies.ai API."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from shared.secrets import require_secret

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.fireflies.ai/graphql"


class FirefliesError(RuntimeError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=_ENDPOINT,
        headers={
            "Authorization": f"Bearer {require_secret('FIREFLIES_API_KEY')}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


def _gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as c:
        resp = c.post("", json={"query": query, "variables": variables or {}})
    if resp.status_code != 200:
        raise FirefliesError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if "errors" in data:
        raise FirefliesError(str(data["errors"]))
    return data.get("data", {})


def list_transcripts(
    from_date: str | None = None,
    to_date: str | None = None,
    participant_email: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = """
    query($limit: Int, $from: DateTime, $to: DateTime, $email: String) {
      transcripts(limit: $limit, fromDate: $from, toDate: $to, participant_email: $email) {
        id title date duration host_email participants
      }
    }
    """
    data = _gql(query, {"limit": limit, "from": from_date, "to": to_date, "email": participant_email})
    return data.get("transcripts", [])


def get_transcript(transcript_id: str) -> dict[str, Any]:
    query = """
    query($id: String!) {
      transcript(id: $id) {
        id title date duration host_email participants
        summary { overview action_items keywords }
        sentences { text speaker_name start_time }
      }
    }
    """
    return _gql(query, {"id": transcript_id}).get("transcript", {})


def search_transcripts(search: str, limit: int = 20) -> list[dict[str, Any]]:
    query = """
    query($search: String!, $limit: Int) {
      transcripts(title_contains: $search, limit: $limit) {
        id title date host_email
      }
    }
    """
    return _gql(query, {"search": search, "limit": limit}).get("transcripts", [])


def get_meeting_summary(transcript_id: str) -> dict[str, Any]:
    query = """
    query($id: String!) {
      transcript(id: $id) {
        id title
        summary { overview action_items keywords short_summary bullet_gist }
      }
    }
    """
    return _gql(query, {"id": transcript_id}).get("transcript", {})


def _smoke() -> None:
    rows = list_transcripts(limit=1)
    print(f"fireflies transcripts fetched: {len(rows)}")


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:
        _smoke()
