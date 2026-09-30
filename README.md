# EdgeMind ⚡
### Offline-First AI Memory for Electricity Line Crews

**Code Cubicle 6.0 · Problem Statement 03 · Qdrant Edge on Every Node ⇄ Qdrant Server**

---

## 🚨 The Problem

Line crews at power distribution companies (DISCOMs) fix transformers, feeders, and poles where mobile connectivity comes and goes.

The knowledge they need — **safety procedures, previous fixes, and lessons from past monsoons** — often lives in people's heads, WhatsApp groups, and paper registers.

**EdgeMind** puts a **searchable AI memory on every crew's edge node** — the van box or tablet — using **Qdrant Edge**.

Crew phones communicate with their local node over Wi-Fi, allowing them to search and retrieve information **with no cloud dependency**.

When connectivity returns, EdgeMind:

- Decides what information the rest of the fleet should know
- Synchronizes relevant information through Qdrant Server
- Detects and flags conflicting information before someone acts on it
- Keeps phones, laptops, edge nodes, and the control-room dashboard updated live

![Dashboard](docs/dashboard.png)

---

# 🎯 What Judges Can Do in the Room

| Demo | What happens |
|---|---|
| **📱 Join from your phone** | Scan the QR code on the big screen, pick a crew, and access that crew's field app. Ask, add, and review information while the big screen updates live. |
| **⚡ Live fleet** | Add a fix on Crew A. It becomes searchable on Crew B's node in **~0.3 s**, with no manual synchronization. |
| **📡 Kill the signal** | Turn a node's **Uplink** off or disconnect Wi-Fi on a laptop node. Searches continue to work in milliseconds using the node's local Qdrant Edge shards. Writes are queued, with safety items prioritized. |
| **☁️ Kill the cloud** | Stop Qdrant Server and the gateway. Every edge node continues working locally. Queued changes drain in priority order when the cloud starts again. |
| **🔎 Fleet demand** | Crew B asks something it cannot answer → **Ask the fleet** → Crew A receives **★ Another crew needs this** as a private suggestion. One tap shares the knowledge. |
| **⚠️ Conflicting facts** | Write `tightened bushing terminals to 50 Nm`. EdgeMind immediately flags the statement against an SOP specifying **40 Nm** — even while offline. |
| **🔐 Privacy** | Write a note containing a phone number or PIN. PINs never leave the node. PII is redacted before being sent to the fleet; the original remains local. |
| **➕ Elastic fleet** | Use **+ Edge node** or run one command on another laptop. The new node enrolls using the join code, bootstraps from a snapshot, and appears live on the map. |
| **🌐 Ask in Hindi or Marathi** | Ask `लाइन पर काम शुरू करने से पहले क्या करना चाहिए?` Cross-lingual retrieval finds the English permit-to-work SOP in ~20 ms and the on-node AI answers in plain English with a citation. |
| **🤖 AI that admits it doesn't know** | Ask something no crew has documented. The local LLM responds that it is **not in this node's memory** and highlights **Ask the fleet** instead of hallucinating an answer. |
| **📷 Photo evidence** | Capture a photo of a burnt bushing. CLIP runs locally, making the photo searchable using both text (`burnt bushing`) and images (`seen this before?`). A thumbnail and vector reach the fleet while the full photo remains on the node. |
| **🧠 Why did this rank?** | Search in the Memory tab. Each result displays meaning, keyword, photo score, and fused score. Filter chips show native Qdrant facet counts. |
| **🕘 Memory history** | Open history on any memory to see every version the node has seen — created, edited, received from another crew, merged, or deleted. |
| **📈 Scale** | Run a 20,000-memory scale test directly on a node. The real hybrid pipeline achieves approximately **2.4 ms p50** latency. |

---

# 🏗️ Architecture

