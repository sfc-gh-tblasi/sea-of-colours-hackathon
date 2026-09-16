"""Opponent weapon stock — read off the public station observation.

Since v1.34 the engine broadcasts every seat's *weaponised blue*
(``station_intel.opponents[X].arms.blue``): the build cost of the
ordnance it is holding, exact, to everybody (RULEBOOK §4.9.8). This
module turns that number back into ``{emp, chaff}`` counts.

**This used to be inference, and the shape of the answer is why the
inference is gone rather than merely bypassed.** The old estimator
watched a rival's blue-purity band drop between nights and reasoned
about what could have been built with the difference. It was a good
mechanic and it was wrong most of the time: a 150-purity pip is coarse
enough that one drop admitted an EMP, a chaff, or neither, so the
``max`` on both weapons crept upward all season and every opponent
eventually read as armed with everything. Worse, the anchor was a
*previous night*, so the first turn of any game — and every turn in the
turn lab, which has no previous night at all — had no signal whatsoever.

What is kept, deliberately:

  * ``WeaponEstimate`` and its ``[min..max]`` fields, which ~47 call
    sites read by name. They answer one question honestly — "could this
    seat have any of X" — and that is all they are now for.
  * The audit lines. An agent should be able to tell a stated fact from
    a deduced one, and the line says which it is.

**v1.38 — the bounds are no longer the answer, ``racks`` is.** The 1-2-3
retune made most totals ambiguous on purpose, and the module reported
that ambiguity as one independent range per weapon. Those are marginals,
and marginals read together describe racks that cannot exist: 600 blue
rendered as ``emp=[0..3] chaff=[0..2]``, which a model reads as "up to
three EMPs *and* up to two chaff" — 1200 blue under a 600 cap. Of the
seven real racks at 600, exactly one holds both an EMP and a chaff.

So the estimate now carries the decoded set itself, and everything
written for a reader — :meth:`WeaponEstimate.summary`, the audit line,
the prompt block — states it as "exactly ONE of these N". At the shipped
prices N is never more than seven, so there is no reason to compress a
short list of true answers into a wide box of mostly false ones.

SNAP arrived in v1.36 and was missing here entirely, which was the same
bug wearing a different hat: 100 blue decodes to a lone SNAP, leaving
both old maxima at zero, so the seat read as unarmed and was dropped
from the prompt. Bounds are now derived from whatever the game prices
rather than from a hardcoded pair.

The one thing a reader here must not do is treat ``is_exact()`` as the
normal case. It is the exception now.

What is gone:

  * Band-drop inference, per above.
  * The launch decrement. It would now double-count: firing a weapon
    drains ``weapon_stock``, so a launch is already reflected in the
    ``arms.blue`` we are reading. Launches survive as an audit note
    because "they fired one last night" is still worth telling the
    model, but they no longer move the arithmetic.
  * The need for cross-turn memory. Stock is derived fresh from the
    view every turn, so a missing prior is no longer a blind spot. The
    store stays for the audit trail, and because callers expect it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sea_of_colours.game.weapons import decode_rack


@dataclass
class WeaponEstimate:
    """Per-opponent weapon stock: the exact total, and the racks that fit it.

    Since v1.34 this is read off the public arsenal figure rather than
    inferred. Since v1.36 one total usually admits several racks, so the
    honest answer is a *set* — and since v1.38 this class carries that
    set in ``racks`` rather than only its shadow.

    **``racks`` is the answer; the ``[min..max]`` fields are a lossy
    projection of it.** They are per-weapon marginals taken
    independently, so reading two of them together invents a rack that
    cannot exist: at 600 blue the bounds are ``emp=[0..3]`` and
    ``chaff=[0..2]``, and "3 EMP and 2 chaff" is 1200 blue — twice the
    cap. Exactly one of the seven real racks holds both an EMP and a
    chaff. Anything rendering this for a human or a model must use
    ``racks`` (or :meth:`summary`, which does); the bounds are kept
    because ~47 call sites ask the one question they answer honestly,
    which is "could this seat have any of X".

    ``max == 0`` remains the one always-safe conclusion: no rack at that
    total includes one. Any ``max > 0`` means "could have", never "has".
    """
    seat: str
    emps_min: int = 0
    emps_max: int = 0
    chaff_min: int = 0
    chaff_max: int = 0
    #: v1.38 — SNAP was missing entirely, so a seat holding one read as
    #: unarmed everywhere downstream (100 blue decodes to a lone SNAP,
    #: which left both fields above at zero and dropped the seat from
    #: the prompt). Bounds for every kind live in ``bounds``; these three
    #: pairs are the named shortcuts the existing call sites use.
    snap_min: int = 0
    snap_max: int = 0
    #: ``{kind: (min, max)}`` over whatever the game prices — the
    #: generic form, so a fourth weapon needs no new field here.
    bounds: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    #: Every loadout that fits the public total, cheapest kind first.
    #: This is the fact; everything above is derived from it.
    racks: List[Dict[str, int]] = field(default_factory=list)
    #: The public figure itself, and the ceiling it is measured against.
    blue: int = 0
    cap: int = 0
    #: The blue price of each kind *on this board*. Kept alongside the
    #: decode rather than re-read from the live constants, so a legacy
    #: season is explained with the ladder it was played on.
    prices: Dict[str, int] = field(default_factory=dict)
    #: Retained for wire-compatibility with stored estimates written by
    #: earlier versions. No longer read — the arsenal is stated now, so
    #: there is nothing to anchor a band comparison against.
    last_blue_band: Optional[int] = None
    #: Free-form one-line explanations rendered into the prompt as a
    #: small audit trail. Bounded to the last few entries.
    inferences: List[str] = field(default_factory=list)

    def has_any(self) -> bool:
        """Is this seat holding ANY ordnance?

        v1.38 — was ``emps_max + chaff_max``, which answered "any EMP or
        chaff" and called a seat holding 100 blue of SNAP unarmed. The
        public total is the whole point: if it is positive the seat has
        bought something, whatever it is.
        """
        return int(self.blue) > 0 or any(
            hi > 0 for _lo, hi in self.bounds.values()
        )

    def is_exact(self) -> bool:
        """True when the public total admitted exactly one rack."""
        return len(self.racks) == 1

    def could_hold(self, kind: str) -> bool:
        """Is a ``kind`` in ANY rack that fits this seat's public total?

        v1.38 — the single gate for "should I warn about X against this
        seat", so the three weapon warnings ask one question instead of
        each inventing its own.

        This is a spend threshold and it enforces itself: a rack
        containing a chaff costs at least its 300 blue, so a seat that
        has weaponised 100 cannot be in one and no chaff warning can
        fire against it. Nothing here hardcodes 300 — the arithmetic
        already happened in ``decode_rack``, and asking the bounds is
        how a fourth weapon inherits the same guarantee for free.
        """
        return self.bounds.get(kind, (0, 0))[1] > 0

    def min_spend_for(self, kind: str) -> int:
        """Cheapest total at which ``kind`` becomes possible, or 0.

        Read off ``prices`` — the table the game that owns this board
        stamped, not today's dials. A season played before a retune
        keeps its own ladder (§4.9.8), so quoting the live constant here
        would explain a warning with a number that board never used.

        Only for explaining a warning. The decision is
        :meth:`could_hold`.
        """
        return int(self.prices.get(kind, 0))

    def rack_text(self, sep: str = " | ") -> str:
        """The candidate racks, spelled out. Empty when nothing is held."""
        parts = []
        for rack in self.racks:
            held = [
                f"{n} {kind}" for kind, n in sorted(rack.items()) if n
            ]
            if held:
                parts.append(" + ".join(held))
        return sep.join(parts)

    def summary(self) -> str:
        """One-line render for prompts / logs.

        States the total and the racks that fit it. The old form printed
        the marginals alone, which read as a joint range and overstated
        what a seat could be carrying (see the class docstring).
        """
        if int(self.blue) <= 0:
            return f"{self.seat}: nothing (0 blue of ordnance)"
        held = self.rack_text()
        if self.is_exact():
            return f"{self.seat}: {self.blue} blue of ordnance — {held}"
        return (
            f"{self.seat}: {self.blue} blue of ordnance — exactly ONE of "
            f"these {len(self.racks)}: {held}"
        )


# ─────────────────────────────────────────────────────────────────────────
# In-process store — keyed by (session_id, viewer_player) → { seat: est }
# ─────────────────────────────────────────────────────────────────────────

_MEMORY_STORE: Dict[Tuple[str, str], Dict[str, WeaponEstimate]] = {}


def _key(session_id: str, viewer: str) -> Tuple[str, str]:
    return (str(session_id), str(viewer))


def load_estimates(session_id: str, viewer: str) -> Dict[str, WeaponEstimate]:
    """Return the CURRENT stored estimates (copy — safe to mutate the
    returned dict without corrupting the store). Empty on first call."""
    stored = _MEMORY_STORE.get(_key(session_id, viewer)) or {}
    return {k: v for k, v in stored.items()}


def store_estimates(
    session_id: str, viewer: str, estimates: Mapping[str, WeaponEstimate],
) -> None:
    """Persist ``estimates`` for the next turn's audit trail."""
    _MEMORY_STORE[_key(session_id, viewer)] = dict(estimates)


