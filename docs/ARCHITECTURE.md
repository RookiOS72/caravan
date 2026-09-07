# Architecture: the self-organizing Caravan agent

Design target for Caravan's next phase, worked out in a 2026-09-06 design
session (see `docs/LOG.md` for the narrative version of how we got here).
This doc is the current prescriptive reference; update it in place as the
design evolves, rather than letting it drift out of sync with LOG.md's
diary entries.

## Goal

One app (the "Caravan agent"), installed identically on every machine in
the cluster. Each instance figures out on its own whether it's acting as
**head** (serves inference, holds the full model) or a **tail**
(contributes compute to whichever machine is head) — no manually-edited
plist per role, no hand-picked IPs, no human deciding up front which
machine is "the server."

## Why this isn't just "build what exo does"

**Correction (2026-09-06, later that night):** earlier LOG.md entries and
an earlier version of this section claimed exo requires every node to
hold a full local copy of the model. That was wrong -- never verified,
carried forward from an earlier session's assumption. Checked directly:
exo uses pipeline parallelism and each node downloads only the layer
range it's assigned (its "Placement Engine" decides the split, each
node's "DownloadCoordinator" fetches just its own shard) -- see
[exo's README](https://github.com/exo-explore/exo/blob/main/README.md).
That's the same shard-only-storage property this project's own planned
Phase 2 (below) is aiming for, not something Caravan does instead of
exo. Corrected the reasoning below; see LOG.md for the fuller writeup of
what was wrong and why.

llama.cpp's RPC backend still has a real structural asymmetry, just not
the one originally claimed here:

- **Head** (`llama-server`) needs the *full model file* on local disk (to
  read metadata/tokenizer and do orchestration) and does the actual
  serving.
- **Tail** (`rpc-server`) needs *nothing stored* — a dumb, stateless
  compute backend that executes whatever tensors arrive over the wire and
  forgets them when the process exits.

The genuine difference from exo isn't storage footprint -- it's that
exo's pipeline-parallel design has no node that needs *full* model
knowledge; every node, including whichever one starts the pipeline, only
ever needs its own shard. llama.cpp's RPC backend isn't built that way:
the head specifically needs the complete model file present, even though
it only computes some of the layers itself. That's a real, inherent
constraint of this backend, not a duplication cost Caravan is avoiding by
being cleverer than exo. Practical effect is the same either way: head
role follows wherever the full model already lives.

## Role determination — self-declared, not elected

No leader-election protocol. Each agent instance checks its own local
Ollama blob store on startup/periodically:

- Has the model requested → **head** for that model. Launches
  `llama-server --rpc <discovered peers>`.
- Doesn't have it → **tail** candidate. Ensures `rpc-server` is running,
  waits to be used.

In practice there's one Ollama instance across the whole cluster (the
machine running openclaw's gateway, or whichever machine a human/agent
points requests at) — that machine pulls models and is therefore always
head. This is fine and expected, not a limitation to design around: the
value of "one app everywhere" is a uniform install and automatic
tail-side behavior, not that head literally moves around day to day.
Multi-model support falls out for free regardless (different nodes could
independently be head for different models, if that ever becomes real
usage) without any extra design work.

No safeguard is needed for "two nodes both have the same model and both
try to become head" — given the single-Ollama-instance reality this
isn't a real scenario. Revisit only if actual usage changes.

## Discovery — hybrid: LAN mDNS + Tailscale when available

- Every agent advertises + browses a `_caravan._tcp` Bonjour/mDNS service
  on the LAN — zero-config, no dependency on any other infrastructure.
- Separately, when Tailscale is present and running on a machine, the
  agent also queries `tailscale status --json` for peers.
- Each agent has a small persistent identity (a UUID written to disk on
  first run) so a peer discovered both ways doesn't get treated as two
  different nodes.
- When a peer is reachable both ways, **prefer its Tailscale IP**. That's
  the empirically reliable path from tonight's incident (LAN IPs threw
  unexplained `EHOSTUNREACH` from certain process contexts, worked around
  by switching to node-b's Tailscale IP — see LOG.md, "The retry cascade,
  root-caused" and the full-rebrand entry). LAN IPs are used only when a
  peer has no Tailscale address to offer.

## Head/tail interaction

Head, once it knows which peers are live tail candidates, decides a
tensor split and launches `llama-server --rpc host1:port,host2:port,...`
pointed at them — conceptually "I've got this model, I'm head, here's
your shard." Tails just need `rpc-server` already running and reachable;
llama.cpp's own RPC protocol handles the actual tensor transfer.

"Cache it for next time" (a tail persisting its assigned tensor range
across restarts) is **not** part of this phase — see Phase 2 below.

## Scope: what "one app" actually covers

Not just process spawning. Everything that's currently a separate,
manually-managed piece is in scope to eventually be absorbed into this
one agent, not left standing alongside it forever:

- **Process supervision** (Phase 1, below) -- replaces the hand-written
  launchd plists.
- **The slot-pinning reverse proxy** (`../proxy/slot_pin_proxy.py`) --
  currently a third, separately-supervised process. Not yet folded in;
  real open question is whether it becomes a component the agent runs
  itself, or stays a standalone piece the agent merely launches/monitors.
  Not decided -- see Open Questions.
