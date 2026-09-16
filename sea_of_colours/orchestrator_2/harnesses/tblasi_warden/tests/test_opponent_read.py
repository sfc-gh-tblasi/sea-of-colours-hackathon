"""Fork-local tests for opponent_read (enemy vision, aggression, stance).

Synthetic agent_view fixtures only — no engine, no Snowflake (store=None), so
these run anywhere and are deterministic. We reset the two process-global
caches opponent_read leans on (frontier landings, weapon estimates) so tests
never leak into one another.
"""

from __future__ import annotations

import pytest

from sea_of_colours.orchestrator_2.harnesses.tblasi_warden import (
    frontier,
    opponent_read,
)
from sea_of_colours.orchestrator_2.harnesses.tblasi_warden._v7 import (
    opponent_weapons,
)


@pytest.fixture(autouse=True)
def _clean_caches():
    frontier.reset()
    opponent_weapons.clear_store()
    yield
    frontier.reset()
    opponent_weapons.clear_store()


def _view(**over):
    v = {
        "meta": {"rules": {"probe_lifetime_nights": 3}},
        "hud": {"season_day_cap": 7},
        "world": {"width": 40, "height": 28, "live": []},
        "competitor_intel": {"new_this_day": [], "persistent_echoes": []},
        "station_intel": {"opponents": []},
        "combat_events": [],
    }
    v.update(over)
    return v


# ── enemy vision: live vs echo (sees now / used to see) ──────────────────
def test_live_probe_lights_disk_now():
    view = _view(
        competitor_intel={
            "new_this_day": [
                {"kind": "enemy_probe_launch", "at": [10, 10], "day_seen": 5}
            ],
            "persistent_echoes": [],
        }
    )
    vis = opponent_read.build_enemy_vision(
        view, session_id="s1", player="P1", day=5
    )
    assert (10, 10) in vis.live_probes
    assert vis.sees((10, 10)) is True
    assert vis.sees((10, 13)) is True  # inside r4 euclidean disk
    assert vis.classify((10, 10)) == "enemy_live"


def test_expired_probe_is_echo_not_live():
    view = _view(
        competitor_intel={
            "new_this_day": [],
            "persistent_echoes": [
                {"kind": "enemy_probe", "at": [30, 20], "last_seen_day": 1}
            ],
        }
    )
    vis = opponent_read.build_enemy_vision(
        view, session_id="s1", player="P1", day=5
    )
    assert (30, 20) in vis.expired_probes
    assert vis.sees((30, 20)) is False
    assert vis.saw((30, 20)) is True
    assert vis.classify((30, 20)) == "enemy_echo"


def test_live_and_echo_are_disjoint():
    view = _view(
        competitor_intel={
            "new_this_day": [
                {"kind": "enemy_probe_launch", "at": [10, 10], "day_seen": 5}
            ],
            "persistent_echoes": [
                {"kind": "enemy_probe", "at": [12, 10], "last_seen_day": 0}
            ],
        }
    )
    vis = opponent_read.build_enemy_vision(
        view, session_id="s1", player="P1", day=5
    )
    assert vis.live & vis.echo == frozenset()
    # a cell shared by both disks is credited to LIVE, never echo
    assert (11, 10) in vis.live
    assert (11, 10) not in vis.echo


# ── aggression ───────────────────────────────────────────────────────────
def test_quiet_field_reads_passive():
    vis = opponent_read.build_enemy_vision(
        _view(), session_id="s1", player="P1", day=3
    )
    agg = opponent_read.estimate_aggression(
        _view(), vis, session_id="s1", player="P1", day=3
    )
    assert agg.label == "passive"
    assert agg.score < opponent_read._PASSIVE_BELOW


def test_hits_and_launches_read_aggressive():
    view = _view(
        station_intel={
            "opponents": [
                {
                    "seat": "P2",
                    "arms": {"blue": 400, "cap": 600},
                    "activity": {"probes": 1, "emps": 1, "chaff": 1, "snaps": 0},
                }
            ]
        },
        combat_events=[
            {"type": "snap_hit", "victim": "P1", "by": "P2", "outcome": "crippled"}
        ],
    )
    vis = opponent_read.build_enemy_vision(
        view, session_id="s2", player="P1", day=4
    )
    agg = opponent_read.estimate_aggression(
        view, vis, session_id="s2", player="P1", day=4
    )
    assert agg.components["hits_on_me"] == 1.0
    assert agg.components["weapons_fired"] == 2.0
    assert agg.label == "aggressive"
    assert "P2" in agg.per_seat


def test_hit_on_other_seat_not_counted_against_me():
    view = _view(
        combat_events=[
            {"type": "emp_hit", "victim": "P3", "by": "P2"}
        ]
    )
    vis = opponent_read.build_enemy_vision(
        view, session_id="s3", player="P1", day=4
    )
    agg = opponent_read.estimate_aggression(
        view, vis, session_id="s3", player="P1", day=4
    )
    assert agg.components["hits_on_me"] == 0.0


# ── stance ───────────────────────────────────────────────────────────────
def test_stance_eco_when_quiet_and_early():
    view = _view()
    vis = opponent_read.build_enemy_vision(
        view, session_id="s4", player="P1", day=2
    )
    agg = opponent_read.estimate_aggression(
        view, vis, session_id="s4", player="P1", day=2
    )
    stance, _reason = opponent_read.decide_stance(view, agg, vis, day=2)
    assert stance == "eco"


def test_stance_attack_when_field_aggressive():
    agg = opponent_read.Aggression(
        score=0.8, label="aggressive", components={"eyes_on_me_frac": 0.4}
    )
    empty = opponent_read.EnemyVision(
        live=frozenset(), echo=frozenset(), ever=frozenset(),
        live_probes=frozenset(), expired_probes=frozenset(),
    )
    stance, reason = opponent_read.decide_stance(_view(), agg, empty, day=3)
    assert stance == "attack"
    assert "aggressive" in reason


def test_stance_endgame_is_flagged():
    agg = opponent_read.Aggression(score=0.4, label="neutral", components={})
    empty = opponent_read.EnemyVision(
        live=frozenset(), echo=frozenset(), ever=frozenset(),
        live_probes=frozenset(), expired_probes=frozenset(),
    )
    stance, reason = opponent_read.decide_stance(
        _view(), agg, empty, day=6, season_len=7
    )
    assert "endgame" in reason
    assert stance == "attack"  # neutral (0.2) + endgame (0.3) >= 0.5


# ── top-level read + prompt block ────────────────────────────────────────
def test_read_bundles_everything_and_renders():
    view = _view(
        competitor_intel={
            "new_this_day": [
                {"kind": "enemy_probe_launch", "at": [15, 12], "day_seen": 4}
            ],
            "persistent_echoes": [],
        },
        world={
            "width": 40, "height": 28,
            "live": [{"x": 15, "y": 12}, {"x": 16, "y": 12}],
        },
    )
    r = opponent_read.read(view, session_id="s5", player="P1", day=4)
    assert r.stance in ("eco", "attack")
    block = r.prompt_block()
    assert "OPPONENT READ" in block
    assert "posture:" in block
    assert "stance ->" in block
    # this seat's own worked ground is inside the live enemy probe disk
    assert r.aggression.components["eyes_on_me_frac"] > 0.0