def clear_store() -> None:
    """Test helper — reset the in-process store."""
    _MEMORY_STORE.clear()


# ─────────────────────────────────────────────────────────────────────────
# Reading the public arsenal
# ─────────────────────────────────────────────────────────────────────────


def update_estimates(
    session_id: str,
    viewer: str,
    agent_view: Mapping[str, Any],
    *,
    max_audit_lines: int = 4,
) -> Dict[str, WeaponEstimate]:
    """Read every opponent's arsenal off ``station_intel``.

    Each opponent entry carries:

      * ``seat`` — opponent's player id
      * ``arms.blue`` — weaponised blue, exact and public (v1.34)
      * ``activity.emps`` / ``.chaff`` — launches on the resolved night,
        used for the audit trail only

    A view with no ``arms`` block is a weapons-disabled game, and every
    seat correctly reads as holding nothing.

    Returns the updated estimates dict (and stores it).
    """
    prior = load_estimates(session_id, viewer)
    station_intel = agent_view.get("station_intel") or {}
    opponents = station_intel.get("opponents") or []
    # v1.36 — decode against the price table THIS game was stamped with,
    # published on meta.rules. Decoding an archived season at today's
    # prices does not merely mislabel a rack: 455 blue is a real 1+1
    # loadout under the old table and impossible under the new one, so
    # it would silently read as an unarmed seat.
    prices = _published_prices(agent_view)

    updated: Dict[str, WeaponEstimate] = {}
    for opp in opponents:
        if not isinstance(opp, Mapping):
            continue
        seat = str(opp.get("seat") or "")
        if not seat or seat == viewer:
            continue

        est = prior.get(seat) or WeaponEstimate(seat=seat)
        blue = _arsenal_blue(opp)

        if blue is None:
            # Weapons are off in this game. Say nothing rather than
            # carrying a stale range forward from a prior turn.
            _set_bounds(est, {}, [], blue=0, cap=0)
        else:
            # Cap the search to the ceiling this game publishes, so a
            # rack the engine could never sell is never proposed.
            cap = _arsenal_cap(opp)
            loadouts = [
                rack for rack in (decode_rack(blue, prices) or [])
                if cap <= 0 or _rack_cost(rack, prices) <= cap
            ]
            _set_bounds(est, prices, loadouts, blue=blue, cap=cap)
            est.inferences.append(
                _audit_line(seat, blue, est, opp, len(loadouts))
            )

        if len(est.inferences) > max_audit_lines:
            est.inferences = est.inferences[-max_audit_lines:]

        updated[seat] = est

    store_estimates(session_id, viewer, updated)
    return updated


