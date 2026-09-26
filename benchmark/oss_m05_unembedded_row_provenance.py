#!/usr/bin/env python3
"""oss-0924-m-05 — of the rows that never got an embedding, which are REAL?

A store holds N live rows with ``embedding IS NULL``. That number on its own
says nothing about customer impact, and quoting it as though it did is the
mistake this repository has now made twice (pm-0918-c-04 sized a change on a
band that was 99.4% synthetic; pm-0918-c-03 nearly repeated it). This script is
the provenance join that has to happen BEFORE the count is spoken aloud.

Read ``docs/unembedded-rows/oss-m05-unembedded-provenance.md`` before quoting
anything out of it.

    python3 benchmark/oss_m05_unembedded_row_provenance.py "postgresql://user@host:5432/<database>"
    python3 benchmark/oss_m05_unembedded_row_provenance.py              # local docker stack, `caura` database
    python3 benchmark/oss_m05_unembedded_row_provenance.py --db memclaw # legacy-name-floor: the other local database literally bears this name

THREE MARKERS, IN DESCENDING ORDER OF TRUST
-------------------------------------------
No single marker settles this, so the script applies three and reports each
separately rather than collapsing them.

1. ``metadata->>'benchmark'`` on the row itself. Explicit, written by the
   harness, unambiguous. It is also INCOMPLETE — see (2).

2. The same marker on the row's PARENT. This is pm-0918-c-03's trap, and it
   fires here too: fan-out children are built with fresh metadata
   (``{parent_memory_id, source, retrieval_hint}`` plus governance signals) and
   do NOT inherit ``benchmark``. A row-level-only filter therefore reports every
   one of them as real traffic. On the development corpus that single omission
   inflates the apparent real-traffic residual by 78%.

3. ``enterprise.tenants`` — the registry a tenant lands in when it is created
   through signup/provisioning. THIS IS THE MARKER THAT DOES NOT DEPEND ON
   NAMING. A tenant slug is free text chosen by whoever wrote the row, so
   ``tenant_id LIKE 'lme-%'`` is a guess; presence in the registry is a fact
   about how the credential was issued. A tenant holding memories while absent
   from the registry was invented by a harness pointing at the store directly,
   and cannot have a customer behind it. The query also prints the registered
   tenants' email domains, because "registered" and "real" are still not the
   same thing on a development box.

Section 5 answers the question the count cannot: is this STILL HAPPENING. There
are two different ways to be fooled into calling it fixed, and the section is
built to catch both.

  * The store stopped. "Nothing un-embedded in the last N days" means the
    failure stopped only if the store has rows from the last N days. Section 1
    prints ``unembedded_newest`` beside ``store_newest`` for exactly this.
  * The TRAFFIC stopped. Rows land un-embedded through the deferred-embed path,
    so a tail of days with no fast-mode writes in them has observed nothing at
    all. Section 5 therefore prints ``fast_writes`` as the denominator next to
    the zeroes. A run of clean days carrying two-figure fast volume, after days
    carrying five-figure fast volume at a 1% failure rate, is not evidence of
    anything; the arithmetic says such a tail would be expected to look clean
    whether or not the defect is still live.

Run it against every database you have, not the biggest one. A retired
snapshot and the live store disagree about the end date, and the retired one is
the one whose quiet tail is most persuasive and least meaningful — compare
``alembic_version`` across them before believing either.
"""

import argparse
import subprocess

# Both halves of the parent link, spelled once, for the same reason
# ``pm_c03_derived_row_population.py`` does it: ``extract_system_metadata``
# merges top-level and ``_system`` on read, so a top-level-only probe would
# silently under-count if a writer ever moved the key. ``nullif`` guards the
# cast — an empty string is not a uuid and would abort the whole script.
PARENT_KEY = (
    "nullif(coalesce(m.metadata->>'parent_memory_id',"
    " m.metadata->'_system'->>'parent_memory_id'), '')::uuid"
)

