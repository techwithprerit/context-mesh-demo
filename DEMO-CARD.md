# Demo card (print this, keep it next to the laptop)

```
BACKSTAGE      make up · pip install -e . · export BUS_URL=redis://localhost:6379/0
               make reset · (30 s) · mesh preflight  →  must say GO    (or all of it: make stage)
PANE B         mesh watch --all              (right pane, leave it)
PANE A         mesh demo --approved-by <you> (left pane; Enter per step)

BEAT 1  0:00   "Quiet estate. Let me ask the graph about orders."           Enter → explain
        0:30   "A deploy is a write; a human approves it."                   Enter → refused, then recorded
               POINT AT PANE B. Four nodes. "Nobody correlated anything yet. Now I ask."
        1:00   Enter → explain: cause / action / hot edges / evidence / one request / sources
               "No provenance, no verdict."

BEAT 2  2:00   "Same threshold, crossed twice."                              Enter → failover
               SUPPRESSED in ~6 s: "expected, re-check in 2 min, sleep. The rule is a sentence."
               wait for cpu_high.cleared (~45 s; director waits)
        3:00   "No story behind it this time."                               Enter → cpu
               PAGE in ~5 s: "same number; one said sleep, one said wake up; both said why."

BEAT 3  3:45   Enter → audit slo:   payments BURNING, orders/web ELEVATED, every p99 BREACH
               "policy maths · metrics ratios · traces edge · the graph only joined"
        4:20   Enter → audit posture: 5 findings; walk sql_tls (+graph context) and write_tools_human
        5:00   "Read-only tools, a bus, one graph. The model is optional."   → slide 25

SPARES         mesh ask "is orders safe to cut over right now?"   (GO with notes)
               mesh chaos freeze on && mesh deploy web v9 --approved-by <you> && mesh audit posture
               mesh ask "who depends on orders-db" · mesh events -n 10

IF BROKEN      preflight FAIL → make reset, 30 s, preflight again
               pane B silent → wrong BUS_URL
               no PAGE → chaos cpu waits for the cleared edge; let it
               dead laptop → asciinema play -s 2 demo.cast
```
