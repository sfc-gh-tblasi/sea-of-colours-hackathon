"""Second-probe cover for a landing a SNAP could unsight (v1.40).

``probe_hints`` answers "where do I open fresh ground". Every filter in it
is tuned to that question and is right about it: seeds must sit outside
live vision, must clear ``_MIN_PROBE_SEPARATION`` from any probe this seat
has ever launched, and must reveal fog to earn a slot. A probe placed to
re-cover ground already covered fails all three, and the comment above the
last of them says so in as many words — re-probing a cell already in a
friendly disk "wastes an hour and a probe slot".

That was true until SNAP. A grab resting on one probe rests on one cell a
rival can delete for 100 blue, and the sight it would have given never
exists, so the landing is refused for want of vision. Redundancy stopped
being waste and became insurance.

Rather than weaken the nomadic filters — they answer their own question
correctly — this module asks a different one: *which landing tonight rests
on a single probe, and where else could a probe stand that sees the same
cell*. It is deliberately a separate file with one call site, because SNAP
is meant to be retirable the way the mines were: delete this module and the
``cover_hints`` call in the harness and nothing else knows it existed.

Two geometric facts shape the placement:

  * ``SNAP_RADIUS`` is 0 — one square — so a second probe on any cell other
    than the first cannot be taken by the same round. Difference of cell,
    not distance, is what buys the insurance.
  * probe vision is a Euclidean r4 disk, so two probes can both see one
    cell while standing up to eight apart. There is room to place the
    cover well away from the primary and still cover the target.

The cover may never stand ON the contested cell. A SNAP aimed there denies
the landing whatever else is true, so a probe parked on it is insurance
that fails in exactly the case it was bought for.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from sea_of_colours.orchestrator_2.harnesses.tblasi_warden._v7.probe_hints import (
    _PROBE_RADIUS,
    _grid_dims,
    _los_cells,
    _visible_red,
)

__all__ = ["cover_hints"]

# Only insure a landing worth insuring. Below this the probe is better spent
# opening new ground — the nomadic default the rest of the compiler keeps.
_MIN_TARGET_PURITY = 128

# Two is already the whole idea; a third cover probe is a fleet spent on
# fear of a weapon that may not exist.
_MAX_HINTS = 2


def _euclid_ok(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    """Does a probe at ``a`` hold ``b`` in live vision? Engine shape."""
    dx, dy = a[0] - b[0], a[1] - b[1]
    return dx * dx + dy * dy <= _PROBE_RADIUS * _PROBE_RADIUS


def _estimates(estimates: Any) -> List[Any]:
    """The estimates themselves, whether given as a mapping or a sequence.

    The harness holds these as ``{seat: WeaponEstimate}`` and every other
    consumer reaches for ``.values()``. Iterating the mapping directly
    yields seat STRINGS, which have no ``could_hold`` — and since the
    pre-v38 fallback below swallows exactly that AttributeError, the gate
    answered "nobody could be holding one" against a seat holding all
    three. Normalise here so the shape cannot decide the answer.
    """
    if hasattr(estimates, "values"):
        return list(estimates.values())
    return list(estimates or [])


def _could_snap(estimates: Any) -> bool:
    """Could any rival be holding a SNAP?

    ``could_hold`` is a spend threshold that enforces itself — a seat that
    has weaponised 100 can be in a SNAP and a seat that has weaponised
    nothing cannot be in anything — so a board that does not price SNAP at
    all answers False here without this module needing to know that.
    """
    for est in _estimates(estimates):
        try:
            if est.could_hold("snap"):
                return True
        except AttributeError:
            # Pre-v38 estimate: no could_hold, and no SNAP either.
            continue
    return False


def _probe_stock(agent_view: Mapping[str, Any]) -> int:
    raw = agent_view.get("probe_stock")
    if raw is None:
        raw = (agent_view.get("orbit") or {}).get("probe_stock")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _own_probe_cells(agent_view: Mapping[str, Any]) -> List[Tuple[int, int]]:
    """Friendly probes still alive tonight — the sight a landing may rest on."""
    out: List[Tuple[int, int]] = []
    for e in ((agent_view.get("entities") or {}).get("mine") or []):
        if not isinstance(e, Mapping) or str(e.get("type") or "") != "probe":
            continue
        nights = e.get("nights_remaining")
        if isinstance(nights, (int, float)) and int(nights) <= 0:
            continue
        pos = e.get("pos") or e.get("at")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                out.append((int(pos[0]), int(pos[1])))
            except (TypeError, ValueError):
                continue
    return out


def _cell(v: Any) -> Optional[Tuple[int, int]]:
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return (int(v[0]), int(v[1]))
        except (TypeError, ValueError):
            return None
    return None


def _planned_landings(
    seam_patterns: Sequence[Any],
    hot_drop_hints: Sequence[Mapping[str, Any]],
) -> List[Tuple[int, int]]:
    """Cells tonight's menu puts a harvester on, in menu order.

    Deny-only waves are skipped: they spend a probe and land nothing, so
    there is no landing to insure.
    """
    out: List[Tuple[int, int]] = []
    seen: Set[Tuple[int, int]] = set()

    def _add(v: Any) -> None:
        c = _cell(v)
        if c is not None and c not in seen:
            seen.add(c)
            out.append(c)

    for pattern in seam_patterns or []:
        for wave in (getattr(pattern, "waves", None) or []):
            if getattr(wave, "deny_only", False):
                continue
            _add(getattr(wave, "drop_at", None))
    for hint in hot_drop_hints or []:
        if isinstance(hint, Mapping):
            _add(hint.get("drop_at") or hint.get("at"))
    return out


def _planned_probe_cells(seam_patterns: Sequence[Any]) -> Set[Tuple[int, int]]:
    """Probes the menu intends to launch tonight.

    These count as cover — a blind grab's sight comes from a probe that has
    not landed yet — and equally as things a SNAP can delete, since SNAP
    resolves on its cell before that hour's vision snapshot.
    """
    out: Set[Tuple[int, int]] = set()
    for pattern in seam_patterns or []:
        for wave in (getattr(pattern, "waves", None) or []):
            c = _cell(getattr(wave, "probe_at", None))
            if c is not None:
                out.add(c)
    return out


def _target_value(
    target: Tuple[int, int],
    red: Mapping[Tuple[int, int], int],
    beacons: Set[Tuple[int, int]],
) -> int:
    """How much this landing is worth insuring. 0 means don't bother.

    A redsign beacon is worth insuring on the strength of the broadcast
    alone: the whole point of a smash-and-grab is that the value is known
    to be there and not yet visible in detail.
    """
    purity = int(red.get(target) or 0)
    if purity >= _MIN_TARGET_PURITY:
        return purity
    if target in beacons:
        return _MIN_TARGET_PURITY
    return 0


def _pick_cover(
    target: Tuple[int, int],
    primary: Tuple[int, int],
    *,
    width: int,
    height: int,
    occupied: Set[Tuple[int, int]],
    los: Set[Tuple[int, int]],
) -> Optional[Dict[str, Any]]:
    """The best cell to stand a second probe on so it sees ``target`` too.

    Ranked on fresh ground first: a cover probe that also opens fog is a
    probe spent twice, and there are usually several placements that see
    the target equally well. Distance from the primary breaks ties — not
    because one SNAP could take both (it cannot, the radius is 0) but
    because two disks far apart cover more between them.
    """
    r = _PROBE_RADIUS
    best: Optional[Tuple[Tuple[int, int, int, int], Dict[str, Any]]] = None
    for bx in range(max(0, target[0] - r), min(width, target[0] + r + 1)):
        for by in range(max(0, target[1] - r), min(height, target[1] + r + 1)):
            cand = (bx, by)
            if not _euclid_ok(cand, target):
                continue
            # Never stand on the contested cell: a SNAP there denies the
            # landing anyway, so cover parked on it fails in the one case
            # it exists for.
            if cand == target or cand in occupied:
                continue
            area_gain = sum(
                1
                for x in range(max(0, bx - r), min(width, bx + r + 1))
                for y in range(max(0, by - r), min(height, by + r + 1))
                if _euclid_ok((bx, by), (x, y)) and (x, y) not in los
            )
            spread = max(abs(bx - primary[0]), abs(by - primary[1]))
            # Negated so plain descending sort on the key picks the best,
            # with coordinates last to keep the choice deterministic.
            key = (-area_gain, -spread, bx, by)
            if best is None or key < best[0]:
                best = (key, {"at": [bx, by], "area_gain": area_gain, "spread": spread})
    return best[1] if best is not None else None


def cover_hints(
    agent_view: Mapping[str, Any],
    *,
    seam_patterns: Sequence[Any] = (),
    hot_drop_hints: Sequence[Mapping[str, Any]] = (),
    estimates: Sequence[Any] = (),
    max_hints: int = _MAX_HINTS,
) -> List[Dict[str, Any]]:
    """Placements for a second probe over a landing that rests on one.

    Returns ``[]`` — the common case — unless every one of these holds:
    a rival could be holding a SNAP, this seat has a probe to spend, the
    menu offers a landing worth insuring, and that landing's sight comes
    from exactly one probe. Two covers at most.

    Each hint carries ``at`` (where to put the cover), ``covers`` (the
    landing it protects), ``primary`` (the single probe it backs up),
    ``area_gain`` (fresh fog the cover opens anyway) and ``spread``
    (Chebyshev distance between the two probes).
    """
    if not _could_snap(estimates):
        return []
    if _probe_stock(agent_view) <= 0:
        return []

    landings = _planned_landings(seam_patterns, hot_drop_hints)
    if not landings:
        return []

    width, height = _grid_dims(agent_view)
    los = _los_cells(agent_view)
    red = _visible_red(agent_view)
    beacons = {
        c for c in (
            _cell(getattr(p, "beacon", None)) for p in (seam_patterns or [])
        ) if c is not None
    }
    live_probes = _own_probe_cells(agent_view)
    planned_probes = _planned_probe_cells(seam_patterns)
    all_probes = set(live_probes) | planned_probes

    scored: List[Tuple[int, Dict[str, Any]]] = []
    for target in landings:
        value = _target_value(target, red, beacons)
        if value <= 0:
            continue
        covering = [p for p in sorted(all_probes) if _euclid_ok(p, target)]
        # Nothing sees it (a frontier dive — no sight to insure), or two
        # already do (insured, by accident or on purpose).
        if len(covering) != 1:
            continue
        pick = _pick_cover(
            target, covering[0],
            width=width, height=height,
            occupied=all_probes, los=los,
        )
        if pick is None:
            continue
        pick["covers"] = [target[0], target[1]]
        pick["primary"] = [covering[0][0], covering[0][1]]
        scored.append((value, pick))

    scored.sort(key=lambda row: (-row[0], row[1]["at"]))
    return [hint for _value, hint in scored[: max(0, int(max_hints))]]
