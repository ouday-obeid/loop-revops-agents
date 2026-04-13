"""sales_reps tests inherit the repo-wide DB isolation fixture from tests/conftest.py.

pytest collects conftest.py by directory; placing this empty module alongside our
tests is enough to let pytest walk up and find the session-scoped fixture. We
re-export nothing here, but keep the file so future sales_reps-only fixtures
have a home.
"""
