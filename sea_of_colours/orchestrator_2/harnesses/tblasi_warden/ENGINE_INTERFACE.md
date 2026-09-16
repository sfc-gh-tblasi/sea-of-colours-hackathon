# tblasi_warden ⇄ game engine — the interface, labelled

The single most important property of this harness: **it is a client of the
engine, not part of it.** It reads a fogged view, thinks, and hands back a list
of moves. It never mutates game state directly, never reaches around the view to
read ground truth, and never edits anything under `sea_of_colours/game/`.

This file states exactly where the boundary is, so a change on either side can
be checked against it. If you find harness code that violates one of the
invariants in §5, that is a bug, not a shortcut.

---

## 1. The boundary in one picture

```
          ┌──────────────────────── ENGINE (authoritative) ──────────────────────┐
          │  game/session.py · game/simulator.py · snowpark/engine.py            │
          └──────────┬──────────────────────────────────────────┬────────────────┘
                     │                                          ▲
       READ  (1 way in)                                WRITE  (2 ways out)
                     │                                          │
        build_agent_view(session, seat)              submit_policy(...)   NIGHT
        → the fogged `view` dict                     submit_orbit_actions(...) ORBIT
                     │                                          │
          ┌──────────▼──────────────────────────────────────────┴────────────────┐
          │                      tblasi_warden  (this package)                      │
          │                                                                      │
          │   view ─▶ derive ─▶ OPTION MENU ─▶ [ LLM picks IDs ] ─▶ packager ─▶  │
          │                                                        sanitizer     │
          └──────────────────────────────────────────────────────────────────────┘
                     │                                          ▲
        list_replay_frames(...)                     memory / journal rows
        (engine truth, last night)                  (harness-owned, see §4)
```

Everything the agent believes about the board comes through `view`. There is no
second channel. The harness is handed the view by the dispatcher — it does not
build it and cannot widen it.

---

## 2. READ — what comes in from the engine

### 2.1 The night view (the only board input)

`run()` receives `view`, the dispatcher's envelope, whose `agent_view` key holds
the seat's fogged board as built by `snowpark/engine.py:build_agent_view`. The
top-level keys v12 consumes:

| key | what the harness takes from it |
|---|---|
| `meta` | day, phase, and the live rules (`drop_mode`, `probe_radius`, `probe_lifetime_nights`) — read dynamically, never hardcoded |
| `hud` | day, `season_day_cap`, `scores`, `season_name` |
| `world` | `live` (current vision) and `echo` (cells seen before, with `last_seen_day`) |
| `red_tiles` / `blue_tiles` | visible resource cells and their purity |
| `redsign` / `blue_sign` | public smeared beacons over pure-red / fissile-blue seams |
| `entities` | harvesters, probes, stations — mine and any rival's that are visible |
| `probe_stock` | probes in inventory, the hard cap on how many the plan may spend |
| `competitor_intel` | rival probe sightings, harvester trails, `new_this_day` events |
| `station_intel`, `opponents` | seat identities and station positions, each with `arms.blue` / `arms.cap` (the **public** arsenal, exact) and an `activity` tally of that seat's observable orbital actions — `probes`, `dropped`, `recovered`, `emps`, `chaff`, `snaps` (v1.39; SNAP shipped without a counter here and read as flat) |
| `last_night` | the engine's own recap of the resolved night |
| `combat_events` | SNAP / EMP / chaff resolutions (an archived season may also carry mine events — see §3.1) |
| `orbit` | ORBIT-phase economy state (credits, build options) |

**Fog is respected as given.** If a cell is not in `world.live`, the harness
treats it as unseen even when it can infer what is probably there. The one
nuance worth knowing is that a *sign* is deliberately smeared by the engine —
you learn an area, never a square — and `out_of_grid.py` exists specifically to
present that as an area and not let the model read it as a coordinate.

**`recent_log` is there, and v12 does not read it.** The view also carries the
tail of the engine's night log as free text. Until v1.38 it was the *shared*
feed — every seat's rows, rival landing coordinates included — and a fork that
piped it into a prompt would have been reading its opponent's orders. It is now
cut to rows naming no other seat (§3.15, issue 45), so it is safe to read; it
is simply redundant, because `last_night` and `combat_events` carry the same
events already parsed. Prefer those. If you do use it, do not rebuild rival
inference on top of it — what a rival lawfully leaks reaches you through
`competitor_intel` and `station_intel`, and those are the fields that will keep
working when the log's wording changes.

