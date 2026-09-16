"""Board-fact formatters — the computation layer V12 reuses verbatim.

V12 forked v7's *feeding* layer (how a prompt is assembled into sections)
but kept its *computation* layer: the pure functions that turn an agent
view into text. That split is the whole reason this file exists inside a
harness named for a different version, and it is a deliberate one — these
formatters are battle-tested and the assembler above them is not the thing
that needed rewriting.

What lives here is only what ``tblasi_warden/prompt.py`` still imports:

  * ``format_state_block``, ``format_drop_legal_block``,
    ``format_fog_and_echo_block``, ``format_opponent_weapons_block``,
    ``format_supersede_hints_block``, ``format_setup_night_advisory``
  * ``format_reflect_block`` / ``format_last_night_block``, which v12
    wraps rather than replaces
  * ``_my_jam_events`` / ``_format_combat_event``, the combat-feed reads
  * ``_THINKER_SCHEMA``, the two-call output contract

v1.40 removed ~470 lines this file no longer answered for: its own
``build_prompt`` (superseded when v10 wrote the sectioned assembler) and
the standalone hint formatters (retired when v11 made the option menu the
single source of truth for actionable plays). None of it was reachable,
and all of it was misleading — the dead ``build_prompt`` still carried an
ungated EMP/chaff weapons check that predates the SNAP work and the
self-enforcing ``could_hold`` gate, so a grep for "where does the EMP
doctrine get attached" found the wrong answer first. Every attendee fork
copies this file, so a stale answer in a plausible place is expensive.

**Nothing here builds a prompt.** The assembler is
``tblasi_warden/prompt.py``; if you are looking for section order, doctrine
gating or the mode switch, it is there.
"""

from __future__ import annotations

import math
from typing import Any, List, Mapping, Sequence

from sea_of_colours.orchestrator_2.harnesses.tblasi_warden._v7.probe_hints import (
    _enemy_probe_cells,
)
from sea_of_colours.orchestrator_2.harnesses.tblasi_warden._v7.validators import (
    _synthetic_green_cells,
    _green_hazard_cells,
)


_THINKER_SCHEMA = """\
=== YOUR TASK (STRATEGIST) ===
You are the STRATEGIST for this night. Do NOT emit any moves. Your ONE job is to
DECIDE the night's POSTURE and the 1-3 cells the mover should prioritise, using
LAST NIGHT / REFLECT / OPPONENT INTEL / hints above.

Return ONE JSON object with these fields, IN THIS EXACT ORDER — the DECISION
comes FIRST, a short rationale LAST:
  {
    "posture": "aggressive|defensive|redsign_race|final_convert",
    "targets": [[x,y], ...],
    "chaff_react": true|false,
    "avoid": [[x,y], ...],
    "note": "short phrase for the mover",
    "reasoning": "1-3 SHORT sentences — why this call. NOT a chain-of-thought."
  }

DECIDE FIRST, then justify — do NOT open with a long chain-of-thought. Emit the
decision fields at the very start of the object; keep "reasoning" to a couple of
sentences at the END. (A rambling reasoning-first answer gets truncated before
the decision lands and is thrown away — a decisive short answer always wins.)

The decision fields:
  * posture: pick ONE.
      - defensive / chaff_react=true → shorten chains, pick up early (an
        opponent has chaff or chaffed us).
      - redsign_race → a public pure-RED beacon is worth racing/contesting.
      - final_convert → final night: convert everything, no frontier probes.
      - aggressive → default: harvest the richest reachable RED.
  * targets / avoid: 0-3 [x,y] cells each (anchors, not full paths).

Weigh these BEFORE you write (in your head — put only the conclusion in
"reasoning"): did last night's prediction miss and why (chaff? held-not-lost
hoard?); which RED / redsign / hot-drop is worth the most REACHABLE points; is
an opponent EMP/chaff-capable near a target; if contested, short direct comb /
commit more harvesters / stage behind a fresh probe / SUPERSEDE the enemy probe
on the beacon to deny it. Then commit. The mover acts on your decision — make
the call decisive.
"""


