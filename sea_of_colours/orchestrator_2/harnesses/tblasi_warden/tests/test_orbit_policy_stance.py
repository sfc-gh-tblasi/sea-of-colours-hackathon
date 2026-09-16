"""Stance-aware orbit buying (tblasi_warden).

Confirms the eco/attack stance retunes weapon buying and that ``stance=None``
leaves the shipped board-blind policy untouched. Synthetic orbit views only.
"""

from __future__ import annotations

import copy

from sea_of_colours.orchestrator_2.harnesses.tblasi_warden.orbit_policy import (
    plan_orbit_actions,
)


def _base_view():
    return {
        "meta": {"rules": {"weapon_blue_cap": 600}},
        "hud": {"day": 3},
        "entities": {"mine": []},
        "orbit": {
            "credits": 2000,
            "harvester_cap_used": 2,
            "harvester_cap_max": 3,
            "probe_stock": 4,
            "ship_prices": {
                "repair": 300, "probe_build": 250, "harvester_build": 1500,
            },
            "weapon_prices": {
                "emp": {"blue": 250, "credits": 250},
                "chaff": {"blue": 300, "credits": 0},
                "snap": {"blue": 100, "credits": 250},
            },
            "weapon_stock": {"emp": 0, "chaff": 1, "snap": 0},
            "blue_purity_total": 350,
        },
    }


def _verbs(actions):
    return [a.get("a") for a in actions]


def test_baseline_builds_emp_from_surplus():
    # blue 350 > always-build 300, chaff already at cap 1 -> EMP.
    actions, _ = plan_orbit_actions(_base_view())
    assert "build_emp" in _verbs(actions)


def test_eco_starves_speculative_ordnance():
    # eco lifts the always-build floor to 400 and kills the EMP roll, so at
    # blue 350 with a full chaff slot nothing is armed -> credits stay for probes.
    actions, _ = plan_orbit_actions(_base_view(), stance="eco")
    verbs = _verbs(actions)
    assert "build_emp" not in verbs
    assert "build_chaff" not in verbs
    # freed credits flow to economy (fleet/vision), not ordnance
    assert ("build_harvester" in verbs) or ("build_probe" in verbs)


def test_attack_still_arms():
    actions, _ = plan_orbit_actions(_base_view(), stance="attack")
    verbs = _verbs(actions)
    assert ("build_emp" in verbs) or ("build_chaff" in verbs)


def test_eco_but_armed_rival_buys_defensive_chaff():
    # Empty chaff rack + armed rival: the defensive rule fires even though eco
    # would otherwise stay quiet at blue 350 (< the eco 400 appetite floor).
    view = _base_view()
    view["orbit"]["weapon_stock"] = {"emp": 0, "chaff": 0, "snap": 0}
    actions, rationale = plan_orbit_actions(view, stance="eco", opp_armed=True)
    assert "build_chaff" in _verbs(actions)
    assert "defensive CHAFF" in rationale


def test_stance_none_is_untouched_when_no_surplus():
    # Below every weapon threshold: no weapon buy, stance or not — the parity
    # the differential test relies on.
    view = _base_view()
    view["orbit"]["blue_purity_total"] = 0
    a_none, _ = plan_orbit_actions(copy.deepcopy(view))
    a_eco, _ = plan_orbit_actions(copy.deepcopy(view), stance="eco")
    assert "build_emp" not in _verbs(a_none)
    assert "build_chaff" not in _verbs(a_none)
    # eco changes nothing when there was nothing to arm with
    assert _verbs(a_none) == _verbs(a_eco)