### 2.2 Engine truth about last night

`store.list_replay_frames(...)` — the per-hour execution log the engine wrote
when it resolved the night (`SOC_REPLAY_FRAME`). Read in two places:

- `last_night.py` — to reconstruct what *actually* happened (what landed, what
  banked, which probes were crushed, which harvesters were lost) rather than
  what the agent hoped would happen.
- `digest.py` — to narrate incoming attacks and collisions.

This is the anti-confabulation anchor. The agent's own reflection is reconciled
against these frames, so it cannot claim a night went well when the frames say
nothing reached the board.

### 2.3 Constants imported from the engine (never copied)

Per the repo's fan-out rule, tuning values are imported rather than duplicated:

| import | used by |
|---|---|
| `game.tuning.probe_vision_radius` | `packager.py`, `out_of_grid.py` |
| `game.tuning.probe_lifetime_nights` | `option_economics.py`, `supersede.py` |
| `game.weapons` (SNAP/EMP/chaff dials) | `prompt.py`, `seam_control.py` |

If a dial moves in the engine, these follow automatically. **Adding a hardcoded
copy of an engine constant to this package is the failure mode this table
exists to prevent.**

---

## 3. WRITE — what goes out to the engine

Exactly two calls, and nothing else in the package writes game state:

| phase | call | site |
|---|---|---|
| NIGHT | `snowpark.engine.submit_policy(store, session_id, player, moves)` | `harness.py` step 8 |
| ORBIT | `snowpark.engine.submit_orbit_actions(store, session_id, player, actions)` | `orbit.py` |

### 3.1 The wire move vocabulary

`submit_policy` takes a flat, ordered list of moves, capped at **21**
(`_MAX_MOVES`, inherited from v7). The packager emits four verbs and no others:

| verb | meaning |
|---|---|
| `drop` | land a harvester on a cell (requires live coverage of that cell) |
| `step` | move a landed harvester one cell (auto-harvests on arrival; **not** vision-gated) |
| `pickup` | lift a harvester back to orbit, banking its hold |
| `probe` | place a probe |

Order matters: the engine resolves moves in the order given, on an hour cadence,
so the packager's sequencing is part of the plan's meaning — this is why
`_order_for_probe_support` reorders runs so a probe that grants drop legality
lands before the drop that needs it.

The wider night grammar also holds `wait`, `emp_launch`, `chaff_flare` and
`snap_launch` (v1.36); a fork that reaches for weapons emits those. It must
**not** emit `mine_lay`.

`snap_launch` takes a bare `{"a": "snap", "at": [x, y]}` — one cell, and a
payload offering a *list* of cells is refused by name rather than quietly
taking the first. It is worth knowing what you are buying: SNAP resolves
**above** the hour's vision snapshot and the EMP resolves below it, so a
SNAP that kills a beacon denies the drop that beacon was lighting *the same
night*, and an EMP on the same beacon in the same hour does not. That is the
one asymmetry in the weapon set that a plan can actually be built around
(RULEBOOK §4.9.4). The stock version is `weapon_stock.snap`; the published
spec is `weapon_specs.snap`, carrying `resolves_before_vision` and
`missile_speed` precisely because a seat cannot plan against them otherwise.

A SNAP also **guards** the square it lands on for the rest of that hour,
and the two ways of arriving there resolve differently — worth knowing
before a fork plans a landing into contested ground. A harvester that
*steps* onto a SNAPped cell completes the step and is crippled on it. A
harvester that tries to *land* on one is refused: the drop does not
happen, the hull is damaged in orbit, and its one outing for the night
is unspent. Either way the square banks nothing that turn.

You read both off `combat_events`. The strike is public —
`{"type": "snap", "owner", "at", "hours"}` — and is recorded even when it
hits nothing, so "they spent a round on an empty square" is visible.
Whether it hit *you* arrives as `{"type": "snap_hit", "unit", "victim",
"by", "hours", "outcome"}`, private to the victim, with `outcome` either
`"crippled"` or `"landing_aborted"`. Do not infer one from the other:
both leave a damaged hull, but only the first leaves it on the board.