def format_state_block(
    agent_view: Mapping[str, Any],
    *,
    day: int,
    day_cap: int,
    vault_score: int,
) -> str:
    """Compact YOUR STATE block. Only fields phase 1 uses."""
    entities = (agent_view.get("entities") or {}).get("mine") or []
    harvesters = [
        {
            "id": e.get("id"),
            "state": "surface" if e.get("pos") else "orbit",
            "at": e.get("pos"),
        }
        for e in entities
        if isinstance(e, Mapping) and str(e.get("type") or "") == "harvester"
    ]
    probes = [
        {
            "id": e.get("id"),
            "at": e.get("pos"),
            "nights_remaining": e.get("nights_remaining"),
        }
        for e in entities
        if isinstance(e, Mapping) and str(e.get("type") or "") == "probe"
    ]

    # Held-but-unshipped RED. `vault_score` counts SHIPPED parcels only;
    # a harvest banks to the HOARD first and scores 0 until the next orbit
    # settles. v1.13 — that settlement is automatic and unconditional, so
    # held RED is banked score in waiting, not a pending decision.
    # Surfacing it stops the agent from reading an unchanged vault_score
    # after a good harvest as "pickup failed / cargo lost" (the day-4
    # S2024 reflection bug).
    hoard = (agent_view.get("hud") or {}).get("hoard") or {}
    held_count = int(hoard.get("count") or 0)
    held_red_value = int(hoard.get("red_value") or 0)
    hoard_line = (
        f"  hoard_red_value: ~{held_red_value} pts held in {held_count} "
        f"parcel(s) — HARVESTED but not yet settled, so it counts 0 toward "
        f"vault_score until the next orbit. Settlement is AUTOMATIC: this "
        f"WILL ship and score, guaranteed. If vault_score did not move "
        f"after a harvest but the hoard grew, the harvest SUCCEEDED and "
        f"the cargo is HELD (not lost).\n"
    )

    return (
        f"YOUR STATE (day {day} of {day_cap}):\n"
        f"  vault_score: {int(vault_score)}  (SHIPPED parcels only — this is your score)\n"
        f"{hoard_line}"
        f"  harvesters_alive: {harvesters}\n"
        f"  probes_alive: {probes}\n"
    )


def format_setup_night_advisory(
    agent_view: Mapping[str, Any],
    day: int,
    day_cap: int,
) -> str:
    """Big fat advisory the prompt prepends when the agent has NO vision.

    Fires when the visible world is entirely fog — i.e. no visible RED
    cells AND no friendly probes on the surface. This is the "setup
    night" situation: the ONLY legal productive action is to launch
    probes. Drops fail because there is no live-vision cell to land on,
    steps do nothing without a harvester on the surface, and pickups
    are impossible without a harvester holding cargo. Say all of this
    to the agent so it doesn't burn hours trying illegal drops or
    passing the night.

    Returns an empty string when the agent DOES have vision (probes
    alive OR any red_tiles visible OR any friendly surface unit) so
    the advisory does not fire spuriously on days 2+.
    """
    red_visible = len(agent_view.get("red_tiles") or [])
    entities = (agent_view.get("entities") or {}).get("mine") or []
    friendly_probes = [
        e for e in entities
        if isinstance(e, Mapping) and str(e.get("type") or "") == "probe"
        and e.get("pos")  # on the surface
    ]
    friendly_surface_units = [
        e for e in entities
        if isinstance(e, Mapping)
        and str(e.get("type") or "") in ("harvester", "probe")
        and e.get("pos")
    ]
    if red_visible > 0 or friendly_probes or friendly_surface_units:
        return ""

    probe_stock = int((agent_view.get("orbit") or {}).get("probe_stock") or 0)
    world = agent_view.get("world") or {}
    fog_count = int(world.get("fog_count") or 0)

    lines = [
        "!!! SETUP NIGHT — YOU HAVE NO VISION YET !!!",
        "",
        f"  Every cell on the {world.get('width', '?')}x{world.get('height', '?')} "
        f"grid is fog. red_visible=0, no friendly probes are on the surface, "
        f"no harvester is on the surface. fog_count={fog_count}.",
        "",
        f"  You have probe_stock={probe_stock}. A probe reveals a Euclidean "
        f"radius-4 disk (~49 cells) for 3 nights.",
        "",
        "  A COLD drop (drop without a probe already down) will be REJECTED —"
        " the cell isn't in live vision. BUT a HOT DROP is legal on night 1:",
        "     hour 1: probe(at=[x,y])       → makes the 4-radius disk live",
        "     hour 2: drop(harvester at=[x',y']) where (x',y') is inside the disk",
        "     hour 3+: step chain + pickup",
        "  This is the setup-night harvest opportunity — do NOT skip it.",
        "",
        "  RECOMMENDED SETUP-NIGHT PLAN (attempt harvest, don't just probe):",
        "    * At LEAST 2 probes total. Setup night is the ONE night you"
        " have full probe magazine and no better use for hours — spread"
        " probes across DIFFERENT quadrants.",
        "    * ATTEMPT AT LEAST ONE HOT DROP. Best target: a bluesign"
        " hotspot (public intensity map, visible from day 1 in the BLUE"
        " HINTS block below — bright cells have real blue nearby). A"
        " bluesign hot drop is not blind — the intensity signal predicts"
        " where value is. Random-probe hot drops are also legal but blinder.",
        "    * Hot-drop sequence: probe(bluesign_cell) → drop(harvester"
        " into disk) → step 1-3 → pickup, all within the same night's 21"
        " hours.",
        "    * SUBMIT NON-EMPTY moves. Passing setup night wastes the season"
        " — nights 2, 3, 4 will have less to harvest.",
        "    * Cold drops (drop without a preceding probe covering the"
        " cell) WILL be rejected by the engine — always probe first if you"
        " want to drop somewhere.",
        "",
        "!!! End setup-night advisory !!!",
    ]
    return "\n".join(lines) + "\n"


