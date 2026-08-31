# Storage architecture

Domain Atlas is meant to hold an inventory that keeps growing — tens of
millions of host names, then hundreds of millions. A schema that is merely
adequate at a million rows becomes unusable at a hundred million, and by then
the data is too large to reshape casually. This document records the shape the
storage has, the measurements behind it, and where the design stops working so
the next decision is made deliberately rather than under pressure.

Every number here was measured on one machine with 300,000 domains and 1.2
million technology pairings, written through the application's own store
rather than a bulk loader, so the figures include the work a real collection
run does.

## Where it stands

| | Before | Now |
|---|---|---|
| Bytes per domain | 852 | 449 |
| Write throughput | 833/s | 4,942/s |
| Technology histogram | 68.7 ms | 0.03 ms |
| Dashboard totals | 99.1 ms | 0.01 ms |
| First page of results | 1.5 ms | 2.1 ms |
| Page 10,000 of results | 1.5 ms | 2.1 ms |
| Filter by technology | 6.1 ms | 3.5 ms |
| Capped count | 4.4 ms | 4.3 ms |

Projected forward, at 449 bytes per domain:

| Domains | Database size | Load time at 4,942/s |
|---|---|---|
| 1 million | 0.45 GB | 3 minutes |
| 10 million | 4.5 GB | 34 minutes |
| 80 million | 36 GB | 4.5 hours |
| 300 million | 135 GB | 17 hours |
| 1 billion | 449 GB | 56 hours |

The old schema reached 68 GB at 80 million and 852 GB at a billion, with a
dashboard that took 26 seconds to draw.

## The four rules

Everything below follows from four properties. They are what make the cost of
an operation independent of how much is stored, and any change that breaks one
of them is a change that will fail at scale.

**Nothing that grows with the table is stored twice.** A name appears once.
Technology names, which repeat across millions of domains, are interned into a
dimension table and referenced by integer.

**No query the interface runs may scan a table.** Listings are paged by
keyset, counts are capped, and totals that would otherwise require a scan are
maintained as rows are written.

**Aggregates are maintained, not computed.** The technology histogram and the
dashboard totals are counters updated in the same transaction as the writes
they summarise, including the decrements a re-check implies.

**Writes touch only what changed.** Batch operations read the rows they may
affect and write only the ones whose value is actually different.

## The schema

```sql
CREATE TABLE domains (
    fingerprint TEXT PRIMARY KEY,   -- the canonical host name
    first_seen  TIMESTAMP,
    checked_at  TIMESTAMP,
    responsive  INTEGER NOT NULL DEFAULT 0,
    is_primary  INTEGER NOT NULL DEFAULT 1,
    site        TEXT,
    status_code INTEGER,
    scheme      TEXT,
    error       TEXT,
    elapsed_ms  INTEGER,
    source      TEXT
);

CREATE TABLE technologies (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE tech_counts  (tech_id INTEGER PRIMARY KEY, domains INTEGER NOT NULL DEFAULT 0);
CREATE TABLE totals       (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);

CREATE TABLE domain_tech (
    domain_id INTEGER NOT NULL,     -- domains.rowid
    tech_id   INTEGER NOT NULL,
    version   TEXT,
    PRIMARY KEY (domain_id, tech_id)
) WITHOUT ROWID;

CREATE TABLE frontier (fingerprint TEXT PRIMARY KEY, origin TEXT, added TIMESTAMP);
```

`fingerprint` stays the natural key. SQLite maintains a row id for the table
regardless, so the integer join key for relations is free; keeping the name as
the primary key means pagination, search and every existing query work on the
column a person actually thinks in.

`domain_tech` is `WITHOUT ROWID`, so the pairing is the row rather than a
pointer to it. Two integers per pairing replaced two full strings stored twice
over, and that one change accounts for most of the size reduction.

`site` and `is_primary` support the grouped listing. A site's representative is
the shortest host name for it, which is always the apex, and the choice is
stable as more variants arrive.

### Where the bytes go now

