# Runbook: `ddp_bill_search` and the `/ddp/search/*` routes

OPEN-310. Design: `ddp-infra/PLAN-enterprise-search.md` sections 4.5 (index, refresh, deployment) and
10.1 (rollout order, rollback, stop conditions). Code: `api/search_projection.py` (OPEN-308) and
`api/ddp_search.py` (OPEN-309).

`ddp_bill_search` is a derived table (one row per bill) that only the `/ddp/search/*` routes read. It is
rebuilt from `opencivicdata_bill`, `opencivicdata_billabstract` and `ddp_bill_version_document`. Nothing
upstream reads or writes it, so dropping it loses nothing that cannot be rebuilt.

## 1. Order of operations (merge and deploy)

1. OPEN-308 (table and refresher) and OPEN-309 (routes) are merged; this runbook follows them.
2. Rebuild and redeploy the `api` image on the instance that serves production (section 4), from a revision
   that contains the merged OPEN-308 AND OPEN-309 code. Check on the running container:
   `docker exec ddp-openstates-api-1 python -c "import api.ddp_search, api.search_projection"` must exit 0.
   Until it does, no step below can run.
3. Run `ensure` (section 3), then the first build (section 5), then check `GET /ddp/search/coverage`.
4. Only then enable the `ddp-sync` hook and (later) the broker flags (plan 10.1 steps 3 to 6).

## 2. Two instances

The RDS-backed `api-v3` (broker host, `deploy/docker-compose.rds.yml`) and the Mac Studio's (`:8002`,
`deploy/docker-compose.ddp.yml`) each build their own table from their own database. Either can serve
search: since OPEN-312 the Mac replica's publication carries 47 tables, including abstracts and people,
so the earlier statement that the Mac subscriber lacks them (plan 4.5.6, written when the publication
carried 7 tables) no longer holds. The RDS-backed one remains the default because it is authoritative
(the replica trails by seconds and lost its connection for about 3h50m on 2026-09-27) and because
building on the Mac writes a table into the database LegBot reads. (An earlier version of this paragraph
named a gap: 0 NC people. Closed 2026-10-22, when the prod agent loaded NC's legislators into RDS; on
2026-10-25 the Mac api-v3 and the production api-v3 both return 173 NC people.)
Three things must name the SAME instance, or search silently reads a stale or incomplete table:

- where `ensure` and the first build were run,
- what `ddp-sync`'s `local_openstates_api_base` points at (the refresh target),
- what `ddp-api`'s `OPENSTATES_SERVICE_URL` / the broker's `DDP_OPENSTATES_API_ROOT` resolves to.

## 3. `ensure` (create `pg_trgm`, the table and its indexes)

```bash
docker exec ddp-openstates-api-1 python -m api.search_projection ensure
# => === BILL SEARCH ENSURE: ddp_bill_search present ===
```

Idempotent (`CREATE ... IF NOT EXISTS`), about half a second, safe to repeat. It raises if a table with the
right name exists but lacks a required column or index, which is the signal to add an `ALTER` to
`DDL_STATEMENTS`. `POST /ddp/search/refresh` also calls it first, so a missed `ensure` self-heals on the
first refresh, but run it explicitly so a permissions problem surfaces at deploy time and not during a
scheduled refresh.

### Mac boot script: deliberately NOT wired yet

Plan 4.5.4 step 2 adds this block to `ddp-open-states/start-os-api.sh`, after the `bulk_dataexport` block:

```bash
# PLAN-enterprise-search.md §4.5.4: ddp_bill_search (and pg_trgm) back the /ddp/search/* endpoints.
# Idempotent (CREATE ... IF NOT EXISTS), like the bulk_dataexport step above.
if ! docker exec ddp-openstates-api-1 python -m api.search_projection ensure >>"$LOG" 2>&1; then
    log "ERROR: failed to ensure ddp_bill_search exists"
    slack_fail ":red_circle: openstates api-v3 could not create ddp_bill_search at boot — /ddp/search/* will 500 — check logs/os-api.log"
    exit 1
fi
log "ddp_bill_search present (created if missing)"
```