def _euclidean_drop_rows(
    cx: int, cy: int, r: int, width: int, height: int,
) -> List[tuple]:
    """Row-by-row x-ranges of the Euclidean radius-``r`` disk around
    ``(cx, cy)``, clamped to the board.

    This MUST match the engine's live-vision disk
    (:func:`sea_of_colours.game.session._euclidean_disk`), which is what
    gates a legal drop (``tiles_visible_now`` membership). A cell is in
    the disk iff ``dx*dx + dy*dy <= r*r`` — i.e. for each row ``dy`` the
    legal x-span is ``[cx - floor(sqrt(r^2 - dy^2)), cx + ...]``. This is
    NARROWER than the Chebyshev 9x9 box: the four box corners are fog and
    a drop there is rejected.
    """
    rows: List[tuple] = []
    for dy in range(-r, r + 1):
        y = cy + dy
        if not (0 <= y < height):
            continue
        dx = int(math.isqrt(r * r - dy * dy))
        xmin = max(0, cx - dx)
        xmax = min(width - 1, cx + dx)
        if xmin > xmax:
            continue
        rows.append((y, xmin, xmax))
    return rows


def format_drop_legal_block(agent_view: Mapping[str, Any]) -> str:
    """Explicit list of where a drop is legal THIS turn.

    Engine rule: a drop cell must be (a) inside the **Euclidean radius-4
    disk** of an active friendly probe (the ~49-cell live-vision disk —
    NOT the 81-cell Chebyshev box; the four corners of the 9x9 bounding
    box are fog and the engine rejects a drop there), OR (b) on a
    friendly harvester's tile or one of its four Manhattan-1 neighbours
    (the "plus"). Echo-only cells are NOT legal.

    Reasoning about probe expiry + disk math is exactly the kind of
    thing the model gets wrong under time pressure (a night-1 hot-drop at
    (36,20) off a probe at (33,17) was rejected because (36,20) is a box
    corner outside the Euclidean disk, crashing the whole chain).
    Rendering the exact per-row legal x-ranges means the LLM only has to
    CHECK, not COMPUTE.
    """
    entities = (agent_view.get("entities") or {}).get("mine") or []
    world = agent_view.get("world") or {}
    width = int(world.get("width") or 40)
    height = int(world.get("height") or 28)
    lines: List[str] = []

    # Hazards to steer drops away from, and enemy vision to warn about.
    bad_cells = _synthetic_green_cells(agent_view) | _green_hazard_cells(agent_view)
    enemy_probes = [
        r["at"] for r in _enemy_probe_cells(agent_view)
        if isinstance(r.get("at"), tuple)
    ]

    def _under_enemy_vision(x: int, y: int) -> "tuple | None":
        for ex, ey in enemy_probes:
            if (x - ex) * (x - ex) + (y - ey) * (y - ey) <= 16:
                return (ex, ey)
        return None

    # Active probes.
    probe_lines: List[str] = []
    for e in entities:
        if not isinstance(e, Mapping):
            continue
        if str(e.get("type") or "") != "probe":
            continue
        nr = e.get("nights_remaining")
        if isinstance(nr, (int, float)) and int(nr) <= 0:
            continue
        pos = e.get("pos") or e.get("at")
        if not (isinstance(pos, (list, tuple)) and len(pos) == 2):
            continue
        try:
            cx, cy = int(pos[0]), int(pos[1])
        except (TypeError, ValueError):
            continue
        pid = str(e.get("id") or "probe")
        rows = _euclidean_drop_rows(cx, cy, 4, width, height)
        range_str = ", ".join(
            f"y={y}:[{xmin},{xmax}]" for (y, xmin, xmax) in rows
        )
        nr_txt = f" [{int(nr)}n]" if isinstance(nr, (int, float)) else ""
        # Terse per-probe line — the disk/crush rules live in the header.
        probe_lines.append(f"  {pid}@({cx},{cy}){nr_txt}: {range_str}")
        # Only surface annotations that actually apply (keeps the block short).
        disk_cells = [
            (x, y) for (y, xmin, xmax) in rows for x in range(xmin, xmax + 1)
        ]
        avoid_green = sorted(c for c in disk_cells if c in bad_cells)
        if avoid_green:
            probe_lines.append(f"      ! AVOID (green -100): {avoid_green}")
        watched = sorted(
            {c for c in disk_cells if _under_enemy_vision(c[0], c[1])}
        )
        if watched:
            probe_lines.append(f"      ! WATCHED by enemy probe: {watched}")

    # Surface harvesters — the plus (self + 4 Manhattan-1 neighbours) is
    # also drop-legal (rule of adjacency to a friendly unit).
    harv_lines: List[str] = []
    for e in entities:
        if not isinstance(e, Mapping):
            continue
        if str(e.get("type") or "") != "harvester":
            continue
        pos = e.get("pos") or e.get("at")
        if not (isinstance(pos, (list, tuple)) and len(pos) == 2):
            continue
        try:
            hx, hy = int(pos[0]), int(pos[1])
        except (TypeError, ValueError):
            continue
        hid = str(e.get("id") or "harvester")
        plus = [(hx, hy), (hx+1, hy), (hx-1, hy), (hx, hy+1), (hx, hy-1)]
        harv_lines.append(
            f"  {hid} plus @ {plus}"
        )

    if not probe_lines and not harv_lines:
        return (
            "DROP-LEGAL ZONES: NONE right now. No active probe and no "
            "harvester on the surface — but you CAN hot-drop: launch a "
            "probe at (cx,cy) at hour K, then at hour K+1 drop inside its "
            "Euclidean radius-4 disk (dx*dx+dy*dy<=16, ~49 cells; the 9x9 "
            "box corners are fog and get rejected). Land ADJACENT to the "
            "centre (an in-disk neighbour) — do NOT drop on (cx,cy) itself "
            "or you CRUSH the probe you just paid for. Sequence in moves[] "
            "matters (probe FIRST). Probes are always legal.\n"
        )

    # Rules stated ONCE here (not repeated per probe) to keep the block
    # short. Per-probe lines are just: id@(cx,cy)[Nn]: legal x-ranges.
    lines.append(
        "DROP-LEGAL ZONES — every drop.at MUST be inside AT LEAST ONE zone "
        "below or the engine rejects it. Each probe's legal cells are its "
        "Euclidean r4 disk (dx^2+dy^2<=16, ~49 cells; 9x9 box CORNERS are "
        "fog=rejected). Format: id@(cx,cy)[Nn left]: y=<row>:[xmin,xmax]. "
        "Dropping OR stepping on a probe's own (cx,cy) CRUSHES it (lose its "
        "remaining vision) — land ADJACENT unless the loot IS on (cx,cy). "
        "'! AVOID' = green/harvested (-100); '! WATCHED' = an enemy probe "
        "sees it (collision/EMP risk)."
    )
    if probe_lines:
        lines.append("  active probe disks:")
        lines.extend(probe_lines)
    if harv_lines:
        lines.append("  surface harvester plus-cells (drop on unit or its 4 neighbours):")
        lines.extend(harv_lines)
    lines.append(
        "  HOT DROP: a NEW probe you launch at hour K is a drop-legal disk "
        "from hour K+1 — probe FIRST in moves[], then drop ADJACENT to its "
        "centre."
    )
    return "\n".join(lines) + "\n"


