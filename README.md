# EdgeMind ⚡ Offline-first AI memory for electricity line crews

**Code Cubicle 6.0 · Problem Statement 03 · built on Qdrant Edge + Qdrant Server**

Line crews at power distribution companies (DISCOMs) fix transformers, feeders and poles in places with little
or no mobile signal. The knowledge they need (safety procedures, past fixes, what worked last monsoon) lives in
people's heads, WhatsApp groups and paper registers.

EdgeMind puts a **searchable AI memory on every crew's tablet** using Qdrant Edge. It answers questions with no
network. When the tablet reconnects, it **decides what the rest of the fleet should know**, syncs it through
Qdrant Server using **partial snapshots**, and **flags conflicting information** before someone acts on it.

![Dashboard](docs/dashboard.png)

## What makes it different

| | What happens | Why it matters |
|---|---|---|
| **Qdrant-native edge sync** | Each tablet runs Qdrant's recommended two-shard layout: a mutable shard for its own writes plus a read-only mirror shard per site, restored from a Qdrant Server **snapshot** and refreshed with **partial snapshots** | After 20 new fleet memories a tablet downloads **124 KB instead of 41.8 MB** (0.3%), measured |
| **Fleet-demand sharing** | When crew B searches and finds nothing, the query goes to the fleet. Crew A's tablet notices that its *private* note answers it and offers "Another crew needs this → Share" | Private knowledge flows only when someone needs it, and only with consent |
| **Conflicting-facts detection** | Writing "tightened bushing terminals to 50 Nm" flags the SOP that says 40 Nm, offline | Catches wrong numbers before a lineman uses them |
| **Privacy by policy** | PINs and passwords never leave the tablet; phone numbers and emails reach the fleet only as a redacted copy while the original stays local | Every decision shows its reasons and a score breakdown |
| **Safety-first queue** | Items mentioning shock, snapped lines, fire or leaks upload first after reconnect | The most urgent knowledge reaches others first |
| **Honest conflict handling** | Server-side compare-and-set; two crews editing the same memory offline get *keep mine / keep theirs / merge*; readings use newest-wins; delete vs edit keeps the edit | No silent overwrites |
| **Offline answers** | "Ask" answers from the tablet's own memory with citations, using a local LLM (Ollama) or, without one, an extractive answer | Works in a basement with no signal |

## Measured

Real Qdrant Server 1.19.1 and qdrant-edge-py 0.8, laptop CPU. Reproduce with `python tools/bench.py`.

| Measure | 5,000 memories | 20,000 memories |
|---|---|---|
| Hybrid search on the tablet, p50 / p95 (excluding query embedding) | 0.56 / 0.69 ms | 1.22 / 1.64 ms |
| Full snapshot (first sync of a site) | 10.9 MB | 41.8 MB |
| Partial snapshot after 20 new memories | 120 KB (1.1%) | 124 KB (0.3%) |
| Applying the partial snapshot on the tablet | 215 ms | 228 ms |

**Engineering note.** With default server settings, a partial snapshot after 20 changes was **50%** of the full
shard, because new points land in one large unindexed segment and the whole segment ships. EdgeMind configures
the server collection (`indexing_threshold=500 KB`, `default_segment_number=8`) so bulk data sits in sealed,
indexed segments and new writes go to a small one. That took it from 50% to 0.3%.

## Architecture

```
  Tablet A (process :8001)                  Cloud                              Tablet B (process :8002)
 ┌──────────────────────────┐    ┌───────────────────────────────┐    ┌──────────────────────────┐
 │ local shard  (mutable)   │    │ Sync gateway :8100            │    │ local shard  (mutable)   │
 │ mirror: pune   (snapshot)│◄──►│  · compare-and-set on version │◄──►│ mirror: nagpur (snapshot)│
 │ mirror: global (snapshot)│    │  · server-side sequence nos.  │    │ mirror: global (snapshot)│
 │ outbox · review · policy │    │  · device tokens              │    │ outbox · review · policy │
 │ Ask (Ollama / extractive)│    │  · fleet demand · snapshot    │    │ Ask (Ollama / extractive)│
 └──────────────────────────┘    │    proxy                      │    └──────────────────────────┘
                                 │ Qdrant Server :6333           │
                                 │  one collection per site      │
                                 └───────────────────────────────┘
        Dashboard :8000 = launcher that starts/stops every process ("Stop cloud" really kills the server)
```

**One sync cycle** (every 4 s while online, and immediately on reconnect):