It is not part of this repo (`start-os-api.sh` belongs to `ddp-open-states`, whose production checkout is
not edited in place), and it is unsafe to add before the image is rebuilt: on the current image the module
does not exist, the block fails, the script `exit 1`s and posts a Slack alert on every boot, and the
smoke tests after it never run. Add it in a `ddp-open-states` PR, merged only AFTER the Mac `api` image
has been rebuilt and that import check passes. The RDS-backed instance has no boot hook (plan 4.5.4 step 3):
run the one-liner above by hand after each image rebuild that changes the schema.

## 4. Which instance serves production (recorded 2026-09-30)

**The RDS-backed api-v3 on the broker host** (`ddp-openstates-api-1`, `docker-compose.rds.yml`, port 8002).
`ddp_bill_search` exists only on that instance. Evidence, all read-only:

- `coverage` requested through ddp-api returns exactly what that instance returns (FL: 7,685 bills, 7,685
  projected, 539 people), and no other instance has the table.
- The broker's `DDP_OPENSTATES_API_ROOT` and ddp-sync's `RDS_OPENSTATES_API_BASE` on that host both name it.
- **Not read directly:** ddp-api's `OPENSTATES_SERVICE_URL` (inferred from the first bullet).
- The Mac's ddp-sync `local_openstates_api_base` points at the Mac's own api-v3, which has no
  `ddp_bill_search` and is unrelated to this path. **Whatever calls `POST /ddp/search/refresh` later (SYNC-87)
  must target the RDS-backed instance, not that setting.**

Internal addresses are deliberately not written here (this repository is public); they are in the private
ops notes.

## 5. First build, refresh, repair

```bash
docker exec ddp-openstates-api-1 python -m api.search_projection refresh --dry-run   # would_refresh=<n>
docker exec ddp-openstates-api-1 python -m api.search_projection refresh             # first build, all jurisdictions
docker exec ddp-openstates-api-1 python -m api.search_projection refresh --jurisdiction fl [--full]
```

- The scheduled path is `POST /ddp/search/refresh?jurisdiction=<abbr>` (driven by `ddp-sync` after each
  archive run). One call does at most about 20 s of work; the caller repeats while `more` is true. A
  second call for the same jurisdiction while one runs returns `busy: true`.
