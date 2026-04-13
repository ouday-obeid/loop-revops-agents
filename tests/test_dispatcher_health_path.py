import asyncio

from sqlalchemy import text

from agents.oo.dispatcher import OODispatcher
from shared.db.connection import get_engine


def test_dispatcher_health_command():
    with get_engine().begin() as conn:
        conn.execute(text(
            """INSERT INTO integration_health (integration, status, error_message, checked_at)
               VALUES ('salesforce', 'healthy', NULL, CURRENT_TIMESTAMP),
                      ('fireflies', 'down', 'auth fail', CURRENT_TIMESTAMP)"""
        ))
    out = asyncio.run(OODispatcher().run("test", {"text": "health"}))
    assert "salesforce" in out["text"]
    assert "fireflies" in out["text"]


def test_dispatcher_empty_command():
    out = asyncio.run(OODispatcher().run("test", {"text": ""}))
    assert "pong" in out["text"].lower()


def test_dispatcher_unknown_fallthrough():
    out = asyncio.run(OODispatcher().run("test", {"text": "reticulate splines"}))
    assert "received" in out["text"].lower() or "phase 1" in out["text"].lower()