1. **hello**: heartbeat, report failed searches, receive fleet demand and each site's head sequence number
2. **push**: outbox in priority order through compare-and-set
3. **pull**: for each subscribed site whose head moved, a partial snapshot (full snapshot the first time) goes into the mirror shard via `snapshot_manifest()` and `update_from_snapshot()`
4. **settle**: purge local copies the mirror now holds (Qdrant's dual-write pattern), check new fleet memories for contradictions, re-run the policy on private notes against fleet demand

## How it maps to PS03

| PS03 goal | EdgeMind |
|---|---|
| Searchable semantic memory on the edge device | Qdrant Edge shards on disk per tablet (1 mutable + 1 mirror per site), dense + BM25 hybrid |
| Low-latency vector and hybrid search without network | RRF fusion of dense (FastEmbed, local ONNX) and Qdrant Edge's built-in BM25; ~1 ms at 20k memories |
| Dynamically decide what stays local vs syncs | Hard rules (credentials, private, PII → redacted) + adaptive share score (team value, **fleet demand**, local use) |
| Intermittent connectivity, keep operating offline | Everything runs on the tablet; outbox with safety-first priority; real server kill in the demo |
| Sync edge ⇄ Qdrant Server when connectivity returns | Full + **partial snapshots** into mirror shards; point sync fallback without a server |
| Evolving memory, updates, conflicting information | Versioned compare-and-set, 3-way conflict resolution, near-duplicate linking, **contradicting-value detection** |
| UI to inspect memory, search, sync status, activity | Two tablets + cloud side by side, review queue, sync queue, live metrics, activity log |
| A meaningful edge-to-cloud AI workflow | Fleet demand → private note suggested → shared → another crew receives it, with no central operator |

## Run it

Needs **Python 3.10–3.12** (tick "Add python.exe to PATH" on Windows).

```powershell
# Windows (PowerShell, inside the edgemind folder)
.\run.bat
```
```bash
# macOS / Linux
./run.sh
```

The first run installs packages, downloads **Qdrant Server** (~30 MB, from GitHub releases) and the **embedding
model** (~70 MB), which takes a few minutes. The dashboard then opens at **http://127.0.0.1:8000**. Ctrl+C stops everything.

- Already have Qdrant running (Docker or Qdrant Cloud)? `set QDRANT_URL=http://localhost:6333` before `run.bat`.
- No internet for the server download? It falls back to an embedded Qdrant with point sync; every feature except snapshots still works.

### Optional: real local LLM for answers

Install [Ollama](https://ollama.com), then `ollama pull llama3.2:1b`. EdgeMind detects it automatically; answers
then show "local LLM · llama3.2:1b". Without it, answers are extractive (the best matching sentences, cited).

## 2-minute demo script

1. **(15 s) The problem.** "Line crews work where there's no signal. Their knowledge is in heads and WhatsApp. EdgeMind puts it on the tablet."
2. **(25 s) Offline.** Switch crew A **Offline**. Ask *"what must I do before working on a line?"*: a cited answer in milliseconds. Add a fix; it shows ⏳ queued.
3. **(20 s) Privacy.** In Memory, the shift note is 🔒 device-only, and the T-417 fix is ✂ synced redacted (the cloud column shows `[EMAIL]` / `[PHONE]`). Crew B's store-room PIN never left.
4. **(25 s) Fleet demand.** Switch crew A back **Online** (its queued fix uploads, safety items first). On crew B ask *"transformer drain valve leaking, no spare valve"*, then click **Ask the fleet**. Within a few seconds crew A's Review shows **★ Another crew needs this**: its private "Drain valve leak trick". Click **Share**; it appears on crew B.
5. **(20 s) Conflicting facts.** On crew B add a fix *"Tightened HV bushing terminals on T-88 to 50 Nm"*. Review shows **50 Nm vs 40 Nm** against the SOP.
6. **(15 s) Real outage.** Click **Stop cloud** (really kills Qdrant + gateway). Crews keep working; queue grows. **Start cloud**: queue drains, partial snapshot size shows in the top bar.
7. **(20 s) Numbers.** "Partial snapshot: 124 KB instead of 41.8 MB. Search: 1 ms at 20,000 memories. Every decision explained."

Conflict demo (if asked): put both tablets offline, edit *Permit-to-work* differently on each, bring A then B online, then **Merge both** on B.

## Tests

```bash
python tests/test_e2e.py                          # real processes + real Qdrant Server
EDGEMIND_QDRANT=embedded python tests/test_e2e.py # no server (point sync)
python tools/bench.py --n 20000                   # latency + snapshot sizes (needs a running Qdrant Server)
```

The end-to-end test drives the full story over HTTP: subscriptions, redaction, offline write + search, partial
snapshot pull, conflict → merge, contradiction, fleet demand → share, offline answer, real server kill → queue →
drain, and delete propagation. **Run it once on your laptop**: it also confirms the thresholds work with the real
embedding model.

## Project layout

```
launch.py            start everything (dashboard + supervisor)
app/console.py       launcher: Qdrant Server, gateway, tablets as separate processes; dashboard API
app/gateway.py       cloud sync gateway: compare-and-set, sequence numbers, snapshot proxy, fleet demand
app/cloudstore.py    Qdrant Server collections (one per site), tuned for small partial snapshots
app/device.py        a tablet: mutable + mirror Qdrant Edge shards, hybrid search, review queues, metrics
app/sync_client.py   hello → push → pull (full/partial snapshot or points) → settle
app/device_api.py    a tablet's HTTP API (runs offline)
app/policy.py        what stays local, redaction, share score, priority
app/facts.py         conflicting-value detection
app/answer.py        offline answers: Ollama or extractive, with citations
app/qdrant_bin.py    downloads the Qdrant Server binary for your OS
app/seed.py          demo data: DISCOM line crews, Pune and Nagpur circles
static/              dashboard (plain HTML/CSS/JS)
tests/test_e2e.py    end-to-end story test
tools/bench.py       benchmark
```

## Known limits (honest)

- Tablets are separate processes on one laptop, not physical devices yet. The next step is the same device code on a Raspberry Pi or Android phone (Qdrant Edge has a React Native package).
- Contradiction detection compares numbers with units across similar memories; it does not understand free-text disagreements.
- Device tokens are static demo secrets; local shards are not encrypted at rest.
- If the embedding model cannot be downloaded, all processes fall back to a hashing embedder (search still works, less semantically).
