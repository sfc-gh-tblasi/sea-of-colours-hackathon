"""Deterministic opponent read — enemy vision map, aggression, and stance.

This module answers three questions with NO LLM call, purely from the fogged
``agent_view`` plus this seat's own public-signal history:

1. **What can the enemy SEE — now, and what did they USED to see?**
   An enemy probe launch is PUBLIC (RULEBOOK §3.15): the orbital station reports
   where a rival's probe landed even through fog. A probe lights a Euclidean
   radius-4 disk (the drop-legal / live-vision shape, :func:`_vision_disk`) for
   ``probe_lifetime_nights`` nights. So from the public launch markers we can
   reconstruct the rival's vision footprint:
     * ``live``  — cells a rival probe covers whose launch is still within its
       lifetime window: the enemy SEES these cells this night.
     * ``echo``  — cells a rival probe once covered but whose probe has expired:
       the enemy USED TO see these (and may hold stale belief about them), but
       is blind there now.
     * everything else is ``dark`` to the enemy.
   The distinction is the whole point of the module (per the design ask): a seam
   an opponent can see NOW is contested and a drop there may be met; a seam they
   only USED to see is one you can work while their picture goes stale.

2. **How aggressive is the field?**  A 0..1 score folded from public signals —
   weapons fired at me (strongest), a rival's weaponised-blue arming, weapons
   they *could* hold, orbital probe cadence (scouting), and how much of MY own
   footprint their live probes are watching. Mapped to passive|neutral|aggressive.

3. **Should I play eco or attack this turn?**  A stance derived from the
   aggression read, how heavily I am watched, the season clock, and (wired in a
   later step) my own means to fight back.

Everything here is a *read*. It authors no moves and writes no game state; it is
consumed by the prompt (as a terse OPPONENT READ block) and by the orbit/menu
levers. It never raises on a malformed view — a bad read must degrade to
"nothing known", never crash a turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from sea_of_colours.orchestrator_2.harnesses.tblasi_warden import frontier
from sea_of_colours.orchestrator_2.harnesses.tblasi_warden._v7 import (
    opponent_weapons,
)
from sea_of_colours.orchestrator_2.harnesses.tblasi_warden._v7.probe_hints import (
    _enemy_probe_cells,
    _friendly_probe_positions,
    _grid_dims,
    _los_cells,
    _vision_disk,
)

Cell = Tuple[int, int]

# Engine constant, imported not copied (ENGINE_INTERFACE §2.3 / invariant 4).
# We still PREFER the per-game value on ``meta.rules`` because an archived
# season may have been played on a different lifetime; the import is the
# fallback when a hand-built view carries no meta block.
try:  # pragma: no cover - trivial import guard
    from sea_of_colours.game.tuning import probe_lifetime_nights as _ENGINE_LIFETIME
except Exception:  # pragma: no cover
    _ENGINE_LIFETIME = None

_DEFAULT_LIFETIME = 3

# Hit events in ``combat_events`` that are private to the victim — the honest
# "a weapon landed on ME" signal (ENGINE_INTERFACE §3.1). We match by suffix so
# a fourth weapon's ``*_hit`` is counted without an edit here.
_HIT_SUFFIX = "_hit"

# Aggression component weights. They sum to 1.0 so the score stays in 0..1.
# Ordered by how directly each signal proves intent to harm THIS seat:
#   fired-at-me  > weapons-fired-anywhere > arming > watching-me > scouting.
_W_HITS = 0.30      # a weapon actually resolved on one of my units
_W_FIRED = 0.20     # opponents launched ordnance last night (at anyone)
_W_ARMED = 0.20     # weaponised blue sitting in a rival rack
_W_EYES = 0.15      # fraction of MY footprint their live probes watch
_W_SCOUT = 0.15     # orbital probe cadence (map pressure)

_PASSIVE_BELOW = 0.30
_AGGRESSIVE_AT = 0.65


# ──────────────────────────────────────────────────────────────────────────
# Enemy vision map
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class EnemyVision:
    """The rival vision footprint reconstructed from public probe launches.

    ``live`` and ``echo`` are disjoint by construction (a cell a live probe
    covers is never also counted as echo). ``ever`` is their union: every cell
    the enemy has had eyes on at some point this season.
    """

    live: frozenset  # cells the enemy SEES this night
    echo: frozenset  # cells the enemy USED TO see (probe expired)
    ever: frozenset  # live | echo
    live_probes: frozenset  # rival probe centres still within lifetime
    expired_probes: frozenset  # rival probe centres past lifetime
    lifetime: int = _DEFAULT_LIFETIME

    def sees(self, cell: Cell) -> bool:
        """True iff the enemy has LIVE vision of ``cell`` right now."""
        return (int(cell[0]), int(cell[1])) in self.live

    def saw(self, cell: Cell) -> bool:
        """True iff the enemy has EVER had vision of ``cell`` (live or echo)."""
        c = (int(cell[0]), int(cell[1]))
        return c in self.live or c in self.echo

    def classify(self, cell: Cell) -> str:
        """``'enemy_live'`` | ``'enemy_echo'`` | ``'enemy_dark'`` for a cell."""
        c = (int(cell[0]), int(cell[1]))
        if c in self.live:
            return "enemy_live"
        if c in self.echo:
            return "enemy_echo"
        return "enemy_dark"


def _lifetime(agent_view: Mapping[str, Any]) -> int:
    meta = agent_view.get("meta") or {}
    rules = meta.get("rules") if isinstance(meta, Mapping) else None
    if isinstance(rules, Mapping):
        raw = rules.get("probe_lifetime_nights")
        try:
            if raw is not None:
                return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    if _ENGINE_LIFETIME is not None:
        try:
            engine_val = _ENGINE_LIFETIME()  # zero-arg; returns Optional[int]
            if engine_val:
                return max(1, int(engine_val))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_LIFETIME


def _enemy_probe_last_seen(
    agent_view: Mapping[str, Any],
    session_id: str,
    player: str,
    store: Optional[Any],
) -> Dict[Cell, int]:
    """``{cell -> last_seen_day}`` for every enemy probe this seat knows of.

    Two sources, merged by most-recent day:
      * the current view's public markers (freshest; carries ``day_seen``), and
      * this seat's season-long frontier record (durable "used to see", so an
        expired probe that has aged out of the view's echo window is still
        remembered as ground the enemy once watched).
    """
    out: Dict[Cell, int] = {}
    for row in _enemy_probe_cells(agent_view):
        at = row.get("at")
        if isinstance(at, tuple) and len(at) == 2:
            try:
                cell = (int(at[0]), int(at[1]))
                day_seen = int(row.get("day_seen") or 0)
            except (TypeError, ValueError):
                continue
            out[cell] = max(out.get(cell, day_seen), day_seen)

    # Durable season history. frontier keeps per-cell last_day in a module
    # cache; hydrate it (cold-cache safe) then read the recency metadata.
    try:
        frontier.enemy_landings(session_id, player, store=store)
        hist = getattr(frontier, "_ENEMY_LANDINGS", {}).get(
            frontier._mem_key(session_id, player), {}
        )
        for cell, meta in (hist or {}).items():
            try:
                c = (int(cell[0]), int(cell[1]))
                last_day = int((meta or {}).get("last_day", 0))
            except (TypeError, ValueError, IndexError):
                continue
            out[c] = max(out.get(c, last_day), last_day)
    except Exception:
        pass
    return out


def build_enemy_vision(
    agent_view: Mapping[str, Any],
    *,
    session_id: str,
    player: str = "",
    day: int,
    store: Optional[Any] = None,
) -> EnemyVision:
    """Reconstruct the enemy vision footprint from public probe launches."""
    width, height = _grid_dims(agent_view)
    lifetime = _lifetime(agent_view)
    last_seen = _enemy_probe_last_seen(agent_view, session_id, player, store)

    live_probes: Set[Cell] = set()
    expired_probes: Set[Cell] = set()
    for cell, ls in last_seen.items():
        # age < lifetime → still lit. A negative age (history dated later than
        # the turn-lab's day=0) is treated as freshest, hence live.
        if (int(day) - int(ls)) < lifetime:
            live_probes.add(cell)
        else:
            expired_probes.add(cell)

    live_cells: Set[Cell] = set()
    for (x, y) in live_probes:
        live_cells.update(_vision_disk(x, y, width, height))
    echo_cells: Set[Cell] = set()
    for (x, y) in expired_probes:
        for c in _vision_disk(x, y, width, height):
            if c not in live_cells:
                echo_cells.add(c)

    return EnemyVision(
        live=frozenset(live_cells),
        echo=frozenset(echo_cells),
        ever=frozenset(live_cells | echo_cells),
        live_probes=frozenset(live_probes),
        expired_probes=frozenset(expired_probes),
        lifetime=lifetime,
    )


# ──────────────────────────────────────────────────────────────────────────
# Aggression
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Aggression:
    """A 0..1 field-aggression read with its component breakdown."""

    score: float
    label: str  # "passive" | "neutral" | "aggressive"
    components: Dict[str, float] = field(default_factory=dict)
    per_seat: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def _my_footprint(agent_view: Mapping[str, Any]) -> Set[Cell]:
    """Cells this seat is currently working — my live vision plus my probes."""
    foot: Set[Cell] = set(_los_cells(agent_view))
    for p in _friendly_probe_positions(agent_view):
        try:
            foot.add((int(p[0]), int(p[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return foot


def _cheapest_weapon_price(
    agent_view: Mapping[str, Any], estimates: Mapping[str, Any]
) -> int:
    meta = agent_view.get("meta") or {}
    rules = meta.get("rules") if isinstance(meta, Mapping) else None
    costs = rules.get("weapon_blue_costs") if isinstance(rules, Mapping) else None
    prices: List[int] = []
    if isinstance(costs, Mapping):
        for v in costs.values():
            try:
                prices.append(int(v))
            except (TypeError, ValueError):
                continue
    if not prices:
        for est in estimates.values():
            for v in getattr(est, "prices", {}).values():
                try:
                    prices.append(int(v))
                except (TypeError, ValueError):
                    continue
    prices = [p for p in prices if p > 0]
    return min(prices) if prices else 100


def _hits_on_me(agent_view: Mapping[str, Any], player: str) -> int:
    me = str(player or "")
    hits = 0
    for row in (agent_view.get("combat_events") or []):
        if not isinstance(row, Mapping):
            continue
        etype = str(row.get("type") or "")
        if not etype.endswith(_HIT_SUFFIX):
            continue
        victim = str(row.get("victim") or "")
        if me and victim and victim != me:
            continue
        # A hit event is private to the victim, so an unlabelled victim on my
        # own view is still my hit; a labelled one must match me.
        hits += 1
    return hits


def estimate_aggression(
    agent_view: Mapping[str, Any],
    vision: EnemyVision,
    *,
    session_id: str,
    player: str = "",
    day: int = 0,
    store: Optional[Any] = None,
) -> Aggression:
    """Fold public signals into a 0..1 field-aggression read."""
    try:
        estimates = opponent_weapons.update_estimates(session_id, player, agent_view)
    except Exception:
        estimates = {}

    opponents = ((agent_view.get("station_intel") or {}).get("opponents")) or []
    per_seat: Dict[str, Dict[str, Any]] = {}
    fired_total = 0
    probes_total = 0
    max_blue = 0
    for opp in opponents:
        if not isinstance(opp, Mapping):
            continue
        seat = str(opp.get("seat") or "")
        if not seat or seat == str(player or ""):
            continue
        activity = opp.get("activity") or {}
        fired = sum(
            int(activity.get(k) or 0) for k in ("emps", "chaff", "snaps")
        )
        probes = int(activity.get("probes") or 0)
        fired_total += fired
        probes_total += probes
        est = estimates.get(seat)
        blue = int(getattr(est, "blue", 0) or 0) if est is not None else 0
        max_blue = max(max_blue, blue)
        per_seat[seat] = {
            "blue": blue,
            "fired": fired,
            "probes": probes,
            "could_emp": bool(est.could_hold("emp")) if est is not None else False,
            "could_snap": bool(est.could_hold("snap")) if est is not None else False,
            "could_chaff": bool(est.could_hold("chaff")) if est is not None else False,
            "racks": getattr(est, "rack_text", lambda: "")() if est is not None else "",
        }

    hits = _hits_on_me(agent_view, player)
    footprint = _my_footprint(agent_view)
    eyes_frac = 0.0
    if footprint:
        watched = sum(1 for c in footprint if c in vision.live)
        eyes_frac = watched / float(len(footprint))

    unit = _cheapest_weapon_price(agent_view, estimates)

    # Normalise each signal to 0..1 with deliberately shallow caps: one hit,
    # two launches, ~two weapons' worth of arming, and a handful of live
    # rival probes are each enough to peg their component.
    c_hits = min(1.0, hits / 1.0)
    c_fired = min(1.0, fired_total / 2.0)
    c_armed = min(1.0, max_blue / float(2 * unit)) if unit > 0 else 0.0
    c_eyes = min(1.0, eyes_frac)
    c_scout = min(1.0, (probes_total + len(vision.live_probes)) / 4.0)

    score = (
        _W_HITS * c_hits
        + _W_FIRED * c_fired
        + _W_ARMED * c_armed
        + _W_EYES * c_eyes
        + _W_SCOUT * c_scout
    )
    score = max(0.0, min(1.0, score))
    if score >= _AGGRESSIVE_AT:
        label = "aggressive"
    elif score < _PASSIVE_BELOW:
        label = "passive"
    else:
        label = "neutral"

    return Aggression(
        score=round(score, 3),
        label=label,
        components={
            "hits_on_me": float(hits),
            "weapons_fired": float(fired_total),
            "armed_blue_max": float(max_blue),
            "eyes_on_me_frac": round(eyes_frac, 3),
            "scout_probes": float(probes_total),
            "live_enemy_probes": float(len(vision.live_probes)),
        },
        per_seat=per_seat,
    )


# ──────────────────────────────────────────────────────────────────────────
# Stance
# ──────────────────────────────────────────────────────────────────────────
def _season_cap(agent_view: Mapping[str, Any]) -> Optional[int]:
    hud = agent_view.get("hud") or {}
    for key in ("season_day_cap", "day_cap"):
        raw = hud.get(key) if isinstance(hud, Mapping) else None
        try:
            if raw is not None:
                return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def decide_stance(
    agent_view: Mapping[str, Any],
    aggression: Aggression,
    vision: EnemyVision,
    *,
    day: int = 0,
    season_len: Optional[int] = None,
) -> Tuple[str, str]:
    """Return ``("eco"|"attack", reason)``.

    Default is ``eco`` — accumulate RED score and BLUE currency. We tilt to
    ``attack`` (spend BLUE on ordnance, contest seams, deny the finder) when the
    field is hostile, when a rival's live probes are watching a large share of
    my ground, or in the endgame where denial and conversion beat accumulation.
    A later step folds in MY own means (held/affordable weapons) so we never
    pick a fight we cannot arm.
    """
    reasons: List[str] = []
    tilt = 0.0
    if aggression.label == "aggressive":
        tilt += 0.5
        reasons.append("field aggressive")
    elif aggression.label == "neutral":
        tilt += 0.2

    eyes = float(aggression.components.get("eyes_on_me_frac", 0.0))
    if eyes >= 0.25:
        tilt += 0.2
        reasons.append(f"watched ({int(round(eyes * 100))}% of my ground)")

    cap = season_len if season_len is not None else _season_cap(agent_view)
    if cap and int(day) >= int(cap) - 2:
        tilt += 0.3
        reasons.append("endgame")

    stance = "attack" if tilt >= 0.5 else "eco"
    return stance, ("; ".join(reasons) if reasons else "accumulate")


# ──────────────────────────────────────────────────────────────────────────
# Top-level read + prompt block
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class OpponentRead:
    vision: EnemyVision
    aggression: Aggression
    stance: str
    stance_reason: str
    day: int

    def prompt_block(self, max_probes: int = 3) -> str:
        """A terse, fact-only OPPONENT READ block for the prompt.

        Counts and a few public probe centres only — no coordinate dumps and no
        doctrine prose (added narration measurably loses points), so the model
        reads a compact situational fact and applies its own doctrine to it.
        """
        agg = self.aggression
        live_probe_centres = sorted(self.vision.live_probes)[:max_probes]
        centres = ", ".join(f"({x},{y})" for (x, y) in live_probe_centres)
        lines = [
            "OPPONENT READ (deterministic, from public signals):",
            (
                f"- posture: {agg.label.upper()} ({agg.score:.2f}) — "
                f"hits_on_me={int(agg.components.get('hits_on_me', 0))}, "
                f"fired={int(agg.components.get('weapons_fired', 0))}, "
                f"arming={int(agg.components.get('armed_blue_max', 0))}b"
            ),
            (
                f"- enemy vision: {len(self.vision.live)} cells LIVE "
                f"({len(self.vision.live_probes)} live probes / "
                f"{len(self.vision.expired_probes)} expired), "
                f"{len(self.vision.echo)} cells they USED TO see; "
                f"they watch {int(round(agg.components.get('eyes_on_me_frac', 0.0) * 100))}% "
                f"of your worked ground"
            ),
        ]
        if centres:
            lines.append(f"- live enemy probes at: {centres}")
        lines.append(f"- stance -> {self.stance.upper()} ({self.stance_reason})")
        return "\n".join(lines)


def read(
    agent_view: Mapping[str, Any],
    *,
    session_id: str,
    player: str = "",
    day: int = 0,
    store: Optional[Any] = None,
    season_len: Optional[int] = None,
) -> OpponentRead:
    """One call producing the enemy-vision map, aggression, and stance."""
    vision = build_enemy_vision(
        agent_view, session_id=session_id, player=player, day=day, store=store
    )
    aggression = estimate_aggression(
        agent_view, vision, session_id=session_id, player=player, day=day, store=store
    )
    stance, reason = decide_stance(
        agent_view, aggression, vision, day=day, season_len=season_len
    )
    return OpponentRead(
        vision=vision,
        aggression=aggression,
        stance=stance,
        stance_reason=reason,
        day=int(day),
    )
