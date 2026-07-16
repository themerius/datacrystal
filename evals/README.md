# Proving grounds — real-dataset evals

datacrystal's **proving grounds** run real external datasets through the library and report
honest absolute numbers — throughput, latency, peak RSS, and correctness against *known-true*
answers. They are the frontier sensor of the eval loop; see
[`docs/design/EVAL-STRATEGY.md`](../docs/design/EVAL-STRATEGY.md) for the strategy and a run-log
of what each has proven.

**They are NOT unit tests.** They download and ingest tens of MB to multi-GB, so they live
*outside* the fast `pytest` suite and run **on demand**, never in CI. (A real dataset's *shape* —
fan-out, depth, cycles — is distilled into `benchmarks/_gen.py` so the unit/fitness tests stay
mineral-cabinet-fast and toy-free.)

## What is in version control

Only the **scripts** (`proving_grounds/*.py`) and this README. The datasets and the stores they
build are **git-ignored** — see [`.gitignore`](.gitignore): `data/` and `*.store/`. The repo never
carries a big dataset. Fetch the data into `evals/data/` with the commands below; re-running a
proving ground rebuilds its `.store/` from scratch.

## Reproduce

Run from the repo root. Each command downloads one dataset into `evals/data/` (`--create-dirs`
makes the folder on a fresh clone), then runs its proving ground.

### #1 — Gene Ontology · knowledge-graph polyhierarchy · CC-BY 4.0 · ~31 MB

```bash
curl -sL --create-dirs -o evals/data/go-basic.obo \
  https://current.geneontology.org/ontology/go-basic.obo
uv run python evals/proving_grounds/gene_ontology.py
```

### #2 — GLEIF · legal-entity ownership (SOR / org-digital-twin persona) · CC0 1.0 · ~23 MB

```bash
curl -sL --create-dirs -o evals/data/gleif-rr.csv.zip \
  "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/rr/latest.csv"
uv run python evals/proving_grounds/gleif.py
```

Optional Level-1 enrichment + the (multi-GB) vision-scale ingest — drop `gleif-lei2.csv.zip`
next to the RR file and it is streamed in too:

```bash
curl -sL --create-dirs -o evals/data/gleif-lei2.csv.zip \
  "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/lei2/latest.csv"   # ~466 MB
```

### #3 — deps.dev · CYCLIC software-dependency graph (the #29 reproducer) · CC-BY 4.0

Unlike #1/#2 this one **fetches its own data** from the keyless deps.dev REST API (a BFS over a
small npm seed set) and caches every response into `evals/data/` — no manual download, and
re-runs touch no network:

```bash
uv run python evals/proving_grounds/deps_dev.py
```

### #4 — MaStR (German Marktstammdatenregister) · SOR/metadata at vision SCALE · dl-de/by-2.0

The only **local** dataset — the Gesamtdatenexport is portal-download only (no URL). Point
`MASTR_DIR` at the unpacked export directory; tune the run with `MASTR_MAX` (0 = full corpus,
~22 GB / millions) and `MASTR_BATCH` (records/commit, the RAM-vs-batch lever):

```bash
MASTR_DIR=/path/to/Gesamtdatenexport_* MASTR_MAX=500000 \
  uv run python evals/proving_grounds/mastr.py   # quick (a few hundred k)
MASTR_DIR=/path/to/Gesamtdatenexport_* \
  uv run python evals/proving_grounds/mastr.py   # full corpus (tens of GB, minutes)
```

### #5 — BEIR / MIRACL · full-text search with a RELEVANCE oracle · CC-BY-SA / Apache-2.0

The first ground with real relevance judgments (qrels), so it measures **ranking quality**
(nDCG@10 / precision@k / nDCG against human judgments), not just throughput. Needs the `fts`
extra. Default = BEIR NFCorpus (tiny, English, densely judged):

```bash
curl -sL --create-dirs -o evals/data/nfcorpus.zip \
  https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip
(cd evals/data && unzip -o nfcorpus.zip)
uv run --extra fts python evals/proving_grounds/search.py
# German Snowball stemming + scale (BEIR-formatted MIRACL-de dir):
# SEARCH_DIR=miracl-de SEARCH_LANG=german QRELS=dev uv run --extra fts python evals/proving_grounds/search.py
```

### #6 — Blob store · real PDFs/documents (enterprise-search + SOR-archive persona) · local

Like MaStR, this one is **local-first** — point `BLOB_DIR` at a directory of documents you have
(the persona is literally "your invoice/scan archive"). It proves the two blob claims (ADR-007):
the object table stays flat no matter how many GB of PDF you store, and streamed write/read keep
peak RSS far below the bytes. Correctness oracle: every blob's sha256 round-trips.

```bash
BLOB_DIR=/path/to/your/pdfs uv run python evals/proving_grounds/blob_store.py
# knobs: BLOB_GLOB='**/*.pdf' (default) · BLOB_MAX=0 (all) · BLOB_CHUNK=1048576
```

No corpus handy? Any folder of files works (PDFs are ideal — multi-MB, real shape). A quick
public-domain set, e.g. a few NASA technical reports (US-gov, public domain):

