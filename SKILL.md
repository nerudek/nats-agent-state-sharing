---
name: nats-agent-state-sharing
description: Shared runtime state for multi-agent AI systems using NATS JetStream KV. Agents know who is doing what, in real time, without polling files or reinventing discovery.
version: 1.0.0
author: nerudek
compatible-with: claude-code, openclaw, hermes-agent, kimi-code, goose
tags: [nats, jetstream, multi-agent, shared-state, ai-agents, coordination]
---

# NATS Agent State Sharing — Shared Runtime Memory for Multi-Agent AI

## Problem

**You have 4 AI agents running. None of them know what the others are doing.**

Vox is deploying configs. Claude is writing a constitution. Kimi is researching banner formats. Goose is training a model. Each agent has its own memory, its own session state, its own task list. But they are blind to each other.

This blindness causes real damage:
- **Duplicated work.** Two agents research the same topic because neither knows the other already started.
- **Conflicting changes.** Agent A changes a config parameter. Agent B changes the same parameter in a different file. Now neither value is correct and nobody knows why.
- **Lobotomized handoffs.** Agent A finishes a task, writes "done" to a file, and terminates. Agent B starts a new session, reads a DIFFERENT file, and has no idea what Agent A did. The knowledge is lost.
- **Silent failures.** Agent C crashes. Agent D waits for Agent C's output forever. There is no heartbeat, no dead-man switch, no way to detect the failure.

The root cause is **fragmented runtime state.** Configuration values, task statuses, agent heartbeats, and coordination data are scattered across: Obsidian vault files, `~/.hermes/memories/`, `~/.mempalace/palace/`, NATS subjects, TCP relay messages, and individual agent session files. There is no single source of truth for "what is happening right now."

**Why existing solutions fail:**

- **File-based state (Obsidian, MemPalace, SESSION-STATE.md):** Files work for long-term knowledge. They fail for runtime state because: (a) agents poll files at different intervals, (b) there is no atomic read-modify-write, (c) two agents writing to the same file corrupt it, (d) there is no notification mechanism — agents must actively check, which they forget to do.
- **Custom TCP protocols (bridge, relay):** These work for point-to-point messages but don't provide shared state. Each agent must implement its own state tracking. There is no persistence — if the relay crashes, all state is lost.
- **Redis/memcached:** Requires separate infrastructure, isn't embedded, and doesn't solve the discovery problem (agents still need to know where Redis is).
- **HTTP polling:** Agents would need to poll an endpoint every N seconds. This wastes resources, has latency, and agents forget to poll consistently.