def format_fog_and_echo_block(agent_view: Mapping[str, Any]) -> str:
    """Expose the fog map + echo hints so the LLM can reason about what
    it CAN'T see yet — the prerequisite for making probe decisions.

    Shows fog_count, up to 2 largest fog_clusters (centroid + nearest
    visible edge + size), and up to 5 best_red_echo cells if any.
    """
    world = agent_view.get("world") or {}
    fog_count = int(world.get("fog_count") or 0)
    probe_stock = int((agent_view.get("orbit") or {}).get("probe_stock") or 0)

    clusters = [c for c in (agent_view.get("fog_clusters") or []) if isinstance(c, Mapping)]
    clusters_sorted = sorted(clusters, key=lambda c: int(c.get("size") or 0), reverse=True)[:2]

    echo = [
        row for row in (((agent_view.get("navigation") or {}).get("best_red_echo")) or [])
        if isinstance(row, Mapping)
    ][:5]

    lines = ["FOG + ECHO (what you CAN'T see):"]
    lines.append(f"  fog_count: {fog_count}  probe_stock: {probe_stock}")
    if clusters_sorted:
        lines.append("  top_fog_clusters:")
        for c in clusters_sorted:
            centroid = c.get("centroid") or [None, None]
            nve = c.get("nearest_visible_edge") or [None, None]
            size = c.get("size")
            lines.append(
                f"    centroid=({centroid[0]},{centroid[1]}) size={size} "
                f"nearest_visible_edge=({nve[0]},{nve[1]})"
            )
    else:
        lines.append("  top_fog_clusters: (none — you see everything)")
    if echo:
        lines.append("  best_red_echo (traces beyond LOS — a probe here reveals real cells):")
        for row in echo:
            lines.append(
                f"    ({row.get('x')},{row.get('y')}) purity_est={row.get('purity') or row.get('value')}"
            )
    else:
        lines.append("  best_red_echo: (no echoes surfaced this turn)")
    return "\n".join(lines) + "\n"


