"""Self-serve coordinator is a deliberate stub until Sundar's interface lands."""
from agents.onboarding.self_serve_coordinator import coordinate


def test_coordinate_returns_deferred():
    result = coordinate("a01ABC")
    assert result["status"] == "deferred"
    assert "sundar" in result["reason"].lower()
    assert result["onboarding_id"] == "a01ABC"