```text
  Crew phones (any browser)          Hub laptop / cloud VM              Another laptop (optional)
  ┌───────────────┐   Wi-Fi   ┌──────────────────────────────────┐   LAN   ┌──────────────────────┐
  │ field app /m  │◄─────────►│ Console :8000                    │◄──────►│ edge node            │
  └───────────────┘    SSE    │ dashboard · field app · proxy    │   SSE   │ (launch.py join)     │
                              │                                  │         │                      │
                              │ Live aggregator (SSE fan-out)   │         │ own Qdrant Edge      │
                              │                                  │         │ shards               │
                              │ Gateway :8100                     │         │                      │
                              │ enroll · tokens · site ACL       │         │ field app for crew   │
                              │ compare-and-set · seq nos        │         └──────────────────────┘
                              │ fleet demand · live push         │
                              │ snapshot proxy (gzip)            │
                              │                                  │
                              │ Qdrant Server :6333              │
                              │ (loopback only)                   │
                              │                                  │
                              │ Edge nodes: tablet-A, tablet-B… │
                              │ each: mutable shard + mirror     │
                              │ shard per site                   │
                              └──────────────────────────────────┘
```

## Edge Node Design

A **single node** consists of:

- A mutable Qdrant Edge shard for its own writes
- A read-only mirror shard for each subscribed site
- An outbox
- Review queues
- A synchronization policy

This follows Qdrant's recommended edge layout.

Search runs completely offline across all shards:

```text
                 ┌───────────────────────────────┐
                 │        Search Pipeline         │
                 └───────────────────────────────┘
                              │
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
      Meaning Search    Keyword Search    Photo Search
      multilingual     Qdrant BM25       CLIP
      MiniLM           built-in           text → image
      384-d dense                         512-d image
             │                │                │
             └────────────────┼────────────────┘
                              ▼
                       RRF Fusion
                              │
                              ▼
                     Recency Formula
                       (exp decay)
                              │
                              ▼
                  Results + "Why" Explanation

                     OR

                         MMR
                    for AI sources
```

Candidates come from each shard's native Qdrant query.

The final ranking uses **Reciprocal Rank Fusion (RRF)** across all shards because RRF scores from individual shards are not directly comparable — for example, a 3-memory local shard versus a 5,000-memory fleet mirror.

Payload indexes on:

- `kind`
- `site`
- `status`
- `photo`
- `updated_at`

drive filters and facet counts.

---

# 🤖 AI Answering

EdgeMind uses a two-stage answer pipeline:

### 1. Instant Answer

An answer is extracted directly from semantic search in **tens of milliseconds**.

### 2. Local LLM

A local LLM using **Ollama + `qwen2.5:1.5b`** streams a grounded and cited answer.

If the knowledge base does not contain the answer, EdgeMind explicitly says so instead of inventing information.

```text
User Question
      │
      ▼
Hybrid Search
      │
      ├── Dense / Meaning
      ├── BM25 / Keywords
      └── CLIP / Photos
      │
      ▼
RRF Fusion + Ranking
      │
      ▼
Instant Answer
      │
      ▼
Local LLM (optional)
      │
      ▼
Grounded + Cited Response
```

---

# 🔄 Synchronization Architecture

A synchronization cycle can be triggered by:

- A gateway event
- A local write
- A 15-second fallback timer

### Sync Flow

```text
1. HELLO
   │
   ├── Heartbeat
   ├── Report failed searches
   ├── Receive fleet demand
   ├── Receive site heads
   └── Receive cloud epoch
          │
          ▼
2. PUSH
   │
   └── Priority-ordered outbox
       through server-side compare-and-set
          │
          ▼
3. PULL
   │
   ├── No mirror yet
   │      └── Full snapshot
   │
   ├── Few changes
   │      └── Point delta
   │
   └── Many changes / periodic reconciliation
          └── Partial snapshot
          │
          ▼
4. SETTLE
   │
   ├── Purge local copies now held by mirror
   ├── Scan new fleet memories for contradictions
   └── Re-run privacy/share policy
```

### Adaptive Transport

| Situation | Transport |
|---|---|
| No mirror exists | Full snapshot restored into a Qdrant Edge shard |
| Few changes | Point delta upserted into the mirror |
| Many changes | Partial snapshot |
| Periodic reconciliation | `snapshot_manifest()` → changed segments → `update_from_snapshot()` |

Partial snapshots reconcile the mirror exactly with the server, including server-side deletes.

---

# ⚡ Live Synchronization

Every node maintains an event stream to the gateway.

When information changes on a site followed by a node:

- Synchronization begins within approximately **200 ms**
- The open stream also acts as the node's presence signal
- Node state streams to phones and the hub
- The hub fans updates out to dashboards
- No polling loops are required

---

# 📊 Measured Performance

**Test environment:** Windows 11 · Qdrant Server 1.19.1 · `qdrant-edge-py 0.8`

