"""Move the other windows in a group when one of them is repositioned.

i3's pointer drag updates the floating rect inside its own loop and does not
emit ``window::move`` for those motion events. The drag loop does keep servicing
the IPC socket, so a poll can see the new x,y and send move commands while the
button is still down. That is what ``--watch`` is for. It is deliberately
unadorned: no bar, no notify, just the commands and a log line per move so a
drag can be checked for jitter and IPC cost.

Positions remembered for the group live in ``WindowRecord.extra["group_xy"]``.
Ordinary pool updates (focus, title, …) do not rewrite that pair, so a drag
that produced no IPC event still yields a delta against the last settled spot.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .pool import WindowPool, WindowRecord

log = logging.getLogger("i3-nice-layout.follow")

# Give-up for a watched group: this many polls in a row where i3 reported
# success but the tree still shows the old x,y. Caps a retry storm if the
# rect we read is not the rect `move` changes.
_GIVE_UP_POLLS = 3
_ARRIVE_PX = 2
_MIN_INTERVAL_S = 0.01
_DEFAULT_INTERVAL_S = 0.05


@dataclass
class LiveGeom:
    x: int
    y: int
    floating: str
    workspace: str | None


@dataclass
class SnapMember:
    id: int
    origin_x: int | None
    origin_y: int | None
    live_x: int | None
    live_y: int | None
    floating: str
    workspace: str | None


@dataclass
class Move:
    id: int
    dx: int
    dy: int
    target_x: int
    target_y: int


@dataclass
class Plan:
    action: str
    leader_id: int | None = None
    dx: int = 0
    dy: int = 0
    moves: list[Move] = field(default_factory=list)
    # Origins to write when this plan is accepted. (id, x, y)
    settle: list[tuple[int, int, int]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    note: str | None = None


def is_floating(flag: str | None) -> bool:
    return flag not in (None, "off", "user_off", "auto_off")


def capture_origin(rec: WindowRecord) -> tuple[int, int] | None:
    """Return the settled group position, recording it from pool geometry
    the first time. Pool geometry is the last position an i3 event reported,
    which is the pre-drag spot when the drag itself emitted nothing."""
    xy = rec.extra.get("group_xy")
    if isinstance(xy, (list, tuple)) and len(xy) == 2:
        return int(xy[0]), int(xy[1])
    if rec.geometry is None:
        return None
    rec.extra["group_xy"] = [int(rec.geometry.x), int(rec.geometry.y)]
    return int(rec.geometry.x), int(rec.geometry.y)


def write_origin(rec: WindowRecord, x: int, y: int) -> None:
    rec.extra["group_xy"] = [int(x), int(y)]


def move_command(moves: list[Move]) -> str:
    """One i3 command string. Comma keeps the con_id criteria; semicolon
    starts the next window. Relative `move` shifts the floating parent,
    which is the rect a pointer drag also changes."""
    chunks: list[str] = []
    for move in moves:
        parts: list[str] = []
        if move.dx > 0:
            parts.append(f"move right {move.dx} px")
        elif move.dx < 0:
            parts.append(f"move left {-move.dx} px")
        if move.dy > 0:
            parts.append(f"move down {move.dy} px")
        elif move.dy < 0:
            parts.append(f"move up {-move.dy} px")
        if not parts:
            continue
        chunks.append(f'[con_id="{move.id}"] ' + ", ".join(parts))
    return "; ".join(chunks)


def _moved(member: SnapMember) -> bool:
    if member.origin_x is None or member.live_x is None:
        return False
    return member.live_x != member.origin_x or member.live_y != member.origin_y


def plan_sync(
    members: list[SnapMember],
    *,
    leader_id: int | None,
    anchor_id: int | None,
) -> Plan:
    """Decide the group delta.

    ``leader_id`` set: that window is the one that was repositioned (one-shot).
    ``leader_id`` none: pick it. An anchor, if set, is the only window that
    pulls the others. Otherwise exactly one member may have moved.
    """
    by_id = {m.id: m for m in members}

    if leader_id is not None:
        leader = by_id.get(leader_id)
        if leader is None:
            return Plan(action="no-window", error="window is not in this group")
        if anchor_id is not None and leader_id != anchor_id:
            if leader.live_x is None or leader.live_y is None:
                return Plan(
                    action="no-geometry",
                    leader_id=leader_id,
                    error="no live geometry for that window",
                )
            return Plan(
                action="not-anchor",
                leader_id=leader_id,
                settle=[(leader_id, leader.live_x, leader.live_y)],
                note=(
                    f"anchor is {anchor_id}; recorded this window and left the group"
                ),
            )
    elif anchor_id is not None:
        leader = by_id.get(anchor_id)
        if leader is None:
            return Plan(action="no-window", error="anchor is not in this group")
        if not _moved(leader):
            nudged = [m for m in members if m.id != anchor_id and _moved(m)]
            if not nudged:
                return Plan(action="unchanged", leader_id=anchor_id)
            settle = [
                (m.id, m.live_x, m.live_y)
                for m in nudged
                if m.live_x is not None and m.live_y is not None
            ]
            return Plan(
                action="settled",
                leader_id=anchor_id,
                settle=settle,
                note="member moved; anchor did not, so the group was not pulled",
            )
    else:
        moved = [m for m in members if _moved(m)]
        if not moved:
            return Plan(action="unchanged")
        if len(moved) > 1:
            return Plan(
                action="ambiguous",
                error="more than one window in the group moved; pass --id or set an anchor",
                skipped=[{"id": m.id, "reason": "ambiguous"} for m in moved],
            )
        leader = moved[0]

    if leader.live_x is None or leader.live_y is None or leader.origin_x is None or leader.origin_y is None:
        return Plan(
            action="no-geometry",
            leader_id=leader.id,
            error="leader has no geometry",
        )
    if not is_floating(leader.floating):
        return Plan(
            action="leader-not-floating",
            leader_id=leader.id,
            error="leader is not floating",
        )

    dx = leader.live_x - leader.origin_x
    dy = leader.live_y - leader.origin_y
    settle = [(leader.id, leader.live_x, leader.live_y)]
    if dx == 0 and dy == 0:
        return Plan(action="unchanged", leader_id=leader.id, settle=settle)

    moves: list[Move] = []
    skipped: list[dict[str, Any]] = []
    for member in members:
        if member.id == leader.id:
            continue
        if member.live_x is None or member.live_y is None:
            skipped.append({"id": member.id, "reason": "no-geometry"})
            continue
        if not is_floating(member.floating):
            skipped.append({"id": member.id, "reason": "not-floating"})
            continue
        if member.workspace != leader.workspace:
            skipped.append({"id": member.id, "reason": "other-workspace"})
            continue
        moves.append(
            Move(
                id=member.id,
                dx=dx,
                dy=dy,
                target_x=member.live_x + dx,
                target_y=member.live_y + dy,
            )
        )

    action = "moved" if moves else "leader-only"
    return Plan(
        action=action,
        leader_id=leader.id,
        dx=dx,
        dy=dy,
        moves=moves,
        settle=settle,
        skipped=skipped,
    )


def index_tree(tree) -> dict[int, LiveGeom]:
    found: dict[int, LiveGeom] = {}
    for con in tree.descendants():
        if not getattr(con, "window", None) or con.rect is None:
            continue
        ws = con.workspace()
        found[con.id] = LiveGeom(
            x=int(con.rect.x),
            y=int(con.rect.y),
            floating=con.floating or "off",
            workspace=ws.name if ws else None,
        )
    return found


def _focused_window_id(tree) -> int | None:
    con = tree.find_focused()
    if not con or not getattr(con, "window", None):
        return None
    return int(con.id)


def _near(x: int, y: int, tx: int, ty: int, tol: int = _ARRIVE_PX) -> bool:
    return abs(x - tx) <= tol and abs(y - ty) <= tol


def _command_error(reply) -> str | None:
    if not reply:
        return None
    bad: list[str] = []
    for item in reply:
        success = getattr(item, "success", True)
        if success:
            continue
        bad.append(getattr(item, "error", None) or "command failed")
    return "; ".join(bad) if bad else None


class GroupFollow:
    """Runs one-shot syncs and the optional poll loop.

    ``_ipc_lock`` is always taken before ``_pool_lock``. i3 IPC runs without
    the pool lock so window events are not stuck behind a round trip.
    """

    def __init__(self, pool: WindowPool):
        self.pool = pool
        self._pool_lock = threading.Lock()
        self._ipc_lock = threading.Lock()
        self._watched: set[str] = set()
        self._interval = _DEFAULT_INTERVAL_S
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn = None
        self._misses: dict[str, int] = {}
        self._ambiguous_sig: tuple | None = None
        self.stats: dict[str, Any] = {
            "ticks": 0,
            "moves": 0,
            "last_poll_ms": 0.0,
            "last_ipc_ms": 0.0,
            "last_action": None,
            "last_dx": 0,
            "last_dy": 0,
            "slow_ticks": 0,
            "unconfirmed": 0,
            "gave_up": 0,
        }

    # ------------------------------------------------------------------
    # pool lock, shared with the daemon event thread
    # ------------------------------------------------------------------
    def pool_lock(self) -> threading.Lock:
        return self._pool_lock

    def drop_window(self, con_id: int) -> None:
        self.pool.drop_anchors_for(con_id)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._close_conn()

    # ------------------------------------------------------------------
    # command entry
    # ------------------------------------------------------------------
    def handle(self, req: dict) -> dict[str, Any]:
        anchor = bool(req.get("anchor"))
        clear_anchor = bool(req.get("clear_anchor"))
        watch = bool(req.get("watch"))
        unwatch = bool(req.get("unwatch"))
        status = bool(req.get("status"))
        group = req.get("group")
        con_id = req.get("id")
        interval_ms = req.get("interval_ms")

        if status and (anchor or clear_anchor or watch or unwatch or group or con_id is not None):
            return {"ok": False, "error": "--status does not take other arguments"}
        if unwatch and (watch or anchor or clear_anchor):
            return {"ok": False, "error": "--unwatch only stops watching"}
        if anchor and clear_anchor:
            return {"ok": False, "error": "--anchor and --clear-anchor conflict"}
        if interval_ms is not None and not watch:
            return {"ok": False, "error": "--interval is only used with --watch"}

        if status:
            return self._status()

        reply: dict[str, Any] = {"ok": True}
        if clear_anchor:
            cleared = self._clear_anchor(group, con_id)
            if not cleared.get("ok"):
                return cleared
            reply.update(cleared)
        if anchor:
            armed = self._set_anchor(group, con_id)
            if not armed.get("ok"):
                return armed
            reply.update(armed)
        if unwatch:
            reply.update(self._unwatch(group))
        if watch:
            if not group and not reply.get("group"):
                return {"ok": False, "error": "--watch needs --group"}
            watched = self._watch(reply.get("group", group), interval_ms)
            if not watched.get("ok"):
                return watched
            reply.update(watched)
        if not any((anchor, clear_anchor, watch, unwatch, status)):
            return self._sync_once(group, con_id)
        return reply

    def _status(self) -> dict[str, Any]:
        with self._pool_lock:
            watching = sorted(self._watched)
            anchors = dict(self.pool.group_anchors)
            interval_ms = int(self._interval * 1000)
            stats = dict(self.stats)
        return {
            "ok": True,
            "action": "status",
            "watching": watching,
            "interval_ms": interval_ms,
            "anchors": anchors,
            "stats": stats,
        }

    def _clear_anchor(self, group: str | None, con_id: int | None) -> dict[str, Any]:
        with self._pool_lock:
            name, err = self._resolve_group_name(group, con_id)
            if err:
                return {"ok": False, "error": err}
            assert name is not None
            self.pool.clear_anchor(name)
            self.pool.save_snapshot()
        log.info("follow anchor cleared group=%s", name)
        return {"ok": True, "action": "anchor-cleared", "group": name}

    def _set_anchor(self, group: str | None, con_id: int | None) -> dict[str, Any]:
        with self._ipc_lock:
            conn = self._connection()
            tree = conn.get_tree()
            live = index_tree(tree)
            if con_id is None:
                con_id = _focused_window_id(tree)
            if con_id is None:
                return {"ok": False, "error": "no focused window"}
            with self._pool_lock:
                rec = self._record(con_id, live)
                if rec is None:
                    return {"ok": False, "error": f"unknown id {con_id}"}
                if group:
                    if group not in rec.groups:
                        rec.groups.append(group)
                    name = group
                else:
                    name, err = self._resolve_group_name(None, con_id)
                    if err or name is None:
                        return {"ok": False, "error": err or "no group"}
                capture_origin(rec)
                self.pool.set_anchor(name, rec.id)
                anchor_id = rec.id
                self.pool.save_snapshot()
        log.info("follow anchor group=%s id=%s", name, anchor_id)
        return {"ok": True, "action": "anchor", "group": name, "id": anchor_id}

    def _watch(self, group: str, interval_ms: int | None) -> dict[str, Any]:
        with self._pool_lock:
            members = self.pool.by_group(group)
            if not members:
                return {"ok": False, "error": f"group {group} has no windows"}
            for rec in members:
                capture_origin(rec)
            ids = [rec.id for rec in members]
            if interval_ms is not None:
                self._interval = max(_MIN_INTERVAL_S, int(interval_ms) / 1000.0)
            self._watched.add(group)
            self._ensure_thread()
            interval = int(self._interval * 1000)
        log.info("follow watch on group=%s interval_ms=%s members=%s", group, interval, ids)
        return {
            "ok": True,
            "action": "watch",
            "group": group,
            "interval_ms": interval,
            "members": ids,
        }

    def _unwatch(self, group: str | None) -> dict[str, Any]:
        with self._pool_lock:
            if group:
                self._watched.discard(group)
                stopped = [group]
            else:
                stopped = sorted(self._watched)
                self._watched.clear()
        log.info("follow watch off groups=%s", stopped)
        return {"ok": True, "action": "unwatch", "groups": stopped}

    def _sync_once(self, group: str | None, con_id: int | None) -> dict[str, Any]:
        with self._ipc_lock:
            conn = self._connection()
            t0 = time.perf_counter()
            tree = conn.get_tree()
            poll_ms = (time.perf_counter() - t0) * 1000.0
            if con_id is None:
                con_id = _focused_window_id(tree)
            if con_id is None:
                return {"ok": False, "error": "no focused window"}
            live = index_tree(tree)
            with self._pool_lock:
                rec = self.pool.get(con_id) or self._record(con_id, live)
                if rec is None:
                    return {"ok": False, "error": f"unknown id {con_id}"}
                name, err = self._resolve_group_name(group, con_id)
                if err or name is None:
                    return {"ok": False, "error": err or "no group"}
            return self._sync_group(
                conn,
                live,
                name,
                leader_id=con_id,
                poll_ms=poll_ms,
                give_up=False,
            )

    # ------------------------------------------------------------------
    # watch loop
    # ------------------------------------------------------------------
    def _ensure_thread(self) -> None:
        """Caller holds the pool lock. The thread stays up after --unwatch
        and just idles, so a later --watch does not race a thread that has
        already decided to exit."""
        if self._stop.is_set():
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="group-follow", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._pool_lock:
                groups = list(self._watched)
                interval = self._interval
            if not groups:
                if self._stop.wait(0.2):
                    break
                continue
            try:
                self._tick(groups)
            except Exception:
                log.exception("follow tick failed")
            if self._stop.wait(interval):
                break

    def _tick(self, groups: list[str]) -> None:
        with self._ipc_lock:
            conn = self._connection()
            t0 = time.perf_counter()
            tree = conn.get_tree()
            poll_ms = (time.perf_counter() - t0) * 1000.0
            live = index_tree(tree)
            with self._pool_lock:
                self.stats["ticks"] += 1
                self.stats["last_poll_ms"] = round(poll_ms, 2)
            for index, group in enumerate(groups):
                result = self._sync_group(
                    conn,
                    live,
                    group,
                    leader_id=None,
                    poll_ms=poll_ms,
                    give_up=True,
                )
                if result.get("moved") and index + 1 < len(groups):
                    live = index_tree(conn.get_tree())

    def _sync_group(
        self,
        conn,
        live: dict[int, LiveGeom],
        group: str,
        *,
        leader_id: int | None,
        poll_ms: float,
        give_up: bool,
    ) -> dict[str, Any]:
        """Caller holds ``_ipc_lock`` and does not hold ``_pool_lock``."""
        with self._pool_lock:
            members = self.pool.by_group(group)
            if not members:
                return {"ok": False, "error": f"group {group} has no windows", "group": group}
            snaps: list[SnapMember] = []
            for rec in members:
                origin = capture_origin(rec)
                geom = live.get(rec.id)
                snaps.append(
                    SnapMember(
                        id=rec.id,
                        origin_x=origin[0] if origin else None,
                        origin_y=origin[1] if origin else None,
                        live_x=geom.x if geom else None,
                        live_y=geom.y if geom else None,
                        floating=geom.floating if geom else rec.floating,
                        workspace=geom.workspace if geom else rec.workspace,
                    )
                )
            anchor_id = self.pool.group_anchors.get(group)
            plan = plan_sync(snaps, leader_id=leader_id, anchor_id=anchor_id)

        if plan.action in ("settled", "not-anchor"):
            with self._pool_lock:
                self._commit_settle(plan)
            log.info(
                "follow group=%s action=%s leader=%s",
                group, plan.action, plan.leader_id,
            )
            return self._plan_reply(group, plan, poll_ms, 0.0)

        if plan.action == "ambiguous":
            sig = tuple(sorted(item["id"] for item in plan.skipped))
            if sig != self._ambiguous_sig:
                self._ambiguous_sig = sig
                log.warning("follow group=%s ambiguous ids=%s", group, list(sig))
            return self._plan_reply(group, plan, poll_ms, 0.0, ok=False)

        if plan.action in ("no-window", "no-geometry", "leader-not-floating"):
            log.info("follow group=%s action=%s error=%s", group, plan.action, plan.error)
            return self._plan_reply(group, plan, poll_ms, 0.0, ok=False)

        if plan.action == "unchanged" or (plan.dx == 0 and plan.dy == 0 and not plan.moves):
            return self._plan_reply(group, plan, poll_ms, 0.0)

        cmd = move_command(plan.moves)
        ipc_ms = 0.0
        observed = live
        cmd_error = None
        if cmd:
            t1 = time.perf_counter()
            try:
                reply = conn.command(cmd)
            except Exception as exc:
                log.exception("follow command failed group=%s", group)
                return {"ok": False, "error": str(exc), "group": group, "action": "command-error"}
            cmd_error = _command_error(reply)
            tree = conn.get_tree()
            observed = index_tree(tree)
            ipc_ms = (time.perf_counter() - t1) * 1000.0

        landed = cmd_error is None and self._moves_landed(plan, observed)
        with self._pool_lock:
            self.stats["last_poll_ms"] = round(poll_ms, 2)
            self.stats["last_ipc_ms"] = round(ipc_ms, 2)
            self.stats["last_action"] = plan.action
            self.stats["last_dx"] = plan.dx
            self.stats["last_dy"] = plan.dy
            interval_ms = self._interval * 1000.0
            if landed:
                self._misses[group] = 0
                self._commit_settle(plan)
                # Prefer the position i3 actually has, in case it clamped.
                self._commit_observed(plan, observed)
                if plan.moves:
                    self.stats["moves"] += 1
                # Watch ticks would rewrite the snapshot on every motion.
                # One-shot is rare; persist that baseline. Watch state is
                # saved when the daemon shuts down.
                if not give_up:
                    self.pool.save_snapshot()
            else:
                self.stats["unconfirmed"] += 1
                if give_up:
                    self._misses[group] = self._misses.get(group, 0) + 1
                if give_up and self._misses[group] >= _GIVE_UP_POLLS:
                    self._snap_observed(plan, observed)
                    self._misses[group] = 0
                    self.stats["gave_up"] += 1
                    plan.action = "gave-up"
            slow = give_up and (poll_ms + ipc_ms) >= max(40.0, interval_ms * 0.8)
            if slow:
                self.stats["slow_ticks"] += 1

        positions = self._positions(plan, observed)
        if plan.action == "gave-up":
            log.warning(
                "follow group=%s gave up after %d polls leader=%s dx=%d dy=%d ipc_ms=%.1f",
                group, _GIVE_UP_POLLS, plan.leader_id, plan.dx, plan.dy, ipc_ms,
            )
        elif not landed:
            log.warning(
                "follow group=%s unconfirmed leader=%s dx=%d dy=%d ipc_ms=%.1f error=%s",
                group, plan.leader_id, plan.dx, plan.dy, ipc_ms, cmd_error,
            )
        else:
            log.info(
                "follow group=%s action=%s leader=%s dx=%d dy=%d moved=%d poll_ms=%.1f ipc_ms=%.1f",
                group, plan.action, plan.leader_id, plan.dx, plan.dy,
                len(plan.moves), poll_ms, ipc_ms,
            )
        if slow:
            log.warning(
                "follow slow group=%s poll_ms=%.1f ipc_ms=%.1f interval_ms=%d",
                group, poll_ms, ipc_ms, int(interval_ms),
            )

        out = self._plan_reply(group, plan, poll_ms, ipc_ms, ok=landed or plan.action == "gave-up")
        out["positions"] = positions
        if cmd_error:
            out["ok"] = False
            out["error"] = cmd_error
        elif not landed and plan.action != "gave-up":
            out["action"] = "unconfirmed"
            out["ok"] = False
            out["error"] = "move command returned but the tree does not show the new positions"
        return out

    def _moves_landed(self, plan: Plan, observed: dict[int, LiveGeom]) -> bool:
        for move in plan.moves:
            geom = observed.get(move.id)
            if geom is None or not _near(geom.x, geom.y, move.target_x, move.target_y):
                return False
        return True

    def _commit_settle(self, plan: Plan) -> None:
        for con_id, x, y in plan.settle:
            rec = self.pool.get(con_id)
            if rec is not None:
                write_origin(rec, x, y)

    def _commit_observed(self, plan: Plan, observed: dict[int, LiveGeom]) -> None:
        """Leader keeps the position the delta was measured from. Peers keep
        whatever i3 actually applied, so a clamped move does not drift."""
        for move in plan.moves:
            geom = observed.get(move.id)
            rec = self.pool.get(move.id)
            if geom is not None and rec is not None:
                write_origin(rec, geom.x, geom.y)

    def _snap_observed(self, plan: Plan, observed: dict[int, LiveGeom]) -> None:
        ids = [plan.leader_id] if plan.leader_id is not None else []
        ids.extend(move.id for move in plan.moves)
        for con_id in ids:
            geom = observed.get(con_id)
            rec = self.pool.get(con_id) if con_id is not None else None
            if geom is not None and rec is not None:
                write_origin(rec, geom.x, geom.y)

    def _positions(self, plan: Plan, observed: dict[int, LiveGeom]) -> list[dict[str, int]]:
        ids = []
        if plan.leader_id is not None:
            ids.append(plan.leader_id)
        ids.extend(move.id for move in plan.moves)
        rows = []
        for con_id in ids:
            geom = observed.get(con_id)
            if geom is None:
                continue
            rows.append({"id": con_id, "x": geom.x, "y": geom.y})
        return rows

    def _plan_reply(
        self,
        group: str,
        plan: Plan,
        poll_ms: float,
        ipc_ms: float,
        ok: bool = True,
    ) -> dict[str, Any]:
        reply: dict[str, Any] = {
            "ok": ok,
            "action": plan.action,
            "group": group,
            "leader": plan.leader_id,
            "dx": plan.dx,
            "dy": plan.dy,
            "moved": [move.id for move in plan.moves],
            "skipped": plan.skipped,
            "poll_ms": round(poll_ms, 2),
            "ipc_ms": round(ipc_ms, 2),
        }
        if plan.note:
            reply["note"] = plan.note
        if plan.error and not ok:
            reply["error"] = plan.error
        return reply

    def _resolve_group_name(
        self, group: str | None, con_id: int | None
    ) -> tuple[str | None, str | None]:
        """Caller holds the pool lock."""
        if group:
            return group, None
        if con_id is None:
            return None, "pass --group"
        rec = self.pool.get(con_id)
        if rec is None:
            return None, f"unknown id {con_id}"
        if not rec.groups:
            return None, f"window {con_id} is not in a group"
        if len(rec.groups) > 1:
            names = ", ".join(rec.groups)
            return None, f"window {con_id} is in several groups ({names}); pass --group"
        return rec.groups[0], None

    def _record(self, con_id: int, live: dict[int, LiveGeom]) -> WindowRecord | None:
        """Caller holds the pool lock. Insert a stub if the daemon has not
        seen this window yet but i3 has it."""
        rec = self.pool.get(con_id)
        if rec is not None:
            return rec
        geom = live.get(con_id)
        if geom is None:
            return None
        rec = WindowRecord(
            id=con_id,
            window=None,
            name="",
            window_class=None,
            instance=None,
            floating=geom.floating,
            workspace=geom.workspace,
        )
        from .pool import Geometry

        rec.geometry = Geometry(x=geom.x, y=geom.y, width=0, height=0)
        rec.push_geometry(rec.geometry)
        self.pool.insert(rec)
        return rec

    def _connection(self):
        """Caller holds ``_ipc_lock``. Separate from the daemon subscription
        socket so a poll during a drag does not share that socket."""
        if self._conn is None:
            from i3ipc import Connection

            self._conn = Connection()
        return self._conn

    def _close_conn(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        sock = getattr(conn, "_cmd_socket", None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
