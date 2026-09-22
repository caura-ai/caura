#!/usr/bin/env python3
"""pm-0918-c-04 — how often does the atomic-fact fan-out actually fire, by content length?

A70 shipped partly on a measurement that the fan-out "almost never fires", taken
on conversational content. reg-a75, the proof gate that would have covered the
rest, was closed WITHOUT BEING RUN because its harness lived in a private repo
nobody could reach. This script exists so that cannot happen to this number: it
is a plain query over a live database, it needs nothing but psycopg, and it
lives in the repo it makes claims about.

Run it against any store. Read the findings document BEFORE trusting a number
out of it — ``docs/atomic-fact-fanout/pm-c04-fanout-rate-findings.md`` explains
why an aggregate over the development corpus is misleading and what this query
structurally cannot see:

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
-- Broken out by write_mode and by benchmark provenance ON PURPOSE. A single
-- aggregate over this corpus is misleading, and that is not hypothetical: the
-- first version of this measurement reported one, and its >=2,000-character
-- band turned out to be 99.4% synthetic benchmark conversation. See
-- docs/atomic-fact-fanout/pm-c04-fanout-rate-findings.md.
--
-- Also note what this CANNOT see. It infers "fanned out" from surviving linked
-- children, so a parent whose facts were all deduplicated on creation reads as
-- "did not fan out"; and before A70 (2026-09-09) the deferred path computed
-- atomic_facts and discarded them, so any fast-mode write older than that reads
-- the same way. Counting the enricher's DECISION is the measurement that
-- actually answers the question.
with kids as (
  select (metadata->>'parent_memory_id')::uuid as pid
  from memories
  where metadata->>'source' = 'atomic_fact_fanout'
),
kc as (select pid, count(*) as n from kids group by 1),
p as (
  select m.id,
         length(m.content) as len,
         coalesce(kc.n, 0) as children,
         coalesce(m.metadata->>'write_mode', '(unset)') as write_mode,
         (m.metadata->>'benchmark' is not null) as is_benchmark
  from memories m
  left join kc on kc.pid = m.id
  where m.metadata->>'source' is distinct from 'atomic_fact_fanout'
    and json_typeof(m.metadata) = 'object'
)
select is_benchmark,
       write_mode,
       case
         when len < 500  then '<500'
         when len < 1000 then '500-999'
         when len < 2000 then '1000-1999'
         when len < 3000 then '2000-2999'
         else                 '>=3000' end as bucket,
       count(*)                                            as parents,
       count(*) filter (where children > 0)                as fanned,
       round(100.0 * count(*) filter (where children > 0)
             / nullif(count(*), 0), 2)                     as fanout_rate_pct,
       sum(children)                                       as children_made
from p
group by 1, 2, 3
having count(*) > 0
order by is_benchmark, write_mode, min(len);
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
    print("Fan-out rate by benchmark provenance, write_mode and content length\n")
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
