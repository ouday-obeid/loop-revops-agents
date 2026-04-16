"""Deal Desk — territory, quota, commissions.

Phase 1.5 of A5 Admin. Every module in this package is policy-driven: the
canonical configuration lives in `policy.yaml` alongside the code. Each
policy section starts life as `UNSIGNED` and is replaced with real values
only after Henry + Anand sign off in writing.

The test `tests/test_deal_desk_policy.py::test_policy_fully_signed` fails
while any `UNSIGNED` sentinel remains, making the branch unmergeable until
policy is approved.
"""
