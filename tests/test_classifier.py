from agents.oo.classifier import classify, is_alertworthy


def test_urgent_fire():
    c = classify("This is URGENT, the pipeline is broken now")
    assert c.category in ("urgent_fire", "automation_broken")
    assert is_alertworthy(c)


def test_automation_broken():
    c = classify("the Salesforce flow is broken and not firing")
    assert c.category == "automation_broken"
    assert is_alertworthy(c)


def test_integration_broken():
    c = classify("Salesforce is down, auth fail")
    assert c.category == "integration_broken"


def test_renewal():
    c = classify("this renewal is at risk, might churn")
    assert c.category == "renewal_issue"


def test_other_fallback():
    c = classify("random chatter")
    assert c.category == "other"
    assert not is_alertworthy(c)