def format_supersede_hints_block(hints: Sequence[Mapping[str, Any]]) -> str:
    """FINAL-NIGHT ONLY — enemy probe cells worth superseding with spare
    probe stock. Caller passes [] on non-final nights."""
    if not hints:
        return ""
    lines = [
        "SUPERSEDE HINTS (FINAL NIGHT — after your harvest chains are "
        "placed, spend any LEFTOVER probe stock landing ON these enemy "
        "probe cells to destroy their vision; your probe survives):"
    ]
    for i, h in enumerate(hints):
        at = h.get("probe_at") or [None, None]
        lines.append(
            f"  Target {chr(ord('A')+i)}: probe at=({at[0]},{at[1]}) "
            f"— supersedes an enemy probe (seen day {h.get('day_seen', '?')})"
        )
    return "\n".join(lines) + "\n"


def format_last_night_block(agent_view: Mapping[str, Any]) -> str:
    """Show what actually happened last night (from the engine's perspective).

    Renders the engine's ground-truth record so the agent can reflect on
    plan vs actual. The engine's ``last_night`` block includes:
      * ``my_orders``: each order you submitted with outcome ("ok" or
        "illegal") and a reason if illegal — this is the single source
        of truth for "did my move even land?"
      * ``my_parcels_banked``: which cells actually banked, with tile
        and purity — this tells you what harvest ACTUALLY yielded.
      * ``my_assets_destroyed``: probes / harvesters killed by opponent
        weapons, with reason (e.g. ``emp_p2``). Read this to decide
        whether to trigger the RECOVERY RE-PROBE exception.
      * ``combat_events``: opponent actions that affected you.
    """
    ln = agent_view.get("last_night") or {}
    if int(ln.get("day_ended") or 0) == 0:
        return ""

    orders = ln.get("my_orders") or []
    destroyed = ln.get("my_assets_destroyed") or []
    banked = ln.get("my_parcels_banked") or []
    combat = ln.get("combat_events") or []

    if not (orders or destroyed or banked or combat):
        return ""

    lines = [f"LAST NIGHT (day {ln.get('day_ended')} — engine record):"]

    # my_orders: per-order truth. The engine tags each with outcome.
    if orders:
        ok = sum(1 for o in orders if isinstance(o, Mapping) and o.get("outcome") == "ok")
        illegal = sum(1 for o in orders if isinstance(o, Mapping) and o.get("outcome") == "illegal")
        lines.append(f"  my_orders ({len(orders)} submitted, {ok} ok, {illegal} illegal):")
        for o in orders[:12]:  # cap to keep prompt bounded
            if not isinstance(o, Mapping):
                continue
            outcome = o.get("outcome", "?")
            text = str(o.get("text") or "").strip()
            # engine's text is free-form ("p1 deployed probe at (7,4)"),
            # keep it short so 12 lines fit comfortably in the prompt.
            if len(text) > 90:
                text = text[:87] + "..."
            line = f"    - {text} → {outcome}"
            if outcome == "illegal" and o.get("reason"):
                rsn = str(o.get("reason") or "").strip()
                if rsn != text and len(rsn) < 100:
                    line += f" (reason: {rsn})"
            lines.append(line)
        if len(orders) > 12:
            lines.append(f"    ... ({len(orders) - 12} more orders truncated)")

    # my_parcels_banked: per-parcel detail so the agent knows exactly
    # which cells produced value. Aggregated by tier for context.
    if banked:
        by_tier: Dict[str, int] = {}
        for b in banked:
            if isinstance(b, Mapping):
                t = str(b.get("tier") or b.get("tile") or "?")
                by_tier[t] = by_tier.get(t, 0) + 1
        lines.append(f"  my_parcels_banked ({len(banked)}) by tier: {by_tier}")
        for b in banked[:8]:
            if not isinstance(b, Mapping):
                continue
            frm = b.get("from") or [None, None]
            lines.append(
                f"    - ({frm[0]},{frm[1]}) {b.get('tile','?')} "
                f"purity={b.get('purity','?')} tier={b.get('tier','?')} "
                f"by={b.get('harvester_id','?')}"
            )
        if len(banked) > 8:
            lines.append(f"    ... ({len(banked) - 8} more parcels)")

    # my_assets_destroyed: opponent-caused losses. Critical for the
    # RECOVERY RE-PROBE exception — if a probe died before you got
    # value from its disk, re-probing that area IS allowed.
    if destroyed:
        lines.append(f"  my_assets_destroyed ({len(destroyed)}):")
        for d in destroyed[:8]:
            if isinstance(d, Mapping):
                at = d.get("at") or []
                at_str = f" at ({at[0]},{at[1]})" if at and at[0] is not None else ""
                lines.append(
                    f"    - {d.get('id') or d.get('kind') or '?'}"
                    f"{at_str} reason={d.get('reason', '?')}"
                )

    if combat:
        lines.append(f"  combat_events ({len(combat)}):")
        for ev in combat[:8]:
            lines.append("    - " + _format_combat_event(ev))

    return "\n".join(lines) + "\n"


