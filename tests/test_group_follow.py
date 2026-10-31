"""Group follow: delta planning and the i3 command string. No running i3."""

from i3_nice_layout.daemon import _merge_user_state
from i3_nice_layout.group_follow import (
    Move,
    SnapMember,
    capture_origin,
    is_floating,
    move_command,
    plan_sync,
)
from i3_nice_layout.pool import Geometry, WindowRecord


def _member(con_id, origin, live, floating="user_on", workspace="1"):
    ox, oy = origin if origin is not None else (None, None)
    lx, ly = live if live is not None else (None, None)
    return SnapMember(
        id=con_id,
        origin_x=ox,
        origin_y=oy,
        live_x=lx,
        live_y=ly,
        floating=floating,
        workspace=workspace,
    )


def _rec(con_id, x, y, groups=None):
    rec = WindowRecord(
        id=con_id,
        window=con_id,
        name="w",
        window_class=None,
        instance=None,
        groups=list(groups or []),
        geometry=Geometry(x=x, y=y, width=100, height=80),
    )
    return rec


def test_move_command_relative_axes_and_criteria():
    cmd = move_command([
        Move(id=7, dx=10, dy=-4, target_x=30, target_y=6),
        Move(id=8, dx=-2, dy=5, target_x=1, target_y=9),
        Move(id=9, dx=0, dy=0, target_x=0, target_y=0),
    ])
    assert cmd == (
        '[con_id="7"] move right 10 px, move up 4 px; '
        '[con_id="8"] move left 2 px, move down 5 px'
    )


def test_one_member_drag_shifts_the_others_by_the_same_delta():
    members = [
        _member(1, (10, 20), (25, 14)),
        _member(2, (40, 20), (40, 20)),
        _member(3, (10, 80), (10, 80)),
    ]
    plan = plan_sync(members, leader_id=1, anchor_id=None)
    assert plan.action == "moved"
    assert (plan.dx, plan.dy) == (15, -6)
    assert [(m.id, m.dx, m.dy, m.target_x, m.target_y) for m in plan.moves] == [
        (2, 15, -6, 55, 14),
        (3, 15, -6, 25, 74),
    ]
    assert plan.settle[0] == (1, 25, 14)


def test_scan_picks_the_only_window_that_moved():
    members = [
        _member(1, (0, 0), (0, 0)),
        _member(2, (10, 0), (12, 3)),
    ]
    plan = plan_sync(members, leader_id=None, anchor_id=None)
    assert plan.action == "moved"
    assert plan.leader_id == 2
    assert (plan.dx, plan.dy) == (2, 3)


def test_two_movers_without_an_anchor_are_ambiguous():
    members = [
        _member(1, (0, 0), (5, 0)),
        _member(2, (0, 0), (0, 5)),
    ]
    plan = plan_sync(members, leader_id=None, anchor_id=None)
    assert plan.action == "ambiguous"
    assert plan.moves == []


def test_anchor_is_the_only_window_that_pulls():
    members = [
        _member(1, (0, 0), (8, 0)),
        _member(2, (20, 0), (20, 0)),
    ]
    pulled = plan_sync(members, leader_id=None, anchor_id=1)
    assert pulled.action == "moved"
    assert pulled.moves[0].id == 2
    assert pulled.moves[0].dx == 8

    alone = plan_sync(members, leader_id=2, anchor_id=1)
    assert alone.action == "not-anchor"
    assert alone.moves == []
    assert alone.settle == [(2, 20, 0)]


def test_anchor_holds_still_while_a_member_is_nudged():
    members = [
        _member(1, (0, 0), (0, 0)),
        _member(2, (20, 0), (22, 4)),
    ]
    plan = plan_sync(members, leader_id=None, anchor_id=1)
    assert plan.action == "settled"
    assert plan.moves == []
    assert plan.settle == [(2, 22, 4)]


def test_skips_tiled_and_other_workspace():
    members = [
        _member(1, (0, 0), (5, 1)),
        _member(2, (0, 0), (0, 0), floating="user_off"),
        _member(3, (0, 0), (0, 0), workspace="2"),
    ]
    plan = plan_sync(members, leader_id=1, anchor_id=None)
    assert plan.action == "leader-only"
    assert plan.moves == []
    assert {item["reason"] for item in plan.skipped} == {"not-floating", "other-workspace"}


def test_leader_not_floating():
    members = [_member(1, (0, 0), (4, 0), floating="off")]
    plan = plan_sync(members, leader_id=1, anchor_id=None)
    assert plan.action == "leader-not-floating"


def test_capture_origin_uses_pool_geometry_once():
    rec = _rec(1, 3, 4)
    assert capture_origin(rec) == (3, 4)
    rec.geometry = Geometry(x=9, y=9, width=1, height=1)
    assert capture_origin(rec) == (3, 4)


def test_merge_keeps_group_xy():
    existing = _rec(1, 1, 1, groups=["notes"])
    existing.extra["group_xy"] = [1, 1]
    existing.tags = ["a"]
    fresh = _rec(1, 6, 7)
    _merge_user_state(fresh, existing)
    assert fresh.groups == ["notes"]
    assert fresh.tags == ["a"]
    assert fresh.extra["group_xy"] == [1, 1]
    assert fresh.geometry.x == 6


def test_is_floating_flags():
    assert is_floating("user_on")
    assert is_floating("auto_on")
    assert not is_floating("user_off")
    assert not is_floating("off")