**The specific failure we experienced:** Vox deployed V3 configs, updated the Obsidian vault, wrote a HANDOFF, and published a NATS event. Claude started a new session, read the Obsidian vault (which had the old configs cached), missed the NATS event (wasn't subscribed), and began rewriting configs that Vox had already deployed. Two hours of work were thrown away because there was no shared runtime state saying "Vox already did this."

## Solution

**NATS JetStream KV as a shared runtime state layer.**

NATS is a lightweight, high-performance messaging system (7MB binary, 0 external dependencies). JetStream adds persistence and Key-Value storage on top of NATS pub/sub. Together they provide:

1. **Shared KV store** — One bucket (`agents`) with keys like `agents.vox.state`, `agents.nerudek.project`, `agents.nerudek.training`. Every agent reads and writes the same keys. There is ONE source of truth.

2. **Real-time notifications** — When Vox writes `agents.nerudek.project`, all agents subscribed to `agents.nerudek.>` get notified instantly. No polling. No missed updates.

3. **Persistence** — JetStream stores values on disk. If NATS restarts, all KV data is recovered. Agents that were offline catch up automatically.

4. **History/revision tracking** — JetStream keeps N revisions per key. You can see WHO changed WHAT and WHEN. This means agents can detect conflicts: "I was about to change this, but Vox changed it 3 seconds ago — let me read the new value first."

5. **Zero extra infrastructure** — NATS runs as a single binary on the same machine as the agents. No Redis, no etcd, no external dependencies.

### Architecture

```
┌─────────────────────────────────────────────────────┐
│                   NATS Server (:4222)                │
│  ┌───────────────────────────────────────────────┐  │
│  │         JetStream KV Bucket: agents           │  │
│  │                                               │  │
│  │  agents.vox.state          = {"status":"idle"}│  │
│  │  agents.hermes.kubuntu.state = {"status":"..."}│  │
│  │  agents.goose.kubuntu.state  = {"status":"..."}│  │
│  │  agents.nerudek.project      = {"pipeline":...}│  │
│  │  agents.nerudek.training     = {"layer1":...} │  │
│  └───────────────────────────────────────────────┘  │
│                                                      │
│  Pub/Sub Topics:                                     │
│    agents.events  — deployment complete, errors      │
│    agents.>       — wildcard for all agent messages  │
└──────────┬──────────┬──────────┬────────────────────┘
           │          │          │
      ┌────┴───┐ ┌────┴───┐ ┌────┴───┐
      │  Vox   │ │ Claude │ │ Goose  │
      │  (M4)  │ │  (M4)  │ │(Kubuntu)│
      └────────┘ └────────┘ └────────┘
```

### State Keys (what we track)

| Key | Purpose | Example Value |
|-----|---------|---------------|
| `agents.vox.state` | What Vox is doing right now | `{"status":"deploying_configs","since":"..."}` |
| `agents.hermes.kubuntu.state` | Hermes on Kubuntu status | `{"status":"training","model":"sdxl"}` |
| `agents.goose.kubuntu.state` | Goose worker status | `{"status":"idle","gpu_locked":false}` |
| `agents.nerudek.project` | Overall project phase | `{"phase":"setup","next":"verify_infra"}` |
| `agents.nerudek.training` | Training pipeline progress | `{"layer1":"done","layer2":"running"}` |

### Usage

**Read state:**
```bash
nats kv get agents agents.vox.state --server nats://localhost:4222
```

**Write state:**
```bash
nats kv put agents agents.vox.state '{"status":"deploying","task":"configs_v3"}' --server nats://localhost:4222
```

**Subscribe to all agent events:**
```bash
nats sub "agents.>" --server nats://localhost:4222
```

**Watch a specific agent:**
```bash
nats kv watch agents agents.nerudek.training --server nats://localhost:4222
```

### Setup

```bash
# 1. Install NATS (macOS)
brew install nats-server nats

# 2. Create config
cat > ~/agentos/infrastructure/nats.conf << 'EOF'
port: 4222
http_port: 8222
jetstream {
  store_dir: "/Users/nerucb1/agentos/nats-data"
  max_memory_store: 256MB
  max_file_store: 1GB
}
EOF

# 3. Start NATS
nats-server -c ~/agentos/infrastructure/nats.conf &

# 4. Create KV bucket
nats kv add agents --history 10 --server nats://localhost:4222

# 5. Initialize agent states
nats kv put agents agents.vox.state '{"status":"not_initialized"}' --server nats://localhost:4222
nats kv put agents agents.hermes.kubuntu.state '{"status":"not_initialized"}' --server nats://localhost:4222
nats kv put agents agents.goose.kubuntu.state '{"status":"not_initialized"}' --server nats://localhost:4222
```

### Agent Integration

**Python (Vox, Goose):**
```python
import nats, json

async def update_state(agent_name, status, task=None):
    nc = await nats.connect("nats://localhost:4222")
    js = nc.jetstream()
    kv = await js.key_value("agents")
    state = {"status": status, "updated": datetime.now().isoformat()}
    if task: state["task"] = task
    await kv.put(f"agents.{agent_name}.state", json.dumps(state).encode())
    await nc.close()
```

**Node.js (OpenClaw):**
```javascript
import { connect, JSONCodec } from 'nats';

const nc = await connect({ servers: 'localhost:4222' });
const js = nc.jetstream();
const kv = await js.views.kv('agents');
await kv.put(`agents.vox.state`, JSON.stringify({status: 'idle'}));
```

**Shell (any agent):**
```bash
nats kv put agents agents.vox.state '{"status":"active"}' --server nats://localhost:4222
```

## FAQ

**Q1: Why NATS instead of Redis?**
NATS is a single 7MB binary. Redis requires a separate server process, more memory, and doesn't have built-in pub/sub with the same simplicity. NATS JetStream gives us KV + messaging in one tool. For agent coordination where we need both state storage AND real-time notifications, NATS is the better fit.

**Q2: What happens if NATS crashes?**
JetStream persists all KV data to disk. When NATS restarts, the KV bucket is automatically recovered. Agents reconnect automatically. The `--history 10` flag means the last 10 revisions of each key are preserved, so you can see what changed during the outage.

**Q3: How is this different from writing files to Obsidian?**
Files are passive. Agents must remember to read them. Agents read at different times. Two agents can write to the same file simultaneously and corrupt it. NATS KV provides: atomic writes, instant notifications on change, revision history, and a single API that works identically from Python, Node.js, shell, and Go.

**Q4: Can agents on different machines use this?**
Yes. NATS listens on `0.0.0.0:4222` by default. Any agent on the Tailscale network can connect to `nats://100.95.129.85:4222`. We have Kubuntu agents connecting to the M4 NATS server through Tailscale. No VPN configuration needed beyond Tailscale.

**Q5: How do agents know which keys to read?**
The key naming convention is `agents.{agent_name}.{category}`. Every agent knows its own name and can read `agents.*.state` to discover other agents. The `agents.nerudek.project` key is the global coordination point — all agents read this to know the current project phase.

**Q6: What about security?**
NATS supports token authentication and TLS. For local/multi-machine setups behind Tailscale, we use token auth. The NATS server only accepts connections from Tailscale IPs. For production, add `authorization { token: "..." }` to the config.

**Q7: How much disk space does this use?**
Minimal. Each state key is a few hundred bytes of JSON. With 10 revisions per key and ~20 keys, total storage is under 100KB. The entire JetStream data directory is under 10MB even after months of operation.

**Q8: What if an agent writes conflicting state?**
The last write wins (standard KV semantics). But with `--history 10`, you can see all previous values and detect conflicts. The convention is: agents READ the current value before WRITING, and include a `previous_revision` field so the reader can detect if someone else changed it.

**Q9: How does this integrate with the existing bridge/relay protocols?**
NATS is the STATE layer. Bridge (TCP :17423) is the COMMAND layer. Relay (TCP :17426) is the CHAT layer. They serve different purposes: NATS for "what is happening", Bridge for "execute this command on M2", Relay for "hey Claude, what do you think about this?" They complement each other.

**Q10: Can I use this without installing anything?**
NATS server is a single binary — `brew install nats-server` on macOS, `apt install nats-server` on Linux. The `nats` CLI is similarly simple. No Docker, no database, no configuration management needed.

**Q11: How does this prevent the "two agents deploying the same config" problem?**
Before deploying, an agent reads `agents.nerudek.project` from KV. If `phase` is `deploying`, it knows someone else is already deploying and waits. After deploying, it updates the key to `phase: deployed`. The next agent sees this and skips the deploy step.

**Q12: What about agents that are offline?**
When an offline agent reconnects to NATS, it reads the current KV state and catches up. It doesn't need to replay events — the KV store has the latest value. For events it missed (like "deploy complete"), it can check the key's history with `nats kv history`.

**Q13: How do I monitor the system?**
NATS has a built-in HTTP monitoring endpoint on port 8222. `curl http://localhost:8222/connz` shows all connected agents. `curl http://localhost:8222/jsz` shows JetStream status. `nats kv ls` lists all buckets and keys. Zero setup required.

**Q14: Can I use this for task queues too?**
Yes. JetStream supports consumer groups with explicit ack. You can create a `tasks` bucket where agents push work items and workers pull them. This is more advanced than KV but available when needed.

**Q15: What's the migration path from file-based state?**
Incremental. Keep writing to Obsidian for long-term knowledge. Add NATS KV writes alongside. Agents that support NATS read from KV first, fall back to files. Over time, runtime state moves entirely to NATS KV, files remain for documentation and knowledge.

---

If this saved you time: [PayPal.me/nerudek](https://www.paypal.me/nerudek)
GitHub: [github.com/nerudek](https://github.com/nerudek)
