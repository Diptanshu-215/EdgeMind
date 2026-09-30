# EdgeMind demo runbook (5 minutes + Q&A)

## Before you go on stage (15 minutes before)

1. **Network.** Turn on a phone hotspot (or bring a travel router). Connect the hub laptop, the second laptop
   and your demo phone to it. Venue Wi-Fi usually blocks device-to-device traffic, and a hotspot doesn't.
2. **Start the hub** on laptop 1: `.\run.bat`. The first time, Windows asks about Python and the network:
   **Private networks → Allow**. Note the *Operator PIN* and *Fleet join code* in the terminal.
3. **Start the second laptop** (optional but the strongest moment): open the dashboard → *📱 Join from a phone* →
   copy the command, and on laptop 2 run `.\run.bat join --hub http://<hub-ip>:8100 --code EM-#### --name "Line crew C" --sites nagpur,global`.
   It appears on the fleet map as `… · laptop`.
4. Open the dashboard full screen on the projector: `http://127.0.0.1:8000`. Click **Reset** once so the data is clean.
5. On your phone open `http://<hub-ip>:8000/m` → *Line crew A*. Test one Ask. Ask once more on each crew so the
   local LLM is warm (the first answer after a cold start takes ~8 s). Close heavy apps: the hub, two nodes
   and the LLM want ~4 GB of free RAM.
6. Fallback if the network misbehaves: everything still works on one laptop. Open `/field/tablet-A` in a
   second browser window at phone size (DevTools → device toolbar).

## The story (say this, click this)

**0:00 – Problem (20 s).**
"Line crews fix transformers where the signal drops. Their knowledge lives in heads and WhatsApp. EdgeMind puts
an AI memory on every crew's edge node with Qdrant Edge. It answers with no network and syncs when it can."
Point at the fleet map: cloud on top, nodes below, green animated links = live.

**0:20 – Judges join (40 s).**
Open *📱 Join from a phone*. "Scan this, pick a crew." While they scan: "Your phone is now talking to that crew's
node. Ask it anything: *What must I do before working on a line?*" The answer is cited, in milliseconds, and the
answer card shows the search time.

**1:00 – Live fleet (30 s).**
On crew A (your phone or panel) → *Add* → kind *fix*, site *global*: "Re-crimped the jumper lug on pole 42 after
a flashover." Save. Crew B's panel flashes the new memory, and the top tile shows **write → searchable on tablet-B
in ~0.3 s**. A dot travels up then down the map. "No refresh, no sync button. The gateway pushes, B pulls a 5 KB
point delta."

**1:30 – No signal (40 s).**
Flip crew A's **Uplink off** (or pull laptop 2's Wi-Fi: the most convincing version). Ask again: still answers,
and the tag says "no uplink: answered on the node". Add an incident mentioning *sparks*: it shows ⏳ queued, priority 0.
Flip it back on: the queue drains safety-first, and the map pulses.

**2:10 – The AI workflow (50 s).**
On crew B ask: *Transformer drain valve leaking and no spare valve*. Tap **Ask the fleet**.
Crew A's phone pops **★ Another crew needs this** within a second: its *private* note "Drain valve leak trick".
"Nobody routed this. The fleet noticed B's failed search, A's node re-ran its policy and asked the human for
consent." Tap **Share**. It lands on B live.

**2:40 – Hindi + photos (30 s).**
Tap the Hindi sample *"लाइन पर काम शुरू करने से पहले क्या करना चाहिए?"*: the permit-to-work SOP comes back and
the AI answers with a citation. "A lineman asks in his language; the node finds the English SOP." Then *Add* →
📷 photo of anything electrical (a charger, a socket) → Save. On crew B's phone, Memory → type "charger" or tap 📷
and photograph the same thing: it finds A's photo, offline. "The full photo never left A; B got a 20 KB
thumbnail and a CLIP vector."

**3:00 – Wrong numbers (30 s).**
On crew B add a fix: "Tightened HV bushing terminals on T-88 to 50 Nm." Review shows **50 Nm vs 40 Nm** against
the SOP, detected offline. "Catches a wrong torque before someone cracks a bushing."

**3:30 – Privacy (20 s).**
Crew A's memory: the vendor note is ✂ synced redacted. The cloud column shows `[EMAIL]` `[PHONE]`. Crew B's
store-room PIN never left the node. Every decision lists its reasons.

**3:50 – Kill the cloud (30 s).**
**Stop cloud**: this really kills Qdrant Server and the gateway (the chips go red). Nodes keep answering;
writes queue. **Start cloud**: queues drain, nodes re-sync in a second.

**4:20 – Scale + engineering (30 s).**
Click **⚡ 20k scale test** on a node: 20,000 memories, same hybrid pipeline, ~2.4 ms. Click **+ Edge node**:
a new process enrolls with the join code, bootstraps its mirror from a 29 KB snapshot, and goes live on the map. "On Windows a Qdrant shard snapshot was 873 MB for four documents because of
preallocated pages. We tuned segments and gzip-stream it: 29 KB. Point deltas give sub-second liveness, partial
snapshots give exact reconciliation."

**4:50 – Close.**
"Offline-first, live when connected, private by policy, and the fleet learns from what crews can't find."

## Likely questions

- **Is this real Qdrant Edge?** Yes. `qdrant-edge-py` shards on each node's disk; mirrors are restored from real
  Qdrant Server shard snapshots (`snapshot_manifest` / `update_from_snapshot`).
- **What if two crews edit the same memory offline?** Put A and B *Uplink off*, edit *Permit-to-work* on both,
  bring A then B back: B's Review shows the conflict → *Merge both*.
- **Security?** Join code → per-node token (hashed, revocable); site ACL (a Pune node can't read Nagpur);
  admin token on every cloud read; operator PIN for admin actions; Qdrant bound to loopback. Gaps: TLS, encryption at rest.
- **Does the AI make things up?** It only sees the node's top memories and must cite them; when they don't
  answer, it says so (tested). Ask it about solar inverter firmware to show this live.
- **Why is the AI answer in English for a Hindi question?** 1–2B models on a laptop CPU write unreliable Hindi;
  we tested and chose correct over fluent. Retrieval is truly multilingual (11/11 on our test set).
- **Does it scale?** The ⚡ scale test (and `tools/bench.py --n 20000`) gives on-device search latency; the gateway
  serializes writes with compare-and-set and server sequence numbers, and nodes only pull what changed.
- **Why a gateway and not direct Qdrant?** Auth, site isolation, compare-and-set, server-side ordering, live
  push and fleet demand. Qdrant has none of those per-device.