| Measurement | Result |
|---|---:|
| Node A write → searchable on Node B | **0.2–0.5 s** |
| Fleet demand → private note suggestion | **~0.3 s** |
| Full snapshot bootstrap | **~29 KB on wire** |
| Snapshot shard size on Windows | ~180 MB on disk |
| Point delta for 1 new memory | ~5.5 KB |
| Offline hybrid search + semantic extraction | **~20–50 ms** |
| Local LLM first words | **~1.4 s** |
| Local LLM full answer | **~2–3 s warm** |
| Hindi / Marathi → English SOP retrieval | **11 / 11 correct** |
| English-only model baseline | 5 / 11 correct |
| 20,000-memory Edge shard hybrid search | **2.2–2.4 ms p50** |
| 20,000-memory search p95 | **3.5 ms** |
| Photo captured on Node A → thumbnail + CLIP vector on Node B | **~2 s** |

### Reproduce the benchmarks

```bash
python tests/test_e2e.py
python tools/bench.py --n 5000
```

The benchmark requires a running Qdrant Server.

---

# 🛠️ Engineering Notes

### Windows Snapshot Size

Windows snapshots were initially **873 MB for only 4 documents**.

Qdrant preallocates 32 MB pages per appendable segment, and NTFS does not support sparse files, causing zeros to be included in the snapshot tar.

With 8 segments, the first synchronization timed out.

**Fix:**

- Reduced to 2 segments
- 1 MB WAL
- Gzip streaming
- Additional gzip pass over Qdrant's compressed output

Result:

```text
873 MB → 29 KB
```

with almost no additional CPU overhead.

### Partial Snapshot Latency

Partial snapshots required **1.5–4.5 seconds** to apply on Windows, which was too slow for live synchronization.

EdgeMind therefore uses:

```text
Point deltas
    ↓
Fast liveness

Partial snapshots
    ↓
Exact reconciliation
```

### Process Lifecycle

On Windows, killing the launcher previously left Qdrant and edge nodes running.

The system now uses:

- Windows Job Objects with `KILL_ON_JOB_CLOSE`
- Python children watching the hub's PID

### Deleted Memories

`qdrant-edge-py 0.8` does not correctly match `MatchValue(True)` on boolean payloads.

As a result, the original "not deleted" filter could silently become a no-op.

EdgeMind now stores keyword flags such as:

- `status`
- `photo`

and uses those flags for edge filtering.

A regression test covers this behavior.

### Cross-Shard Ranking

RRF is rank-based.

Fusing RRF scores independently within each shard could allow the top result from a tiny 3-memory shard to tie with the true best result from a large fleet mirror.

EdgeMind therefore performs final ranking using **global retriever ranks across all shards**.

### RAM Optimization

On a 16 GB laptop:

- The gateway loads the text model only for seeding, then releases it
- Photo models load on first use and unload after 2 idle minutes
- Ollama keeps its model loaded for 10 minutes

### Multilingual LLM Limitation

Tiny 1–1.5B models produced broken Hindi/Marathi responses during testing.

Therefore:

> **Retrieval is multilingual, but AI answers are generated in plain English from the English SOPs.**

The design prioritizes correctness over producing impressive but potentially incorrect multilingual output.

---

# 🎯 How EdgeMind Maps to PS03

| PS03 Goal | EdgeMind Implementation |
|---|---|
| Searchable semantic memory on edge device | Qdrant Edge shards on each node's disk — 1 mutable + 1 mirror per site, with dense + BM25 hybrid search |
| Low-latency vector and hybrid search without network | On-device RRF fusion; field app displays search latency |
| Dynamically decide what stays local vs syncs | Hard rules for credentials/private/PII + adaptive share score based on team value, live fleet demand, and local usage |
| Intermittent connectivity | Per-node uplink switch, real Wi-Fi disconnection on laptop nodes, real server kill, priority outbox |
| Edge ⇄ Qdrant Server synchronization | Full / partial snapshots + point deltas, server sequence numbers, epoch-based rehydration |
| Evolving memory and conflicts | Versioned compare-and-set, keep mine / theirs / merge, near-duplicate linking, contradictory-value detection |
| UI for memory, search, sync status, activity | Control-room dashboard with live fleet map + phone field app per crew |
| Meaningful edge-to-cloud AI workflow | Failed search → fleet demand → another crew's private note suggested → shared → received live |