# The classifier, once. Order matters: a row carrying its own marker is settled
# by (1) and never reaches the parent join, which keeps the classes disjoint.
CLASSIFY = f"""
select m.id, m.tenant_id, m.created_at, m.metadata,
       case
         when m.metadata->>'benchmark' is not null then 'benchmark: own marker'
         when p.metadata->>'benchmark' is not null then 'benchmark: via fan-out parent'
         else 'residual: no benchmark provenance'
       end as provenance
from memories m
left join memories p on p.id = {PARENT_KEY}
where m.deleted_at is null and m.embedding is null
"""

QUERY = f"""
\\echo '=== 1. THE HEADLINE, AND THE TWO NUMBERS THAT BOUND IT.'
\\echo '===    unembedded_newest vs store_newest is section 5 in one line: if they are the'
\\echo '===    same day, the failure did not stop, the CORPUS did.'
select count(*) filter (where embedding is null and deleted_at is null)      as unembedded_live,
       count(distinct tenant_id) filter (where embedding is null
                                           and deleted_at is null)           as tenants,
       min(created_at) filter (where embedding is null
                                 and deleted_at is null)::date               as unembedded_oldest,
       max(created_at) filter (where embedding is null
                                 and deleted_at is null)::date               as unembedded_newest,
       max(created_at)::date                                                 as store_newest,
       count(*) filter (where embedding is null and deleted_at is null
                          and search_vector is not null)                     as unembedded_with_fts,
       -- Which database you are looking at, in the one form that cannot be
       -- argued with. A store several migrations behind the others is a
       -- retired snapshot, and its quiet tail says nothing about today.
       (select version_num from public.alembic_version limit 1)              as schema_version
from memories;

\\echo ''
\\echo '=== 2. THE SPLIT. Class B is the whole point: those rows carry NO marker of their'
\\echo '===    own, and a filter that asked only the row would hand them back as real traffic.'
with c as ({CLASSIFY})
select provenance,
       count(*)                                           as rows,
       count(distinct tenant_id)                          as tenants,
       round(100.0 * count(*) / sum(count(*)) over (), 1) as pct_of_unembedded,
       min(created_at)::date as first, max(created_at)::date as last
from c group by 1 order by rows desc;

\\echo ''
\\echo '=== 3. THE RESIDUAL, CROSS-CHECKED AGAINST THE TENANT REGISTRY — the marker that does'
\\echo '===    not depend on what anyone named anything. A tenant absent from enterprise.tenants'
\\echo '===    was never provisioned; no customer credential points at it. Skipped with a notice'
\\echo '===    when the registry is not in this database (OSS-only deployments).'
\\echo ''
select case when to_regclass('enterprise.tenants') is null
            then 'enterprise.tenants absent — registry cross-check SKIPPED (marker 3 unavailable)'
            else 'enterprise.tenants present — cross-check below is valid' end as registry_status;

\\echo ''
\\echo '--- 3a. registered vs unregistered, over the residual only'
with c as ({CLASSIFY}),
     r as (select * from c where provenance like 'residual%')
select case when to_regclass('enterprise.tenants') is null then '(registry absent)'
            when t.tenant_id is not null then 'REGISTERED (provisioned tenant)'
            else 'unregistered (harness wrote straight to the store)' end as registry,
       count(*) as rows, count(distinct r.tenant_id) as tenants
from r left join enterprise.tenants t on t.tenant_id = r.tenant_id
group by 1 order by rows desc;

\\echo ''
\\echo '--- 3b. every registered tenant and its email domain. "Registered" is not "real":'
\\echo '---     on a development box this list is expected to be entirely example.com.'
select split_part(email, '@', 2) as email_domain, count(*) as tenants,
       min(created_at)::date as first, max(created_at)::date as last
from enterprise.tenants group by 1 order by tenants desc;

\\echo ''
\\echo '--- 3c. the residual tenants themselves, largest first. Read the content column:'
\\echo '---     integration-test exhaust does not look like anything a person wrote. Content is'
\\echo '---     printed ONLY for unregistered tenants, which by construction have no customer'
\\echo '---     behind them; a registered tenant is withheld rather than paged into a terminal.'
with c as ({CLASSIFY}),
     r as (select * from c where provenance like 'residual%')
select r.tenant_id, count(*) as rows,
       (t.tenant_id is not null) as registered,
       min(r.created_at)::date as first, max(r.created_at)::date as last,
       case when t.tenant_id is not null then '(withheld: provisioned tenant)'
            else left(min(mm.content), 44) end as sample_content
from r
join memories mm on mm.id = r.id
left join enterprise.tenants t on t.tenant_id = r.tenant_id
group by 1, 3 order by rows desc limit 25;

\\echo ''
\\echo '=== 4. WHY THEY LANDED WITHOUT A VECTOR. ``embedding_pending`` is the write path'
\\echo '===    saying so out loud (ax-0917-h-06); its ABSENCE on an unembedded row is worse,'
\\echo '===    because nothing was queued and no consumer is waiting.'
with c as ({CLASSIFY})
select coalesce(metadata->>'embedding_pending',
                metadata->'_system'->>'embedding_pending', '(absent)')  as embedding_pending,
       coalesce(metadata->>'write_mode',
                metadata->'_system'->>'write_mode', '(unset)')          as write_mode,
       count(*) filter (where provenance like 'benchmark%')             as benchmark,
       count(*) filter (where provenance like 'residual%')              as residual,
       count(*)                                                         as rows,
       min(created_at)::date as first, max(created_at)::date as last
from c group by 1, 2 order by rows desc;

\\echo ''
\\echo '=== 5. ONGOING OR HISTORICAL — and the column that stops you answering it wrong.'
\\echo '===    A run of zeroes at the bottom of this table looks like a fix and usually is not.'
\\echo '===    ``fast_writes`` is the DENOMINATOR: essentially every row here arrives unembedded'
\\echo '===    through the deferred-embed path, so a quiet tail with no fast writes in it has'
\\echo '===    observed nothing. Compare the zeroes against the fast volume that produced them,'
\\echo '===    and against ``unembedded_pct`` on the last day that had real fast volume.'
select created_at::date                                                as day,
       count(*)                                                        as rows_written,
       count(*) filter (where coalesce(metadata->>'write_mode',
                                       metadata->'_system'->>'write_mode') = 'fast')
                                                                       as fast_writes,
       count(*) filter (where embedding is null)                       as unembedded,
       round(100.0 * count(*) filter (where embedding is null)
             / nullif(count(*), 0), 1)                                 as unembedded_pct,
       round(100.0 * count(*) filter (where embedding is null)
             / nullif(count(*) filter (where coalesce(metadata->>'write_mode',
                                        metadata->'_system'->>'write_mode') = 'fast'), 0), 1)
                                                                       as pct_of_fast
from memories
where deleted_at is null
  and created_at >= (select max(created_at) from memories) - interval '35 days'
group by 1 order by 1;
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

    # ``-f -`` rather than ``-c``: the query carries ``\\echo`` meta-commands
    # that label each section, and psql parses those only when the script
    # arrives as a FILE. Under ``-c`` the whole string goes to the server, which
    # answers with a syntax error on the first backslash.
    #
    # No ``ON_ERROR_STOP``, deliberately, and it is the one place this script
    # differs from its pm-c03 sibling: section 3b reads ``enterprise.tenants``
    # unconditionally, and that schema does not exist in an OSS-only deployment.
    # Section 3 prints a SKIPPED notice for that case; stopping on the first
    # error would throw away sections 4 and 5, which need no registry at all and
    # carry the ongoing-or-historical verdict.
    if args.dsn:
        cmd = ["psql", args.dsn, "-f", "-"]
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
            "-f",
            "-",
        ]
    print(
        "Un-embedded row provenance (oss-0924-m-05). READ-ONLY.\n"
        "A row count is not a customer-impact estimate. Read the findings doc\n"
        "before quoting section 1 without sections 2 and 3.\n"
    )
    return subprocess.run(cmd, input=QUERY, text=True, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