**Retired verbs are refused, not ignored (v1.31).** `mine_lay` — and its orbit
half `build_mine` — are rejected by `game/policy.py` with a named reason ("the
caltrop mine was retired in v1.31 — ... are the remaining weapons", with the
roster derived from the live price table rather than typed out),
and the row still burns one of the 21 slots. A fork that carries a stale tag
therefore loses a slot per occurrence and sees the reason on the card, which is
the whole point of refusing by name rather than dropping silently. Note the v7
sanitizer never filtered `mine_lay`, so nothing upstream of the engine will
catch it for you. RULEBOOK §4.9.4 records the retirement.

### 3.2 The read-only path

`run(..., submit=False)` runs the whole THINK → PLAN → PACKAGE → SANITIZE
pipeline and returns the trace **without calling the engine and without writing
memory or snapshots**. This is what `scripts/advise_v12.py` and the turn suite
use, and it is why a human can play a seat while asking "what would v12 do
here?" without disturbing the game.

---

## 4. Harness-owned state (NOT engine state)

These persist across turns and belong to the harness. The engine neither reads
nor validates them, so a bug here can mislead the agent but can never corrupt a
game.

| store | module | contents |
|---|---|---|
| `SOC_AGENT_MEMORY` | `memory.py` (v7), `journal.py` | per session+seat+day: agent-authored `intent`/`reflection`, plus engine truth folded in (`happened`, `actual_banked`, `probe_crushes`, chosen option IDs) |
| hazard memory | `hazard_memory.py` | the fog-surviving union of stripped/GREEN cells — monotonic, so it is a safe permanent "never drop or step here" set |
| frontier memory | `frontier.py` | which frontier cells have already been mined for exploration probes |
| weapon estimates | `opponent_weapons.py` (v7) | rival racks decoded from the **public** weaponised-blue total (§4.9.8). At the 100/200/300 prices most totals are ambiguous, so the answer is a *set* of racks — `est.racks` — and the seat holds **exactly one** of them. The `emps_max` / `chaff_max` / `snap_max` fields are independent marginals over that set: sound for "could this seat have any X" (`est.could_hold(kind)`), and **wrong if read together** — at 600 they read as 3 EMPs *and* 2 chaff, which is 1200 blue under a 600 cap (v1.39). Render `racks`, gate on `could_hold` |
| turn snapshots | `harness.py` / v7 recorder | turn-start score and probe counts, the anchor next turn's reflection is measured against |

**Why hazard memory is harness-side and not a view field:** the engine tells you
a cell is green only while you can see it. Green is monotonic — a stripped cell
never un-strips — so remembering it is sound inference from past views, not a
fog violation.

---

## 5. Invariants

1. **No engine edits.** Nothing in this package modifies `sea_of_colours/game/`
   or `snowpark/engine.py`. Behaviour changes happen by choosing different
   moves, never by changing what a move does.
2. **One board input.** Everything the agent knows arrives via `agent_view` or
   is derived from it plus this seat's own history. No reading another seat's
   view, no reading `session` directly.
3. **Two writes, both explicit.** `submit_policy` and `submit_orbit_actions`.
   Both are skipped entirely when `submit=False`.
4. **Constants are imported, not copied** (§2.3).
5. **The packager is legality-only.** It compiles chosen option IDs into wire
   moves and may refuse what the engine would reject — dropping without live
   coverage, walking onto known-green, spending probes or harvesters that do not
   exist, exceeding the hold cap. It must **not** author strategy: it does not
   shorten a route because it looks risky, and when it disagrees with a plan it
   *reports* and keeps rather than silently rewriting. Every intervention is
   surfaced in the packager log and appears on the next turn's card.
6. **Engine truth wins over agent belief.** Where the two disagree — banked
   totals, crushes, losses — `last_night.py` renders the frames, not the plan.

Invariant 5 is the one that has been broken most often and costs the most when
it is: a compiler that quietly edits plans makes the agent's reasoning
unfalsifiable, because you can no longer tell a bad decision from a good
decision that was overwritten.

---

## 6. Where to look when something is wrong

| symptom | first place to look |
|---|---|
| agent acted on something it could not see | `world_view.py` / `out_of_grid.py` — is a sign being rendered as a coordinate? |
| plan did not reach the board | packager log on the card; then `move_sanitizer` (v7) |
| memory says a night went well when it did not | `last_night.py` against `list_replay_frames` |
| repeated self-harm on green | `hazard_memory.py` union, and whether the caller used `_known_green_cells` |
| a rule/constant looks stale in the prompt | §2.3 — something hardcoded a copy |