#: Kinds with a named ``*_min`` / ``*_max`` pair on the estimate. The
#: generic answer is ``bounds``; these exist because the harness was
#: written against them and a fork reads them by name.
_NAMED = {"emp": "emps", "chaff": "chaff", "snap": "snap"}


def _rack_cost(rack: Mapping[str, int], prices: Optional[Mapping[str, int]]) -> int:
    table = prices or {}
    return sum(int(n) * int(table.get(kind, 0)) for kind, n in rack.items())


def _set_bounds(
    est: WeaponEstimate,
    prices: Optional[Mapping[str, int]],
    loadouts: List[Dict[str, int]],
    *,
    blue: int,
    cap: int,
) -> None:
    """Fold the rack set into the estimate, generically.

    Derived from the price table rather than a hardcoded kind list, so a
    weapon added tomorrow gets bounds here without an edit — the same
    property that lets ``decode_rack`` and ``weaponised_blue`` iterate
    the table. The three named pairs are written from the same source so
    they can never disagree with ``bounds``.
    """
    est.blue = int(blue)
    est.cap = int(cap)
    est.prices = {str(k): int(v) for k, v in (prices or {}).items()}
    est.racks = [dict(rack) for rack in loadouts]

    kinds = sorted(set(prices or {}) | {k for r in loadouts for k in r})
    est.bounds = {}
    for kind in kinds:
        counts = [int(r.get(kind, 0)) for r in loadouts] or [0]
        est.bounds[kind] = (min(counts), max(counts))

    for kind, stem in _NAMED.items():
        lo, hi = est.bounds.get(kind, (0, 0))
        setattr(est, f"{stem}_min", lo)
        setattr(est, f"{stem}_max", hi)


