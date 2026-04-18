"""Circuit breakers + kill switch.

Phase 2 scope:
    circuit_breakers.py  -- 3-layer breakers (daily soft -2%, hard -3%,
                             total -10%; total writes .killed)
    kill_switch.py       -- .killed lock file mechanics

Hard rule per risk-controls.md: only a human can delete .killed. Engine
NEVER deletes it. Claude CANNOT delete it either -- the file lives on
the Mac Mini filesystem outside any Claude-writable path in the normal
permission set.
"""