| | Bytes per domain |
|---|---|
| `domains` table | 108 |
| `idx_domains_primary` | 61 |
| `idx_domains_first_seen` | 60 |
| `domain_tech` | 50 |
| `idx_domain_tech_tech` | 44 |
| primary key index | 37 |
| `idx_domains_site` | 32 |
| `idx_domains_checked_at` | 32 |
| `idx_domains_source` | 16 |
| `idx_domains_responsive` | 10 |
| **Total** | **449** |

## Reading at scale

Pagination is keyset based. A page carries the position of its last row and
the next page continues from it, so page 10,000 costs what page 1 costs.
`OFFSET` makes SQLite walk every skipped row, which turns deep scrolling into
a multi-second stall on a large table; it is not used anywhere.

The two orderings each have an index whose columns match the sort exactly:
`(first_seen, fingerprint)` for the full listing and `(is_primary, first_seen,
fingerprint)` for the grouped one. The predicate uses bare columns — wrapping
one in `COALESCE` to handle nulls made the index unusable and cost two seconds
a page.

Counts stop at a cap. The interface says "20,000+" rather than paying for an
exact answer nobody reads.

Technologies for a page are fetched in one query against the 250 row ids on
screen, which costs 0.7 ms. Storing them denormalised on every row to avoid
that join cost a fifth of the database.

## Writing at scale

Records are buffered and written with `executemany` inside one transaction, in
WAL mode, so readers never block the collector.

Duplicate rejection is two-tier: a bounded LRU of recently seen names in
memory, backed by a batched existence check against the database. The cache is
an optimisation; the primary key is the guarantee.

Interning, counter maintenance and site election all work per batch, and all
of them read the rows they might affect and write only the ones that change.
Site election originally used a window function and rewrote every row of every
site a batch touched — 57% of write time. Reading the members and writing only
the misflagged rows brought the whole write path from 833 to 4,942 rows a
second.

## Migration

The schema version lives in `PRAGMA user_version`. Opening an older database
converts it: pairings are rebuilt against interned names, the duplicated
columns are dropped by rebuilding the table, sites are backfilled, and the
counters are seeded from the rows they summarise. Conversion is verified to
preserve every name and every pairing exactly.

A read-only handle cannot convert anything, so the query layer detects what a
database supports and degrades — an unconverted database still browses, just
without grouping or the histogram, until the collector next opens it.

## Where this design stops

**Up to roughly 100 million domains** the single file is comfortable. 45 GB,
every listing bounded, every aggregate constant.

**Between 100 and 500 million** the file is between 45 and 225 GB. SQLite
still serves it, but three things start to hurt: a full re-check pass takes
days, `VACUUM` needs as much free space again, and backing the file up means
copying all of it. The answer at this size is horizontal partitioning — shard
by a hash of the site across N files, since every query in the application is
either keyed by name (routes to one shard) or an aggregate (sums across
shards). The store interface is the seam; nothing above it knows how many
files there are.

**Beyond 500 million**, or when more than one machine needs to write, a single
embedded database is the wrong tool. Two directions, depending on what
matters:

- *Analytical* — DuckDB or ClickHouse. Column storage compresses this data
  far better than row storage, and the technology histogram becomes a
  column scan. Choose this if the product is about querying the inventory.
- *Transactional* — PostgreSQL with declarative partitioning on `first_seen`,
  plus a partial index per partition. Choose this if several collectors must
  write concurrently, or the inventory has to be served to other systems.

Neither is worth adopting early. Both are reachable because the application
talks to a store interface rather than to SQLite, and because the schema above
is already normalised the way either would want it.

## The next lever, if it is needed

`first_seen` and `checked_at` are ISO text. They appear in three indexes,
which together cost 153 of the 449 bytes per domain. Storing them as integer
unix seconds would cut roughly 60 bytes per domain, about 14%.

It is deliberately not done. It changes the pagination cursor, the `since`
filter, the export format and every timestamp the interface displays, which is
a broad change for a constant factor — and a constant factor is not what
decides whether the architecture survives. Take it when a single file is
genuinely tight and sharding is not yet warranted; the three properties that
matter are already in place either way.