def _audit_line(
    seat: str,
    blue: int,
    est: WeaponEstimate,
    opp: Mapping[str, Any],
    candidates: int = 1,
) -> str:
    """One line of provenance and recent activity.

    v1.38 — this used to restate the rack, which made it a duplicate of
    ``summary()`` sitting directly above it in the prompt, and in the
    ambiguous case it restated the *marginals*, which is the one
    rendering this file must not do (see the module docstring). The
    inventory is the summary's job; this line's job is to say where the
    number came from and what the seat has been doing with it.

    ``candidates`` is how many racks the total admitted — reported as a
    count, because knowing the reading is uncertain is useful even when
    the list itself is elsewhere.
    """
    if blue <= 0:
        body = "has weaponised nothing"
    elif candidates == 1:
        body = f"has weaponised {blue} blue, and only one rack fits it"
    else:
        body = (
            f"has weaponised {blue} blue, which {candidates} different "
            f"racks fit"
        )

    activity = opp.get("activity") or {}
    # Every weapon the tally counts, not a hardcoded two — SNAP was
    # already missing from this sum on the day it shipped.
    fired = sum(
        int(activity.get(key) or 0) for key in ("emps", "chaff", "snaps")
    )
    tail = f"; fired {fired} last night" if fired else ""
    return f"public: {seat} {body}{tail} (stated by the board, not inferred)"


def _published_prices(agent_view: Mapping[str, Any]) -> Optional[Dict[str, int]]:
    """The game's own weapon price table off ``meta.rules``, or ``None``.

    ``None`` lets :func:`decode_rack` fall back to the engine constants,
    which is right for the callers that hand this function a hand-built
    view with no meta block.
    """
    meta = agent_view.get("meta")
    rules = meta.get("rules") if isinstance(meta, Mapping) else None
    raw = rules.get("weapon_blue_costs") if isinstance(rules, Mapping) else None
    if not isinstance(raw, Mapping) or not raw:
        return None
    out: Dict[str, int] = {}
    for kind, cost in raw.items():
        try:
            out[str(kind)] = int(cost)
        except (TypeError, ValueError):
            continue
    return out or None


def _arsenal_blue(opp: Mapping[str, Any]) -> Optional[int]:
    """Weaponised blue for one opponent, or ``None`` when weapons are off."""
    arms = opp.get("arms")
    if not isinstance(arms, Mapping):
        return None
    try:
        return max(0, int(arms.get("blue") or 0))
    except (TypeError, ValueError):
        return None


def _arsenal_cap(opp: Mapping[str, Any]) -> int:
    """The ceiling this game publishes alongside the total, or 0.

    Used to drop racks the engine could never have sold. ``decode_rack``
    enumerates against the prices alone and does not know the cap, so
    without this a partial price table could propose a loadout worth
    more than any seat may hold.
    """
    arms = opp.get("arms")
    if not isinstance(arms, Mapping):
        return 0
    try:
        return max(0, int(arms.get("cap") or 0))
    except (TypeError, ValueError):
        return 0
