#!/usr/bin/env python3
"""pm-0918-c-03 — how much of a store, and of a top_k, is a DERIVED row?

The ``include_derived`` decision turns on one number nobody had measured: what
fraction of the rows ``/search`` returns are fan-out children rather than the
memories a caller wrote. This script is the re-runnable half of that question —
the write-side population, straight out of a database, in the repo that makes
the claim.

Read ``docs/atomic-fact-fanout/pm-c03-include-derived-blast-radius.md`` BEFORE
quoting a number out of it. The development corpus is not representative and
the document says exactly how; a single aggregate over it is the mistake
pm-0918-c-04 already made once.

    python3 benchmark/pm_c03_derived_row_population.py "postgresql://user@host:5432/<database>"
    python3 benchmark/pm_c03_derived_row_population.py              # local docker stack, `caura` database
    python3 benchmark/pm_c03_derived_row_population.py --db memclaw # local docker stack, another database

WHAT COUNTS AS DERIVED, and why the predicate is a conjunction
--------------------------------------------------------------
Exactly two writers put ``parent_memory_id`` into child metadata
(``core-api/src/core_api/services/memory_service.py``):

  * ``source="atomic_fact_fanout"`` — the A70 fan-out. Facts extracted FROM a
    parent that keeps its full text and stays retrievable. Redundant by
    construction; this is the population the row is about.
  * ``source="auto_chunk"``       — the >2,000-character chunker. These ARE the
    chunk-level retrieval representation of a long document. The parent holds
    the full content but carries ONE embedding over all of it, so excluding
    these does not merely drop duplicates — it removes the only sub-document
    vector the store has.

So ``parent_memory_id IS NOT NULL`` alone is the WRONG predicate: it catches
auto-chunk children too. And ``source`` alone is also wrong: ``source`` is
deliberately absent from ``PLATFORM_ONLY_KEYS``
(``core-api/src/core_api/services/system_metadata.py``), so a caller can put any
value in it, including these. ``parent_memory_id`` IS reserved and is stripped
from caller input. The conjunction is therefore both precise and unforgeable,
and it is what this script counts.

The breakdown is by tenant, by ``write_mode`` and by parent provenance on
purpose. One number over a mixed corpus is what went wrong on the sibling row.

PROVENANCE TRAP, and it bit the earlier measurement: fan-out children do NOT
inherit the parent's ``benchmark`` marker — ``child_meta`` is built fresh as
``{parent_memory_id, source, retrieval_hint}`` plus governance signals. A
provenance filter keyed on the CHILD's own metadata classifies every one of them
as real-user content. This script joins through the parent instead.
"""

import argparse
import subprocess

# Both halves of the marker, spelled once. ``_system`` is checked as well as the
# top level because ``extract_system_metadata`` merges the two on read, so a row
# written through ``set_system_value`` would be invisible to a top-level-only
# probe. Neither writer does that today; the fallback costs nothing and stops
# this query from silently under-counting if one ever does.
PARENT_KEY = (
    "coalesce(metadata->>'parent_memory_id', metadata->'_system'->>'parent_memory_id')"
)
SRC = "metadata->>'source'"
DERIVED = f"({PARENT_KEY} is not null and {SRC} = 'atomic_fact_fanout')"
CHUNK = f"({PARENT_KEY} is not null and {SRC} = 'auto_chunk')"
ORPHAN = f"({PARENT_KEY} is not null and {SRC} is distinct from 'atomic_fact_fanout' and {SRC} is distinct from 'auto_chunk')"

