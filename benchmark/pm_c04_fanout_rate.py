#!/usr/bin/env python3
"""pm-0918-c-04 — how often does the atomic-fact fan-out actually fire, by content length?

A70 shipped partly on a measurement that the fan-out "almost never fires", taken
on conversational content. reg-a75, the proof gate that would have covered the
rest, was closed WITHOUT BEING RUN because its harness lived in a private repo
nobody could reach. This script exists so that cannot happen to this number: it
is a plain query over a live database, it needs nothing but psycopg, and it
lives in the repo it makes claims about.

Run it against any store to re-check the claim in
``docs/atomic-fact-fanout/pm-c04-fanout-rate-findings.md``:

    python3 benchmark/pm_c04_fanout_rate.py "postgresql://user@host:5432/<database>"

or, against the local docker stack:

    python3 benchmark/pm_c04_fanout_rate.py     # defaults to the local docker stack

Children are identified by ``metadata->>'source' = 'atomic_fact_fanout'``. NOTE:
the tracker row named ``split_of`` as the marker — nothing writes that, so a
query using it returns zero and reads as "never fires", which is the same shape
as the original null result that let this ship.
"""

import subprocess
import sys

QUERY = """
with kids as (
  select (metadata->>'parent_memory_id')::uuid as pid
  from memories
  where metadata->>'source' = 'atomic_fact_fanout'
),
kc as (select pid, count(*) as n from kids group by pid),
p as (
  select m.id, length(m.content) as len, coalesce(kc.n, 0) as children
  from memories m
  left join kc on kc.pid = m.id
  where m.metadata->>'source' is distinct from 'atomic_fact_fanout'
)
select case
         when len < 500  then '<500'
         when len < 1000 then '500-999'
         when len < 2000 then '1000-1999'
         when len < 3000 then '2000-2999'
         else                 '>=3000' end as bucket,
       count(*)                                              as parents,
       count(*) filter (where children > 0)                  as fanned,
       round(100.0 * count(*) filter (where children > 0)
             / nullif(count(*), 0), 2)                       as fanout_rate_pct,
       sum(children)                                         as children_made,
       round(avg(children) filter (where children > 0), 2)   as kids_per_fanned_parent
from p
group by 1
order by min(len);
"""


def main() -> int:
    if len(sys.argv) > 1:
        dsn = sys.argv[1]
        cmd = ["psql", dsn, "-c", QUERY]
    else:
        # Default: the local docker stack, which is where the findings doc's
        # numbers came from.
        cmd = [
            "docker",
            "exec",
            "caura-memclaw-enterprise-postgres-1",  # legacy-name-floor: the running container bears this name
            "psql",
            "-U",
            "caura",
            "-d",
            "memclaw",  # legacy-name-floor: the database bears this name
            "-c",
            QUERY,
        ]
    print("Fan-out rate by parent content length\n")
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