```bash
mkdir -p evals/data/pdfs && cd evals/data/pdfs
for id in 19950020935 19930091059 20040031234; do
  curl -sL -o "$id.pdf" "https://ntrs.nasa.gov/api/citations/$id/downloads/$id.pdf"
done
cd - && BLOB_DIR=evals/data/pdfs uv run python evals/proving_grounds/blob_store.py
```

### #7 — Web tier · FastAPI + Strawberry over the Gene Ontology polyhierarchy · CC-BY 4.0 · ~31 MB

The `datacrystal[web]` proving ground: a **real** FastAPI REST boundary and a real
Strawberry GraphQL boundary, both over the Gene Ontology graph from #1. The deep
`is_a` polyhierarchy is the point — a nested GraphQL `term → parents → parents → …`
query is exactly the shape that triggers GraphQL's N+1 read amplification, so it is
the honest real-shape proof of the per-request DataLoader (#100) and the #101
op-count gate. Needs the `web` extra (`fastapi`/`strawberry`/`pydantic`) and `httpx`
(the `TestClient` transport).

It reports honest absolutes — a REST list endpoint p50/p99 + throughput, a nested
GraphQL query p50/p99 + throughput — and asserts two correctness oracles:

- **the N+1 oracle** — the store-load COUNT per GraphQL request is `O(depth)`, not
  `O(nodes)`: a depth-`D` ancestor walk that fans out across each level issues
  exactly `D` `get_many` batches (one per relation level), proving the DataLoader
  actually batches on real shape (the property `tests/fitness/test_graphql_no_n_plus_1.py`
  pins on the mineral cabinet, here on the real graph);
- **the zero-Pydantic oracle** — the GraphQL path resolves off frozen `EntityView`s
  via `getattr` and builds **zero** Pydantic models; REST validates one DTO per row,
  so the tax is REST-only.

```bash
curl -sL --create-dirs -o evals/data/go-basic.obo \
  https://current.geneontology.org/ontology/go-basic.obo
uv run --extra web python evals/proving_grounds/web_api.py
```

A FAST self-check (no download) verifies the whole harness — the app boots, REST +
the nested-GraphQL endpoint respond, and **both oracles fire** — over a tiny
synthetic GO-shaped graph (deep `is_a` chains). It runs automatically when the OBO
file is absent, and on demand:

```bash
WEB_SMOKE=1 uv run --extra web python evals/proving_grounds/web_api.py
```

### #8 — Permissions · the real German solar registry, fenced natively · dl-de/by-2.0 · ~830 MB

The ADR-008 proving ground. The Marktstammdatenregister **redacts itself before
publishing**: every solar plant is public, but ~84% of operators are `Natürliche
Person` whose `Firmenname` is legally suppressed (permanently empty in the file),
while the ~16% that are `Organisation` carry name, address, email and phone. The
regulator strips the column because a CSV has no read floor. This eval holds the
*whole* record and enforces the same rule at read time, per principal — then checks
it two ways: an **oracle** (the public's readable operator set must be exactly the
set the regulator published, computed independently from the CSV) and an
**adversarial sieve** driving every per-record read surface from a principal that
holds standing in the group but sits under the read floor. Also ingests the corpus
twice — protected and unprotected twin — for the absolute wall-clock the CI gates
are forbidden to state (invariant 12).

```bash
curl -sL --create-dirs -o evals/data/mastr_solar.csv.zip \
  "https://zenodo.org/api/records/14843222/files/bnetza_mastr_solar_raw.csv.zip/content"
curl -sL --create-dirs -o evals/data/mastr_actors.csv.zip \
  "https://zenodo.org/api/records/14843222/files/bnetza_mastr_market_actors_raw.csv.zip/content"
uv run python evals/proving_grounds/permissions_solar.py
PERM_UNITS=1000000 uv run python evals/proving_grounds/permissions_solar.py  # bigger lane
```

Unlike #4, this lane needs **no portal download** — the open-MaStR Zenodo mirror is a
plain keyless HTTPS GET. `PERM_UNITS` (default 200000) sizes the run.

## Attribution / licenses

- Gene Ontology — CC-BY 4.0, http://geneontology.org
- GLEIF LEI data — CC0 1.0, https://www.gleif.org. Not endorsed by or affiliated with GLEIF.
- deps.dev (Open Source Insights), Google LLC — CC-BY 4.0, https://deps.dev
- MaStR (Marktstammdatenregister, Bundesnetzagentur) — dl-de/by-2.0, https://www.govdata.de/dl-de/by-2-0. Not endorsed by or affiliated with the Bundesnetzagentur. #8 uses the open-MaStR [unboxed] mirror (Zenodo 10.5281/zenodo.14843222) of the same Gesamtdatenexport, under the same licence — attribution: "Marktstammdatenregister — © Bundesnetzagentur | DL-DE-BY-2.0".
- BEIR (NFCorpus etc.) — CC-BY-SA-4.0, https://github.com/beir-cellar/beir. MIRACL — Apache-2.0, https://github.com/project-miracl/miracl.

Both datasets are free to redistribute, but we do **not** commit them — keep them in the
git-ignored `evals/data/`.
