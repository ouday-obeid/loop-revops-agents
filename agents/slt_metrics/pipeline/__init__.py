"""Pipeline data layer — fetch from SF, snapshot to DB, diff for movers.

Pure-data config lives in `config.py` so scorer/snapshotter/Excel builder all
reference the same stage/segment/product definitions without an I/O dependency.
"""