- The stale predicate is the cursor: an interrupted build simply resumes.
- `--full` marks every row (or one jurisdiction's) stale first; use it after any writer that changes an
  input without touching `opencivicdata_bill.updated_at`.
- Verify completeness (the OPEN-310 done condition):
  `GET /ddp/search/coverage?jurisdiction=US&jurisdiction=FL&...` shows `projected == bills` for every
  enrolled jurisdiction, plus plausible `with_text`, `with_abstract` (only FL and VA have abstracts) and
  `people`.
- Size check:
  `SELECT relname, pg_size_pretty(pg_relation_size(oid)) FROM pg_class WHERE relname LIKE 'ddp_bill_search%';`
  and `SELECT pg_size_pretty(pg_total_relation_size('ddp_bill_search'));`
- Rollback: follow plan 10.1's reverse order. If search is already exposed to users, turn that off FIRST
  (remove the `SearchBox`, then `SEARCH_OPENSTATES_ENABLED = False` on the broker), then set
  `bill_search_refresh.enabled: false` in `ddp-sync` so nothing calls `/refresh` while routes change, and
  leave the table and routes in place. If nothing is exposed yet, disabling the hook alone is enough.
- Recreating the table: `DROP TABLE ddp_bill_search` loses no data (it is derived), but the routes return
  500 (the broker degrades to `partial: true`, plan 4.5.8) until `ensure` and a full refresh finish. Do it
  with the `ddp-sync` hook disabled, then run section 3, section 5's first build, and the coverage check.

## 6. Measurements on the local test database (2026-09-30)

(The production figures differ, notably the first build: see section 7.)

Setup: local Docker Postgres 16 (`openstates` database, an older copy of production, 75,805 bills in 10
jurisdictions with data to 2026-09-01; the 8 enrolled when this was measured (2026-09) were US, FL, MI, AZ, VA, WA, UT, NC, and MA and AL
were also present locally but not enrolled (the current list is `ddp-open-states/jurisdictions.yaml`; production's was 7, without NC, on 2026-10-22), 199,302 archived documents holding 3.02 billion characters of full-length text, the largest
7,037,662 characters, `shared_buffers` 128 MB), the api-v3 code from this branch on its own port with
`apikey_auth` overridden (so auth and the `ddp-api` hop are NOT in these numbers), one uvicorn worker, requests
over localhost HTTP. The same Postgres server also hosts the replica database that serves LegBot, so
these are not lab-quiet numbers.

| Measurement | Result |
|---|---|
| `ensure` on an empty database (creates `pg_trgm` 1.6, table, 4 indexes) | 0.5 s; repeat 0.5 s |
| Full build, CLI (500-bill batches) | 147.8 s; 75,805 rows; 73,951 with archived text |
| Full build, `POST /ddp/search/refresh` per jurisdiction, default `limit=200` (how `ddp-sync` calls it) | 169.3 s in 14 calls; longest call 20.7 s; US alone 37,809 rows in 89.7 s |
| Second run with nothing changed | CLI 1.2 to 1.3 s, 0 rows; `POST` for one jurisdiction 173 ms; `POST` for all 1.7 s |
| Table sizes | heap 48 MB, TOAST 356 MB (the tsvectors), total with indexes 509 MB (source document table: 2.3 GB) |
| Index sizes | fts GIN 75 MB, title trigram GIN 19 MB, primary key 5.7 MB, identifier 1.8 MB, jurisdiction 0.9 MB |
| Worst single refresh statement tried (maximum observed over the cases listed, not over every possible batch) | 0.09 s for the 7.0 M-character bill (text capped at 1 M); 0.37 s for the 10 largest bills; 0.62 s for the 200 largest bills as one batch (33 statements, 173.6 M characters of largest documents, 27.0 s over 2 calls because the 20 s budget stops the first); 0.42 s for 500 random bills. Nowhere near the 30 s statement timeout or the 60 s client timeout, so `MAX_CHARS_PER_STATEMENT` needs no lowering. |
| `GET /ddp/search`, 8 enrolled jurisdictions, 65 judged queries x 4 timed passes = 260 requests, one client at a time, warm (a first pass was discarded; it read p50 38 ms, p95 125 ms, so caching is not the story) | p50 37 ms, p95 116 ms (`limit=100`: p50 39, p95 115) |
| `GET /ddp/search/suggest`, same queries | p50 34 ms, p95 83 ms; per-keystroke prefixes of 4 strings (180 requests): p50 47 ms, p95 111 ms |
| `GET /ddp/search/hydrate`, 50 ids (bills and people) | p50 5.1 ms, p95 5.7 ms, 50 of 50 returned |
| `GET /ddp/search/coverage`, 8 jurisdictions | p50 419 ms (an operator route, not on the request path) |
| Coverage after the build | `projected == bills` for all 10 local jurisdictions (75,805), including MA and AL. Enrolled 8: US 37,809 (text 37,757, abstracts 0, people 725), FL 7,685 (7,684, 7,685, 539), VA 4,380 (4,380, 4,380, 356), MI 4,013 (4,013, 0, 450), WA 3,411 (3,411, 0, 347), NC 2,338 (2,338, 0, 508), AZ 2,190 (2,190, 0, 351), UT 1,021 (1,021, 0, 271) |

Known slow shapes (measured, not fixed here; tuning belongs with the judged set, plan section 9):

- `GET /ddp/search?q=S 1` takes about 1.25 s every time. The token `1` matches a large share of all
  documents and `ts_rank_cd` ranks every one of them (about 0.5 s in SQL alone).
- `suggest` for very short inputs with few trigrams (`Med`, `S 1`) takes about 150 ms, because the title
  trigram index returns 5,000 to 9,000 candidates. That is already at the plan's 150 ms bar before the
  `ddp-api` hop is added, so the production hop measurement below matters.

Importer freshness (plan 4.5.3), read from code, not yet observed live: abstracts are in
`BillImporter.related_models`, `import_item` sets `what = "update"` when any related model changed and
then calls `obj.save()`, and `Bill.updated_at` is `auto_now=True`. So an abstract change advances
`Bill.updated_at`, which is what marks the projection row stale. The live confirmation is in section 7.

## 7. Production rollout and results (2026-09-30, RDS-backed instance)

Run on 2026-09-30 by the production agent and the user. Times UTC. Stop conditions are from plan 10.1.

| # | Step | Result |
|---|---|---|
| 1 | Rebuild and redeploy `api` from `main` | **Done** about 22:51. api-v3 `ce1447c`, image built on the host (x86_64). Rollback tag `ddp-openstates-api:pre-open308` kept. Redis and the broker containers were not restarted. |
| 2 | `ensure` | **Done.** `pg_trgm` 1.6 installed, table plus 5 indexes (4 plus the primary key). Publication still `puballtables = false`, 47 tables, `ddp_bill_search` not in it; replica still streaming. |
| 3 | First build, timed | **Done.** 76,909 rows (74,777 with text) in **1,576 s (about 26 min)**, about 10 times the local figure of 148 s (attributed to network latency to RDS; not confirmed with RDS metrics). Table 502 MB (fts index 76 MB, title index 19 MB). Replica lag stayed between 0.10 and 0.44 s the whole time. A second run changed 0 rows. |
| 4 | Coverage: `projected == bills` | **Done, met for all 8.** US 38,605; FL 7,685; VA 4,382; MI 4,107; WA 3,411; NC 2,338; AZ 2,190; UT 1,021. `with_abstract` nonzero for FL (7,685) and VA (4,382) only. `people` nonzero except **NC = 0** (see below). |
| 5 | Record the serving instance | **Done by inference**, section 4. |
| 6 | Authorisation through the real ddp-api | **Pass.** `POST .../refresh` with no token: 401. With the read-scope token: 403 "Write access required", and api-v3's log shows it never reached api-v3. `GET` search with the read token: 200. Bad token: 403 (not 401; rejected either way). `limit=101`: 422. api-v3 itself: no security group opens port 8002 and the last 24 hours of logs show only localhost, the Docker gateway and the Mac over WireGuard. **Not checked:** host firewall, NACLs, other WireGuard peers. api-v3 keys have no scopes, so any peer holding a valid key could call its refresh directly. |
| 7 | ddp-api hop latency (bar: `suggest` p95 at or under 150 ms) | **Fail, override recorded.** All 65 judged-set queries, two passes, one sequential client: through ddp-api p95 **348 ms** warm (764 ms cold); direct to api-v3 197 ms warm; the hop adds about 120 ms at the median. Slowest were `S 1` and `school lun`, the known slow shapes. Caveats: the judged set has no jurisdiction field, so every query searched all 8 states (worst case), and the numbers include the network to RDS. **The user told the production agent to accept this and move on, which overrides the stop condition rather than satisfying it. Plan 4.5.2 has not been revisited and Ramon has not confirmed.** |
| 8 | Importer advances `Bill.updated_at` on an abstract change | **Not done.** Needs a non-production database. Read from code only (section 6). |
| 9 | `start-os-api.sh` ensure block; refresh hook | **Not done.** The boot block waits until the Mac `api` image is rebuilt and the import check passes there. **Nothing refreshes `ddp_bill_search` yet:** no caller exists until SYNC-87 (or a scheduled call) is built, so new or changed bills will not appear until someone runs `refresh`. A no-op sweep costs about 26 s, so per-jurisdiction refreshes are cheaper. |

**NC people gap (as found on deploy day; since closed).** RDS had 0 people for North Carolina (4,088 in total, the same as the Mac replica). The Mac's
own scrape database had 508 NC people that were never loaded into RDS, so legislator-name search found nothing
for NC. Bill search for NC was unaffected. **Closed 2026-10-22:** the prod agent loaded NC's legislators into RDS
(BROKER-302 flip 7, step D); on 2026-10-25 both api-v3 instances return 173 NC people. Re-run the `people` coverage
column before relying on this on a database you have not checked.

**Not measured on RDS:** the worst-case refresh statement with full-length documents, and RDS free storage
(the build completed, so there was enough; the margin is unknown).

## 8. OPEN-457: a bill's other titles (api-v3 PR #39, merged 2026-10-30): rollout order, rollback, dev results

**What changed.** `ddp_bill_search` has a new column `alt_titles` (a bill's other titles from `opencivicdata_billtitle`, one per
line, never the official title) and an index `ddp_bill_search_alt_titles_idx`. The other titles are in the full-text vector at the
title weight, so `/ddp/search` finds a bill by them. For a query of two or more words that is not a bill number, a bill whose title
or other title contains the typed words in order, as whole words, ranks ahead of bills that only share some of them (the shortest
such title first). A single word or a bill number runs the same SQL as before. The `title` returned is unchanged: the broker
decides the display title (BROKER-355, `Bill.catalogue_title`).

**Rollout order matters (a new code image on an old table fails every search).** `ensure` runs only on `refresh`, never at boot,
and the new code reads `alt_titles`; until the column exists, every `/ddp/search` and `/ddp/search/suggest` call returns 500 and
the broker degrades to `partial: true`. Do the steps in this order, on the instance that serves the traffic (section 4):

1. Build the new `api` image from a revision that contains PR #39 (merge commit `b9d2f610` or later). Nothing is running new code yet.
2. **`ensure` from the new image, before recreating the container.** It adds the column and the index (`ADD COLUMN IF NOT EXISTS
   ... DEFAULT ''`, `CREATE INDEX IF NOT EXISTS`); old code ignores the extra column, so the running container is unaffected.
   Mac dev stack, as it was run on 2026-10-30:
   `cd deploy && docker-compose -f docker-compose.ddp.yml run --rm --no-deps api python -m api.search_projection ensure`
   (expect `=== BILL SEARCH ENSURE: ddp_bill_search present ===`). The production host's `deploy/docker-compose.rds.yml` form of the
   same command has **not** been read from the host: the prod agent adapts it (always `--no-deps`, always the `api` service).
   **Gate before step 3 (stop here if either query is empty):** on the same database the `api` container uses (read its
   `DATABASE_URL`, do not assume), `SELECT current_database();` names the expected database, and
   `SELECT column_name FROM information_schema.columns WHERE table_name='ddp_bill_search' AND column_name='alt_titles';` returns a row and
   `SELECT indexrelid::regclass FROM pg_index WHERE indexrelid = 'ddp_bill_search_alt_titles_idx'::regclass AND indisvalid;` returns the index.
   Locks: `ensure` creates the index without `CONCURRENTLY`, so writes to `ddp_bill_search` wait while it builds (reads do not); the
   column is empty, so on the Mac it took seconds. Run it when no refresh is running.
3. Recreate only the `api` container (Mac: `./refresh-api-v3.sh`, which does not run `ensure` and so must come after step 2; the
   host: `docker compose -f deploy/docker-compose.rds.yml up -d --force-recreate --no-deps api`). Check `GET /healthz`, then **smoke-test
   before starting step 4**: `GET /ddp/search?q=One+Big+Beautiful+Bill` and `GET /ddp/search/suggest?q=One+Big` (with the read key) must answer
   200. **Stop condition:** any 500 here means the table lacks the column; recreate the previous image at once and redo the gate.
4. **`refresh --full`** so every row gets its other titles: `docker exec ddp-openstates-api-1 python -m api.search_projection refresh --full`.
   Until it finishes, `alt_titles` is `''` for rows not yet rebuilt (search works, those bills just cannot be found by another title);
   the first build on the RDS-backed instance took about 26 minutes on 2026-09-30 (section 7) against about 3 minutes on the Mac, so
   allow that long. A refresh is not one transaction: each call upserts a batch of rows (about 20 s of work), every row is
   replaced in place, and the stale predicate is the cursor, so a build that is interrupted (a restart, a lost connection) loses nothing
   and **the same command is simply run again**; a bill is complete or still stale, never half-written. The refresh lock is per jurisdiction (an advisory lock shared by the CLI and the HTTP route): if the `ddp-sync`
   hook is on, its call for a jurisdiction being rebuilt answers `busy: true` and the hook retries, so the two do not collide.
5. Check, and record **host, value and time** on the epic (the production evidence rule): `GET /ddp/search/coverage` shows
   `projected == bills`; `SELECT count(*) FILTER (WHERE alt_titles <> ''), count(*) FROM ddp_bill_search;` (Mac, 2026-10-30: 29,820 of 77,649, about 38%;
   production should be of the same order since both read the same source; a count near 0 means step 4 did not finish, run it again); a search for "One Big Beautiful Bill" returns HR 1 (US 119) first, and "FEHB Protection Act" returns HR 1 in the top five;
   time the two multi-word queries against the section 6 numbers.

**Rollback (an image rollback, not a data rollback).** Re-tag and recreate the previous image (keep a `pre-open457` tag, as `pre-open308` was
kept). The column, the index and the filled `alt_titles` stay and are harmless to old code. **It does not restore the table as it was before
step 4:** after `refresh --full` the full-text vector of every row includes the other titles, so old code on the rebuilt table also finds bills by
them (it just does not rank a whole-phrase match first), and the table is up to date, so whatever the refresh changed in exact-number ordering stays
(see the results below). No copy of the old table exists unless one is taken first (`pg_dump -t ddp_bill_search` before step 4, if wanted); the table
is derived, so nothing else is lost. Nothing needs dropping.

**Dev results (the Mac stack, 2026-10-30, dev only; nothing here is a production fact).** api-v3 `f9dde91` to `b9d2f61`; `ensure`, recreate,
`refresh --full`: **77,649 rows in 186.5 s, 29,820 with other titles** (the table had 76,953 rows before: it was also about 700 bills behind).
The judged set (BROKER-162, 85 queries, `python manage.py run_search_eval --base-url ...` through the dev broker):

| Run | exact success | recall@10 | nDCG@10 | latency p50 / p95 |
|---|---|---|---|---|
| Baseline: old code, old table | 65.0% | 84.6% | 80.4% | 92 / 663 ms (cold first run) |
| New code, old table | 65.0% | 85.2% | 81.0% | n/a |
| New code, rebuilt table | 55.0% | 84.3% | 80.1% | 96 / 197 ms |

- Old code and new code on the **same rebuilt table score identically on every one of the 85 queries** (compared query by query with a
  temporary old-code instance on another port, since removed). The ranking change does not move the judged set.
- Exact success 65% to 55% is 13 to 11 of the 20 exact queries, so **two queries changed from success to failure** between the old table and the
  rebuilt one. **Nine of the 20 now fail, the same nine on the old code and on the new code, on the rebuilt table:** `S 850` (four spellings), `HR 1`,
  `H.R. 1`, `SB 850`, `S 1`, `HR 10284`. Which two flipped cannot be shown row by row (the old table was overwritten by the rebuild; the old-table
  run was recorded only as totals). The conclusion the evidence supports: **it is not PR #39**, because old and new code agree on every query on the
  same table, and `EXACT_SQL` orders bills that share a number by `latest_action_date` and never reads `alt_titles`. The likely cause is the rebuild
  bringing the table up to date (263 bills created and 3,327 updated in the previous 14 days; for example NC `HR 1` of session `2025E1`, created
  2026-10-19, now outranks the Utah and Virginia `HR 1` that the judged set expects first): likely, not proven. The judged set's exact expectations are
  tied to the data of the day it was judged (re-judge: BROKER-162; Ramon's call).
- Recall and nDCG move by 0.3 points against the baseline (within what fresher data explains); concept queries 16.3% to 18.0% recall.
- Run matrix: old code on the old table (totals above), new code on the old table (totals above), old code and new code on the rebuilt table (compared
  query by query: identical on all 85; the new-code totals above are therefore also the old-code totals on that table). Not measured: production
  timings, search latency and errors during the refresh on production, the refresh duration on RDS for this change.