QUERY = f"""
\\echo '=== 1. Store composition. ORPHAN rows carry a parent link with neither known source —'
\\echo '===    a naive parent_memory_id filter would catch them and this query would not have.'
select case when {DERIVED} then 'fanout child (the population in question)'
            when {CHUNK}   then 'auto-chunk child (NOT redundant - see the doc)'
            when {ORPHAN}  then 'ORPHAN: parent link, unrecognised source'
            else 'ordinary row' end                        as kind,
       count(*)                                            as rows,
       round(100.0 * count(*) / sum(count(*)) over (), 2)  as pct_of_store,
       count(*) filter (where source_uri is null)          as source_uri_null,
       count(*) filter (where embedding is null)           as unembedded,
       round(avg(length(content)))                         as avg_len,
       min(created_at)::date as first, max(created_at)::date as last
from memories
where deleted_at is null and jsonb_typeof(metadata::jsonb) = 'object'
group by 1 order by rows desc;

\\echo ''
\\echo '=== 2. Provenance and write_mode of the PARENTS. Children carry no benchmark marker of'
\\echo '===    their own, so asking the child is how you get told synthetic data is real traffic.'
with kids as (select distinct ({PARENT_KEY})::uuid as pid from memories where deleted_at is null and {DERIVED})
select (p.metadata->>'benchmark' is not null)                            as parent_is_benchmark,
       coalesce(p.metadata->>'write_mode',
                p.metadata->'_system'->>'write_mode', '(unset)')         as parent_write_mode,
       count(*)                                                          as parents
from kids join memories p on p.id = kids.pid
group by 1, 2 order by parents desc;

\\echo ''
\\echo '=== 3. Per-tenant share. This is the number that bounds what a top_k can contain:'
\\echo '===    with a small store, derived share OF RETURNS is just derived share OF THE STORE.'
with d as (select tenant_id, count(*) n from memories where deleted_at is null and {DERIVED} group by 1),
     t as (select tenant_id, count(*) n from memories where deleted_at is null group by 1)
select t.tenant_id, t.n as tenant_rows, d.n as derived,
       round(100.0 * d.n / t.n, 1) as derived_pct,
       t.n - d.n as non_derived
from t join d using (tenant_id) order by d.n desc limit 20;

\\echo ''
\\echo '=== 4. TOP_K HEADROOM — the fact that decides whether a server-side filter PROMOTES'
\\echo '===    better rows or merely returns a shorter list. Below top_k there is nothing to'
\\echo '===    promote: the query already returns the whole store.'
with d as (select tenant_id, count(*) n from memories where deleted_at is null and {DERIVED} group by 1),
     t as (select tenant_id, count(*) n from memories where deleted_at is null group by 1),
     j as (select t.tenant_id, t.n as total, d.n as derived from t join d using (tenant_id))
select count(*)                                              as tenants_with_fanout,
       round(avg(total))                                     as avg_tenant_rows,
       round(avg(total - derived))                           as avg_non_derived,
       count(*) filter (where total - derived < 50)          as under_a_top_k_of_50,
       round(avg(100.0 * derived / total), 1)                as avg_derived_pct
from j;
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "dsn", nargs="?", help="Postgres DSN. Omit to use the local docker stack."
    )
    ap.add_argument(
        "--db",
        default="caura",
        help="Database name when using the local docker stack (default: caura).",
    )
    args = ap.parse_args()

    # ``-f -`` rather than ``-c``: the query carries ``\\echo`` meta-commands that
    # label each of the four results, and psql parses those only when the script
    # arrives as a FILE. Under ``-c`` the whole string goes to the server, which
    # answers with a syntax error on the first backslash.
    if args.dsn:
        cmd = ["psql", args.dsn, "-v", "ON_ERROR_STOP=1", "-f", "-"]
    else:
        cmd = [
            "docker",
            "exec",
            "-i",
            "caura-memclaw-enterprise-postgres-1",  # legacy-name-floor: the running container bears this name
            "psql",
            "-U",
            "caura",
            "-d",
            args.db,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ]
    print(
        "Derived-row population (pm-0918-c-03). Read the findings doc before quoting a number.\n"
    )
    return subprocess.run(cmd, input=QUERY, text=True).returncode


if __name__ == "__main__":
    raise SystemExit(main())