---

# 🚀 Getting Started

## Requirements

- **Python 3.10–3.13**
- Approximately **4 GB free RAM** for the full demo
- Windows users should select **Add python.exe to PATH** during installation

---

## ▶️ Run on Windows

```powershell
# Inside the edgemind folder
.\run.bat
```

## ▶️ Run on macOS / Linux

```bash
./run.sh
```

The first run:

1. Installs required packages
2. Downloads Qdrant Server (~30 MB)
3. Downloads the embedding model (~70 MB)
4. Starts the EdgeMind hub

You should see:

```text
EdgeMind hub is live

Dashboard (this laptop): http://127.0.0.1:8000
Dashboard (any device):  http://192.168.x.x:8000
Crew phones:             http://192.168.x.x:8000/m
                           (+ a QR code)

Operator PIN:            ######
Fleet join code:         EM-####
```

### Windows Firewall

Windows may ask whether Python can use the network.

Select:

> **Private networks → Allow**

Otherwise phones and other laptops may not be able to reach the hub.

If port `8000` is already occupied, the hub automatically selects the next available port and displays it.

---

# 📱 Connect Phones

Connect the phone and hub laptop to the **same Wi-Fi network**.

Alternatively, connect both to a phone hotspot. This is often more reliable at venues where Wi-Fi blocks device-to-device traffic.

Then either:

- Open the **Crew phones** URL
- Scan the QR code displayed under **📱 Join from a phone**

---

# 💻 Add Another Laptop as an Edge Node

Copy the project folder to another laptop.

The dashboard's Join dialog provides the exact command, similar to:

```powershell
.\run.bat join `
  --hub http://192.168.x.x:8100 `
  --code EM-#### `
  --name "Line crew C" `
  --sites pune,global
```

The laptop will:

- Maintain its own Qdrant Edge shards
- Serve its crew's field app at `http://<its-ip>:8001`
- Appear on the hub's fleet map
- Continue answering when disconnected
- Automatically catch up when connectivity returns

---

# 🤖 Local LLM with Ollama