- **Cross-node restart coordination** -- `../scripts/restart-rpc-server.sh`
  encodes the safe restart order (stop head, restart tail, start head)
  as a manual/scripted operator action today. Once agents run on both
  nodes with a real health/status surface between them, this becomes
  something the agents negotiate directly (a tail can tell its head "I'm
  about to restart" and get a clean pause instead of a crash) rather
  than a human running a script. See GitHub issue #7.
- **Monitoring/heartbeat** -- `health_server.py`'s `/health` endpoint
  today only answers "is this node a live Caravan agent" (for discovery
  gating). Growing it into real liveness + resource reporting (CPU,
  memory, and GPU memory via `ggml-rpc`'s existing `RPC_CMD_GET_DEVICE_MEMORY`)
  is the natural, single place this lives -- not a separate monitoring
  script -- since this endpoint already exists and already runs on every
  node.

## Two build phases

**Phase 1 — orchestration/robustness layer (building now).** Discovery,
self role-detection, process supervision (spawn/monitor `llama-server` or
`rpc-server` locally based on role, replacing tonight's hand-written
launchd plists), always picking the reliable IP per peer. This directly
absorbs GitHub issue #1 (manual launchd supervision) — once the agent
exists, hand-maintained per-role plists go away entirely rather than
getting a standalone fix first.

**Phase 2 — shard cache.** ~~A real patch to `ggml-rpc.cpp`~~ **Done
already, turned out to need no patch at all** (2026-09-06, later still:
see LOG.md, "Issue #3: solved natively"). `ggml-rpc.cpp` already ships a
hash-keyed, self-invalidating on-disk tensor cache; `rpc-server --cache`
turns it on. Verified live: 53s warm-cache restart vs. 7-11 minutes cold.
What's left for Phase 2 isn't a C++ patch, just making sure the agent
actually passes `--cache` when it spawns `rpc-server` (`supervisor.py`
doesn't yet — see LOG.md's "single unified app" entry). (An incoming
USB4/Thunderbolt link between the two Macs will separately speed up the
*cold*-cache/first-ever transfer — a nice bonus, not a substitute for
this.) Corresponds to GitHub issue #3, now closed.

## Language

**Phase 1: Python.** Matches the existing `slot_pin_proxy.py`, no build
step, cheapest to iterate while the design is still actively moving (it
changed multiple times in the course of one conversation tonight).

**Later: a compiled language, undecided.** Once Phase 1 has proven itself
in real daily use, port it — this is PLAN.md's stage 3 packaging/"real
app for other people" milestone, a distinct later decision, not something
Phase 1 needs to serve. The reasoning captured here and in LOG.md/GitHub
issues is the actual bridge to that port — not portable Python code.
Options, for whenever that decision comes up:

- **Go** — full cross-platform (macOS/Linux/Windows/BSD/more) with
  trivial cross-compilation (`GOOS`/`GOARCH`, no target SDK needed). Best
  choice if a non-Mac node ever joins the cluster.
- **Rust** — same platform breadth, the most battle-tested of the three
  for this class of networked infra tooling, more cross-compile setup
  overhead than Go.
- **Swift** — best native fit for today's all-Mac fleet: built-in
  Bonjour/Network framework (no third-party mDNS library needed), clean
  launchd/codesigning story. Real official Linux support, but less
  mature/common for this than Go or Rust; Windows support is the least
  mature of the three.

**Ruled out: Ruby / Rails.** Ruby is interpreted — a peer to Python, not
a compiled-language option. Rails specifically is a web application
framework (MVC, database-backed models, views, asset pipeline); Caravan's
agent is a networked daemon with no database and no web UI, so Rails'
actual machinery doesn't apply. Plain Ruby would be a fair comparison to
Python but has no advantage here — weaker stdlib/ecosystem for this kind
of systems/networking work, and, like Python, still needs a language
runtime installed on every node, so it doesn't get any closer to the
eventual compiled/dependency-free goal either.

## Open questions (not yet decided)

- Exact wire format for the head→tail "here's your shard" coordination
  message (today this is implicit in how `llama-server --rpc` connects
  out; whether the agent needs its own explicit message on top of that,
  or whether "tail has `rpc-server` reachable" is sufficient, is unresolved).
- Whether the slot-pinning proxy becomes a component the agent runs
  itself, or stays a standalone piece the agent merely launches/monitors
  (see "Scope: what 'one app' actually covers," above).
- Exact protocol for cross-node restart coordination (a tail telling its
  head "restarting, please pause" before it goes down) — GitHub issue #7
  scoped this down considerably once issue #3 made recovery cheap (~50s),
  so this is lower urgency than it was, but still undecided.

## Relationship to existing GitHub issues

- **#1** (supervise processes with launchd) — closed
  (2026-09-06, crash-recovery verified live). Phase 1 still absorbs the
  *manual per-role plist* problem this leaves behind; #1 itself was
  narrower (does it survive a crash/reboot) and that part is done.
- **#2** (system prompt size) — unrelated, still open on its own track.
- **#3** (shard-scoped cache) — closed (2026-09-06, solved natively via
  `rpc-server --cache`, no patch needed — see Phase 2 above). What
  remains for the agent is wiring the flag in, not building the feature.
- **#7** (node-to-node lifecycle signaling) — open; two of its three
  proposed signals turned out to already be covered by existing
  mechanisms (see LOG.md), the remaining monitoring/coordination piece
  is this doc's "Scope" section above, not separate work.
