"""degeneracy_v3 live-pilot service package (Phase 1: market-data spine).

Pure decision/state logic is separated from I/O shells throughout (house law:
"pure core, I/O shells"). Money is Decimal everywhere; floats appear only in
statistics (none in this phase). Stdlib + `websockets` + `requests` only.
"""