def _format_combat_event(ev: Mapping[str, Any]) -> str:
    """Human-readable one-liner for a single last-night combat event.

    Renders the event types that actually reach the agent view
    (:func:`snowpark.view._last_night_recap`): the VICTIM-private
    ``chaff_jam`` / ``emp_hit`` / ``snap_hit`` (these mean YOU were hit)
    and the PUBLIC ``chaff`` flare / ``emp`` salvo / ``snap`` strike.
    Anything else is shown as-is.
    """
    if not isinstance(ev, Mapping):
        return str(ev)
    etype = str(ev.get("type") or "?")
    hours = ev.get("hours") or []
    hrs = f" at hours {list(hours)}" if hours else ""
    if etype == "chaff_jam":
        by = ev.get("by") or []
        units = ev.get("units") or []
        who = f" by {list(by)}" if by else ""
        unit_txt = f" (jammed: {list(units)})" if units else ""
        return (
            f"YOU WERE CHAFFED{who}{hrs}{unit_txt} — those action-slots were "
            f"CANCELLED (a pickup in that window is lost -> dawn-crash risk)."
        )
    if etype == "emp_hit":
        by = ev.get("by") or []
        at = ev.get("at") or []
        at_txt = f" near ({at[0]},{at[1]})" if at and at[0] is not None else ""
        who = f" by {list(by)}" if by else ""
        return (
            f"YOU WERE EMP'd{who}{at_txt}{hrs} — a unit was disabled ~8h "
            f"(it keeps its haul; only a dawn crash kills it)."
        )
    # v1.38 — SNAP's two halves. Without these the events fell through
    # to the raw-dict branch below and the model was handed a Python
    # repr where every other weapon gets a sentence.
    if etype == "snap_hit":
        by = ev.get("by") or []
        who = f" by {list(by)}" if by else ""
        unit = ev.get("unit")
        unit_txt = f" ({unit})" if unit else ""
        if str(ev.get("outcome") or "") == "landing_aborted":
            return (
                f"YOUR LANDING WAS REFUSED{who}{hrs}{unit_txt} — a SNAP had "
                f"the square first. The harvester is STILL IN ORBIT and "
                f"DAMAGED; its outing was NOT spent, so it can go again, "
                f"but a damaged hull cannot harvest until repaired (500c)."
            )
        return (
            f"YOU WERE SNAPPED{who}{hrs}{unit_txt} — the harvester is "
            f"DAMAGED on the surface and harvested NOTHING that night "
            f"(no auto-harvest either). Repair costs 500c."
        )
    if etype == "snap":
        at = ev.get("at") or []
        at_txt = f" at ({at[0]},{at[1]})" if at and at[0] is not None else ""
        return (
            f"SNAP fired by {ev.get('owner','?')}{at_txt}{hrs} (public) — "
            f"that cell was taken before anything else resolved on it."
        )
    if etype == "chaff":
        return f"chaff flare by {ev.get('owner','?')}{hrs} (public)."
    if etype == "emp":
        return f"EMP salvo{hrs} (public)."
    return f"{etype} {dict(ev)}"


#: The victim-private half of each weapon's combat feed — the events
#: that mean "this landed on ME", as opposed to the public flare saying
#: someone fired. v1.38 added ``snap_hit``; without it a seat could be
#: SNAPped every night and never react, because this list was the only
#: thing telling the prompt it had been hit.
_HIT_ME = ("chaff_jam", "emp_hit", "snap_hit")