For the full AI-answering experience, install [Ollama](https://ollama.com):

```powershell
winget install Ollama.Ollama
```

Then:

```bash
ollama pull qwen2.5:1.5b
```

The model is approximately **1 GB**.

EdgeMind automatically detects Ollama and streams the generated answer underneath the instant extracted answer.

Without Ollama:

- Offline search still works
- Instant extracted answers still work
- AI-generated responses are simply unavailable

The first run also downloads multilingual text and CLIP photo models, approximately **700 MB total**.

---

# 🐳 Deploy with Docker

EdgeMind provides a single Docker image for every role.

`docker-compose.yml` starts:

- Cloud tier
- Hub
- Two edge nodes
- Individual volumes for each node's Qdrant Edge shards

The AI models are included in the image, allowing a node to operate without internet access from its first boot.

## Start

```powershell
copy .env.example .env

# Set HOST_IP to this machine's Wi-Fi IP
# Find it using: ipconfig -> IPv4 Address

docker compose up -d --build
```

The first build takes approximately **5 minutes**, downloads around **700 MB of models**, and produces an image of approximately **2 GB**.

Open:

```text
http://localhost:8000
```

or use the configured `HUB_PORT`.

Phones can access:

```text
http://<HOST_IP>:8000/m
```

The operator PIN is configured using:

```text
OPERATOR_PIN
```

AI answers use Ollama through:

```text
host.docker.internal:11434
```

---

## Docker Services

| Service | Role | Published |
|---|---|---|
| `qdrant` | Qdrant Server 1.19.1 | Not published — gateway only |
| `gateway` | Sync gateway | `GATEWAY_PORT` (8100) |
| `hub` | Dashboard + phone app + live aggregator | `HUB_PORT` (8000) |
| `node-a` | Edge node | Not published |
| `node-b` | Edge node | Not published |

Two Docker networks mirror the field architecture:

```text
cloud
  gateway ⇄ Qdrant ⇄ node uplinks

lan
  hub ⇄ edge nodes
```

This makes a real uplink failure easy to simulate.

### Simulate an Uplink Failure

```powershell
docker network disconnect edgemind_cloud edgemind-node-a-1

# Crew A loses cloud connectivity.
# Local writes continue and are queued.

docker network connect edgemind_cloud edgemind-node-a-1

# Uplink restored; queued writes drain.

docker compose stop gateway qdrant

# Entire cloud goes down.

docker compose start qdrant gateway

docker compose logs -f hub

docker compose down

# Add -v to also delete all data.
```

### Add More Nodes

Copy a `node-*` service with a new:

- ID
- Name
- Site configuration
- Volume

Alternatively, run:

```bash
launch.py join
```

on another laptop against:

```text
http://<HOST_IP>:<GATEWAY_PORT>
```

---

# 🌐 Public URL

For judges using mobile data without shared Wi-Fi, EdgeMind supports a Cloudflare Quick Tunnel.

Install `cloudflared`:

```powershell
winget install Cloudflare.cloudflared
```

Then:

```powershell
powershell -ExecutionPolicy Bypass -File tools\public-url.ps1
```

The command prints a public:

```text
https://....trycloudflare.com
```

URL.

The dashboard QR code will then point phones to the public URL, allowing judges to join over mobile data.

### Tunnel Details

- Uses HTTP/2 over TCP 443
- Works on networks that block QUIC/UDP
- Streaming responses are buffered in this mode
- Pages automatically fall back to 2-second polling
- LAN connections continue using instant server push
- The quick-tunnel URL changes every time it starts
- The tunnel only works while the laptop is online

For a permanent hostname, a free Cloudflare account can be used with:

```bash
cloudflared tunnel create ...
```

> **Security note:** Tunnel the Docker deployment. Administrative actions over the public URL require `OPERATOR_PIN`.

Do **not** tunnel the `run.bat` hub as-is. Set:

```text
EDGEMIND_TRUST_LOCALHOST=0
```

first, because tunneled requests appear local to the application.

---

# 🌍 Crews on Other Networks

Edge nodes use a **store-and-forward** architecture.

Nodes save information locally and forward it whenever they can reach the gateway.

To allow a node on another network to reach a gateway running on the current laptop:

```powershell
powershell -ExecutionPolicy Bypass -File tools\public-url.ps1 -Port 8110
```

Then:

```powershell
.\run.bat join `
  --hub https://<gw>.trycloudflare.com `
  --code <JOIN_CODE> `
  --name "Line crew C" `
  --sites nagpur,global
```

### Measured Tunnel Performance

| Scenario | Time |
|---|---:|
| Remote crew bootstrap | ~18 s |
| Crew A → Crew C | ~3.6 s |
| Crew C → Crew B | ~1 s |
| Offline write from C → B after reconnect | ~1 s |

Behind a tunnel, server push may be buffered, so the node synchronizes approximately every **3 seconds**.

For permanent deployment, run:

```text
Qdrant + Gateway + Hub
```

on a cloud server and point every field node to it.

---

# 🔐 Security Model

| Threat | Control |
|---|---|
| Random device writes to fleet | Nodes enroll using a fleet join code and receive a unique random token |
| Token compromise | Tokens are stored hashed on the gateway and can be revoked from the dashboard |
| Pune crew accessing Nagpur data | Site ACLs are enforced on every gateway read/write |
| Wi-Fi user reading cloud data | Gateway read APIs require the admin token; Qdrant listens only on loopback |
| Someone wiping the demo | Reset, Stop Cloud, and node-management actions require the operator PIN |
| Credentials reaching the cloud | Passwords, PINs, and OTPs never leave the node |
| PII reaching the cloud | Phone numbers, emails, and ID numbers are sent only as redacted copies |
| Oversized/malformed requests | API length limits and schema validation |

### Known Security Gaps

The following limitations are currently acknowledged:

- LAN traffic uses plain HTTP
- Production deployment should place the hub behind TLS
- Shards are not encrypted at rest
- The fleet join code is shared across the fleet

---

# 🧪 Testing

EdgeMind includes a feature-by-feature **pytest** suite.

Each feature can run independently, while one command runs the complete suite.

System tests start their own isolated environment containing:

```text
Qdrant Server
    +
Gateway
    +
Tablet A
    +
Tablet B
    +
Hub
```

The stack runs in a temporary folder on free ports, so the existing `data/` directory and running hub are not touched.

## Run Tests

```powershell
# Everything (~10–15 min)
.\test.bat

# List available features
.\test.bat --list

# Run one feature
.\test.bat offline

# Run multiple features
.\test.bat sync conflicts

# Component tests only (~30 s)
.\test.bat --fast

# Pass additional pytest arguments
.\test.bat conflicts -k merge

# Keep the temporary stack folder
.\test.bat --keep ...
```

On any OS:

```bash
python tests/run_tests.py ...
```

Install development dependencies once:

```bash
python -m pip install -r requirements-dev.txt
```

---

# 🧪 Feature Test Coverage

| Feature | What it proves |
|---|---|
| `edge_memory` | Device memory: write, edit, versioning, re-indexing, delete/tombstone, history, restart persistence |
| `hybrid_search` | Dense, BM25, hybrid search, explanations, filters, latency, misses, Hindi/Marathi, scale testing, offline operation |
| `sync_policy` | Local vs synced information, redaction, credential protection, priority, private-memory retraction |
| `sync` | Enrollment, snapshot bootstrap, site subscriptions, live push, dual-write purge, edit/delete propagation, partial snapshot reconciliation |
| `offline` | Uplink failure, priority outbox, real cloud kill, queued writes, automatic drain |
| `conflicts` | Concurrent edits, merge, keep mine, take theirs, edit-vs-delete, newest readings, duplicate linking |
| `contradictions` | 50 Nm vs 40 Nm contradiction detection both offline and when received from another crew |
| `fleet_demand` | Failed search → fleet demand → private note suggestion → sharing → live receipt |
| `answers` | Offline cited answers, honest "don't know", cross-crew knowledge, local LLM |
| `photos` | CLIP on node, text→photo, photo→photo, fleet photo search, full photo remaining local |
| `security` | Join code, tokens, site ACL, admin-only reads, revocation, operator PIN, validation, loopback Qdrant |
| `dashboard_ui` | Pages, assets, JavaScript parsing, fleet/state/activity APIs, live SSE streams, QR code |
| `elastic_fleet` | Add node → bootstrap → live exchange → remove → token revocation → shard deletion |
| `remote_node_and_reset` | Separate `launch.py join` process, HTTP enrollment, offline operation, hub reset and rehydration |

When a test fails, the report includes:

- Failing assertion and values
- Passed flow steps
- Failed flow step
- Sync status of each touched node
- Recent activity logs
- Gateway/node process log tails
- Location of the retained test stack
- Summary table per feature

---

# 🧪 Story Test & Benchmark

The original end-to-end story test and benchmark are also available.

```bash
# Real processes + real Qdrant Server
python tests/test_e2e.py

# Embedded Qdrant — no server required
EDGEMIND_QDRANT=embedded python tests/test_e2e.py

# 20,000-memory benchmark
python tools/bench.py --n 20000
```

The story test drives the system over HTTP through the hub, just like real phones:

1. Enrollment, subscriptions, and redaction
2. Security — admin-only reads, site isolation, invalid join code
3. Offline write and search
4. Reconnection
5. Conflict → merge → memory history
6. Contradiction detection
7. Fleet demand → knowledge sharing
8. Offline answer generation
9. Hindi + Marathi retrieval
10. Local LLM grounded answers
11. "Not in memory" response when knowledge is unavailable
12. Real server kill → queue → drain
13. Delete and verify deleted memories never return
14. Live propagation without manual synchronization
15. Photo text→photo and photo→photo search
16. Scale testing
17. Adding a node from the console
18. Separate `launch.py join` process
19. HTTP bootstrap and live writes
20. Hub reset → remote node rehydrates the fleet

---

# 📁 Project Structure

```text
EdgeMind/
│
├── launch.py
│   └── Hub by default or `join` to turn this machine into an edge node
│
├── app/
│   ├── console.py
│   │   └── Hub: supervisor, dashboard, field app host,
│   │       proxies, live SSE aggregator, operator PIN
│   │
│   ├── gateway.py
│   │   └── Cloud sync gateway: enrollment, tokens, site ACL,
│   │       compare-and-set, live push, snapshots
│   │
│   ├── cloudstore.py
│   │   └── Qdrant Server collections, snapshot tuning,
│   │       gzip snapshot stream
│   │
│   ├── device.py
│   │   └── Edge node: mutable + mirror Qdrant Edge shards,
│   │       hybrid search, review queues, rehydration
│   │
│   ├── sync_client.py
│   │   └── Hello → push → adaptive pull
│   │       (full / partial snapshot / point delta) → settle
│   │
│   ├── device_api.py
│   │   └── Node HTTP API, SSE events and field app
│   │       (works without uplink)
│   │
│   ├── edge_node.py
│   │   └── `launch.py join`
│   │
│   ├── lifeline.py
│   │   └── Child process lifecycle management
│   │       (Windows Job Object + parent watch)
│   │
│   ├── policy.py
│   │   └── Local retention, redaction, share score, priority
│   │
│   ├── facts.py
│   │   └── Contradictory-value detection
│   │
│   ├── answer.py
│   │   └── Offline answers using Ollama or
│   │       semantic sentence extraction with citations
│   │
│   └── seed.py
│       └── Demo data for DISCOM line crews,
│           Pune and Nagpur circles
│
├── static/
│   ├── index.html
│   ├── app.js
│   ├── field.html
│   └── field.js
│       └── Dashboard and phone field app — no build step
│
├── tests/
│   ├── run_tests.py
│   ├── test_NN_<feature>.py
│   └── test_e2e.py
│       └── Feature suite + end-to-end story test
│
├── tools/
│   └── bench.py
│       └── Performance benchmark
│
└── docs/
    └── dashboard.png
```

---

# 🌟 Key Features

### 📴 Offline-First

The entire field workflow continues to work when:

- Mobile internet disappears
- Wi-Fi is disconnected
- The cloud goes down

### 🧠 AI Memory

Search across:

- Semantic meaning
- Keywords
- Images
- Historical memories

### 🔄 Intelligent Synchronization

Adaptive synchronization chooses between:

- Point deltas
- Full snapshots
- Partial snapshots

depending on the state of each site.

### ⚠️ Conflict & Contradiction Detection

EdgeMind detects conflicting field information and provides mechanisms to:

- Keep mine
- Take theirs
- Merge

### 🔐 Privacy-Aware Sharing

Sensitive information remains local while shareable knowledge can propagate through the fleet.

### 🌐 Multilingual Retrieval

Crew members can ask questions in Hindi or Marathi while retrieving English SOPs.

### 📷 Visual Memory

Photos can be searched using both:

- Text
- Other photos

using local CLIP embeddings.

### 📈 Elastic Fleet

New edge nodes can join dynamically, bootstrap from snapshots, and immediately participate in fleet synchronization.

### 🤖 Honest AI

When the information does not exist in the node's memory, EdgeMind says so rather than hallucinating.

---

# ⚡ Why EdgeMind?

EdgeMind is designed around one principle:

> **The field should keep working even when the internet doesn't.**

Instead of treating the edge as a temporary cache of cloud data, EdgeMind makes every crew's device a **real local AI memory node** capable of:

```text
SEARCH
  ↓
UNDERSTAND
  ↓
ANSWER
  ↓
LEARN
  ↓
SHARE
  ↓
SYNC
```

The result is a distributed knowledge system where **every crew can work independently offline while the fleet continuously learns from one another whenever connectivity returns.**

---

## 📌 Tech Stack

| Layer | Technology |
|---|---|
| Edge Vector Database | **Qdrant Edge** |
| Cloud Vector Database | **Qdrant Server 1.19.1** |
| Semantic Search | **Multilingual MiniLM · 384-d** |
| Keyword Search | **Qdrant BM25** |
| Image Search | **CLIP · 512-d** |
| Ranking | **Reciprocal Rank Fusion (RRF)** |
| Diversity | **MMR** |
| Local LLM | **Ollama · qwen2.5:1.5b** |
| Backend | **Python** |
| Communication | **HTTP + SSE** |
| Containerization | **Docker + Docker Compose** |
| Public Networking | **Cloudflare Tunnel** |
| Testing | **Pytest** |
| Supported Python | **3.10–3.13** |

---

## 🏆 Problem Statement

**Code Cubicle 6.0 — Problem Statement 03**

> Build an AI-powered edge memory and intelligence platform capable of operating locally under intermittent connectivity while intelligently synchronizing useful knowledge across the fleet.

**EdgeMind addresses this through local Qdrant Edge memory, offline AI retrieval, adaptive synchronization, privacy-aware sharing, conflict detection, and a live fleet intelligence layer.**
