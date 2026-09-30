# Runbook: `ddp_bill_search` and the `/ddp/search/*` routes

OPEN-310. Design: `ddp-infra/PLAN-enterprise-search.md` sections 4.5 (index, refresh, deployment) and
10.1 (rollout order, rollback, stop conditions). Code: `api/search_projection.py` (OPEN-308) and
`api/ddp_search.py` (OPEN-309).

`ddp_bill_search` is a derived table (one row per bill) that only the `/ddp/search/*` routes read. It is
rebuilt from `opencivicdata_bill`, `opencivicdata_billabstract` and `ddp_bill_version_document`. Nothing
upstream reads or writes it, so dropping it loses nothing that cannot be rebuilt.

## 1. Order of operations (merge and deploy)

1. Merge OPEN-309 (routes) before OPEN-310 (this runbook; the PR is stacked on it).
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
building on the Mac writes a table into the database LegBot reads. Known gap on both: the Mac replica has
0 NC people (the Mac's scrape database has 508); check RDS before relying on NC legislator search.
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

## 4. Which instance serves production (record it here when known)

Not resolved in the repos (plan 4.5.7). Record: the host and container that answers
`OPENSTATES_SERVICE_URL` for `ddp-api`: ______; `local_openstates_api_base` in `ddp-sync`: ______;
`DDP_OPENSTATES_API_ROOT` for the broker: ______. All three must match the instance you ran `ensure` on.

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

Setup: local Docker Postgres 16 (`openstates` database, an older copy of production, 75,805 bills in 10
jurisdictions with data to 2026-09-01; the 8 enrolled ones are US, FL, MI, AZ, VA, WA, UT, NC, and MA and AL
are also present locally but not enrolled, 199,302 archived documents holding 3.02 billion characters of full-length text, the largest
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

## 7. Production checklist for Ramon (NOT done by OPEN-310's PR)

Nothing below has been run. Record each result on OPEN-310. Stop conditions are from plan 10.1.

1. [ ] Rebuild and redeploy the `api` image on the serving instance so it contains `api/ddp_search.py`.
2. [ ] `ensure` on the RDS-backed instance (section 3). Expect `ddp_bill_search present`. `pg_trgm` is a
       trusted extension and DDP controls this instance (Ramon, 2026-09-30), so a permission failure would
       be a surprise; record that it worked. (The stop condition for `pg_trgm` still applies to the BROKER
       database's migration 0064 under BROKER-163, which is a different host and role.)
3. [ ] First build on that instance (section 5), timed. Record seconds and row count. Expect roughly the
       local figure (about 2.5 minutes) on similar hardware; RDS network latency will change it. Stop if any
       single `POST /refresh` call, or any single refresh statement with full-length documents, cannot finish
       inside the request timeout of the path that ddp-sync uses (record that timeout; the total build time
       is informational, because the first build is a loop of bounded calls).
4. [ ] `GET /ddp/search/coverage` for the 8 enrolled jurisdictions shows `projected == bills` (done
       condition) and `with_abstract` is nonzero for FL and VA, `people` nonzero for all except possibly NC (see section 2). Record
       `pg_total_relation_size('ddp_bill_search')` and the index sizes from the size check in section 5.
5. [ ] Record which instance serves production and fill in section 4; assert the three names match.
6. [ ] Authorisation through the real `ddp-api`: an unauthenticated `POST /openstates/ddp/search/refresh` and
       one with a READ-scope token are both rejected (401/403); `GET /openstates/ddp/search` with the read
       token is accepted; the key in `DDP_OPENSTATES_BEARER_TOKEN` is read-scope only. Also confirm
       `api-v3` itself is reachable only from trusted callers (`ddp-api`, `ddp-sync`), because `api-v3`
       keys have no scopes and any valid key can call its `POST /refresh` directly.
7. [ ] `ddp-api` hop latency: from the broker host, time `GET <OPENSTATES_SERVICE_URL>/ddp/search/suggest`
       (via `ddp-api`) for 50 to 100 requests over a mix of judged-set queries (BROKER-162: exact numbers,
       titles, misspellings, short prefixes, names), and record p50 and p95. Bar: full-path `suggest` p95 at or
       under 150 ms, measured through `ddp-api` (not directly against `api-v3`). Above it after `DDPOpenStates` direct mode: stop and revisit plan 4.5.2.
8. [ ] Importer really advances `Bill.updated_at` on an abstract change (section 6 explains why it should):
       on the Mac dev database, change one bill's abstract through a scrape/import, confirm
       `refresh --dry-run` reports `would_refresh=1`. If not, the stale predicate needs another signal.
9. [ ] Only after 1 to 8 pass: add the `start-os-api.sh` block (section 3) in a `ddp-open-states` PR, then
       enable the `ddp-sync` hook (plan 10.1 step 3).