def _my_jam_events(agent_view: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Last-night events that actually HIT me.

    combat_events is already victim-filtered by the view builder, so the
    presence of one of :data:`_HIT_ME` here means I was the victim. Used
    to force the relevant BEWARE doctrine + a concrete warning even when
    the inference tracker is cold.
    """
    out: List[Mapping[str, Any]] = []
    ln = agent_view.get("last_night") or {}
    for ev in (ln.get("combat_events") or []):
        if isinstance(ev, Mapping) and str(ev.get("type") or "") in _HIT_ME:
            out.append(ev)
    return out


def format_reflect_block(
    agent_view: Mapping[str, Any],
    prior_day_entry: "Mapping[str, Any] | None",
    day: int,
) -> str:
    """Force a GROUNDED reflection by handing the agent the engine's real
    prior-night numbers instead of letting it narrate its own plan.

    The prior audit found ``reflection_on_last_night`` was frequently null
    or hallucinated (it "banked ~medium" when the engine banked 0, never
    acknowledged a chaff loss, etc.). The fix is mechanical: surface the
    exact prediction-vs-actual delta and every named loss (chaff/EMP/crush/
    destroyed asset) so the LLM has nothing to guess — it just echoes the
    number and explains the gap. Empty on night 1 (no prior night).
    """
    if int(day) <= 1 or not prior_day_entry:
        return ""
    prior_day = int(prior_day_entry.get("day") or (int(day) - 1))
    predicted = str(
        (prior_day_entry.get("predicted_outcome") or {}).get(
            "banked_pts_estimate"
        )
        or "?"
    )
    actual = prior_day_entry.get("actual_banked")

    # Collect every concrete loss the engine attributes to last night.
    losses: List[str] = []
    for ev in _my_jam_events(agent_view):
        losses.append(_format_combat_event(ev))
    for c in (prior_day_entry.get("probe_crushes") or []):
        losses.append(str(c))
    ln = agent_view.get("last_night") or {}
    for d in (ln.get("my_assets_destroyed") or []):
        if isinstance(d, Mapping):
            at = d.get("at") or []
            at_s = f" at ({at[0]},{at[1]})" if at and at[0] is not None else ""
            losses.append(
                f"{d.get('id') or d.get('kind') or 'asset'}{at_s} destroyed "
                f"(reason={d.get('reason', '?')})"
            )

    # Parcels the engine actually harvested into the HOARD last night, with
    # a tier-weighted RED value estimate. This is the "did my harvest land?"
    # signal — separate from SHIPPED-score change, because a fresh haul sits
    # in the hoard (scoring 0) until the next orbit settles it. Surfacing
    # it stops the agent from reading a still vault_score as "pickup failed".
    _TIER_MULT = {"trace": 0.75, "vein": 1.0, "mass": 1.5, "pure": 3.0}
    banked = ln.get("my_parcels_banked") or []
    red_banked = [b for b in banked if isinstance(b, Mapping)
                  and str(b.get("tile") or "").upper() == "RED"]
    hoard_red_value = 0
    for b in red_banked:
        try:
            pur = int(b.get("purity") or 0)
        except (TypeError, ValueError):
            pur = 0
        hoard_red_value += int(round(pur * _TIER_MULT.get(
            str(b.get("tier") or "").lower(), 1.0)))

    lines = [f"REFLECT ON LAST NIGHT (day {prior_day}) — engine ground truth:"]
    if actual is not None:
        lines.append(
            f"  you predicted \"{predicted}\"; SHIPPED-score change last "
            f"night = {int(actual)} pts (this is the only thing that scores)."
        )
    else:
        lines.append(
            f"  you predicted \"{predicted}\"; shipped-score change not yet "
            f"resolved."
        )
    if banked:
        lines.append(
            f"  you HARVESTED {len(banked)} parcel(s) into the hoard "
            f"(~{hoard_red_value} pts of RED). These are HELD, not scored — "
            f"they count at the next orbit, which settles AUTOMATICALLY. A 0 "
            f"shipped-change with a healthy harvest is a SUCCESS awaiting "
            f"settlement, NOT a lost pickup — do NOT invent an EMP/chaff loss."
        )
    if losses:
        lines.append("  losses the engine recorded (you MUST acknowledge these):")
        for lz in losses[:5]:
            lines.append(f"    - {lz}")
    lines.append(
        "  -> Fill reflection_on_last_night: set actual to the EXACT number "
        "above — the harvested hoard value if you harvested (even if it has "
        "NOT shipped yet), else the shipped-score change. gap_reason must "
        "name any loss; if the harvest banked but vault_score did not move, "
        "say 'held in hoard, not yet shipped' — do NOT report it as lost "
        "cargo. Do NOT leave it null."
    )
    return "\n".join(lines) + "\n"


def format_opponent_weapons_block(
    estimates: "Mapping[str, Any] | None",
) -> str:
    """Render what each opponent is holding.

    Shows every opponent holding ANY ordnance.

    v1.31 — this used to filter out a third, mine row that the estimator
    tracked but the doctrine never acted on. The estimator no longer
    tracks it, so the filter here is just "is there anything to warn
    about", not a curation of what to hide.

    v1.34 — these stopped being estimates. The engine broadcasts every
    seat's weaponised blue (§4.9.8), so the header no longer claims
    inference.

    v1.38 — the block was giving the model a false picture in two ways,
    both of which came from rendering the per-weapon marginals.

    The filter asked for ``emps_max > 0 or chaff_max > 0``, so a seat
    holding 100 blue — a lone SNAP, and SNAP is the weapon that takes a
    square out from under a landing — was dropped from the prompt
    entirely and read as unarmed.

    Worse, the line itself read ``emp=[0..3] chaff=[0..2]`` for a seat at
    600, which is two independent ranges printed side by side. Nothing in
    that string says they are alternatives, so the natural reading is
    "up to three EMPs *and* up to two chaff" — a 1200-blue rack under a
    600-blue cap. It is not a rounding error, it is double the truth, and
    it is the reading that makes an agent cower. The seven racks that
    actually fit 600 include exactly one holding both an EMP and a chaff.

    So the block now states the total, the ladder that prices it and the
    racks themselves, with the exclusivity said out loud. The list is
    short by construction — at the shipped 1-2-3 prices a total admits at
    most seven racks — so there is no reason to compress a handful of
    true answers into a box of mostly impossible ones.
    """
    if not estimates:
        return ""
    interesting = []
    for est in estimates.values():
        has_any = getattr(est, "has_any", None)
        if callable(has_any):
            if has_any():
                interesting.append(est)
        elif (  # pragma: no cover — a fork's own estimate shape
            getattr(est, "emps_max", 0) > 0
            or getattr(est, "chaff_max", 0) > 0
        ):
            interesting.append(est)
    if not interesting:
        return ""
    lines = [
        "OPPONENT ARSENALS (public — the engine broadcasts every seat's "
        "weaponised blue exactly; these are read, not guessed):",
    ]
    ladder = _price_ladder_line(interesting)
    if ladder:
        lines.append(f"  {ladder}")
    for est in interesting:
        summary = getattr(est, "summary", None)
        if callable(summary):
            lines.append(f"  {summary()}")
        else:  # pragma: no cover — a fork's own estimate shape
            lines.append(
                f"  {est.seat}: "
                f"emp=[{est.emps_min}..{est.emps_max}] "
                f"chaff=[{est.chaff_min}..{est.chaff_max}]"
            )
        audit = list(getattr(est, "inferences", []) or [])
        for a in audit[-2:]:
            lines.append(f"    · {a}")
    if any(not _is_exact(est) for est in interesting):
        lines.append(
            "  READ THIS RIGHT: where a seat lists several racks it holds "
            "EXACTLY ONE of them — they are alternatives, not a shopping "
            "list. The blue total is exact and the cap is hard, so adding "
            "two of the listed racks together describes something no seat "
            "can own. Plan against the racks that would actually hurt your "
            "plan, and note which weapons appear in NONE of them: those "
            "the seat provably does not have."
        )
    return "\n".join(lines) + "\n"


def _is_exact(est: Any) -> bool:
    probe = getattr(est, "is_exact", None)
    return bool(probe()) if callable(probe) else True


def _price_ladder_line(estimates: Sequence[Any]) -> str:
    """State what each weapon costs, so the totals above can be reasoned on.

    v1.38 — without this the model is shown a blue figure and a list of
    racks and has no way to check one against the other, or to work out
    what a seat could still buy. Read off the estimate's own ``prices``,
    which is the table the board was played on rather than today's
    dials, so a legacy season is explained with its own ladder.
    """
    prices: Mapping[str, int] = {}
    cap = 0
    for est in estimates:
        prices = getattr(est, "prices", None) or prices
        cap = max(cap, int(getattr(est, "cap", 0) or 0))
    if not prices:
        return ""
    ladder = ", ".join(
        f"{kind} {cost}"
        for kind, cost in sorted(prices.items(), key=lambda kv: kv[1])
    )
    cap_txt = f"; no seat may hold more than {cap} blue of it" if cap else ""
    return f"prices in blue: {ladder}{cap_txt}."
