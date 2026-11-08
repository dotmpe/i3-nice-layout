"""Roll-up (stand-in variant): hide a window on the scratchpad and leave a
title window in its place.

The title is another program, spawned from an argv template. The default
comes from the environment variable ``I3_NICE_STAND_IN``. A typical value::

    i3-nice-gui title --for {id} --title {name} --x {x} --y {y} \\
        --width {width} --height {height}

Placeholders are ``{id}`` ``{name}`` ``{x}`` ``{y}`` ``{width}`` ``{height}``
``{class}`` ``{instance}`` ``{workspace}``. ``{height}`` is the title strip
(28px), not the window's full height. Substitution happens after the
template is split, so a title with spaces stays one argument.

The new window is matched by ``_NET_WM_PID`` (read with ``xprop``, because
i3's tree does not carry the pid). ``WM_CLASS`` class ``i3-nice-gui`` /
instance ``title`` is the backup when the pid property cannot be read.
The wait is ``MATCH_TIMEOUT_S`` (2s). The real window is scratchpadded
only after the stand-in is placed. A miss leaves it where it was.

Unroll puts the real window back at the stand-in's current x, y and the
cached width and height, then signals the stand-in process. Closing the
stand-in is the same unroll. Closing the real window signals the stand-in.
Both paths are idempotent: killing the stand-in produces a close event
that finds the link already cleared.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from i3ipc import Connection

from .pool import Geometry, WindowPool, WindowRecord
from .rollup_geom import DEFAULT_ROLLED_HEIGHT

log = logging.getLogger("i3-nice-layout.rollup-altui")

ENV_STAND_IN = "I3_NICE_STAND_IN"
MATCH_TIMEOUT_S = 2.0
MATCH_POLL_S = 0.05
WM_CLASS = "i3-nice-gui"
WM_INSTANCE = "title"
MARK_PREFIX = "rollup_standin_for_"
ROLLUP_KIND = "stand-in"

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_PID_VALUE = re.compile(r"=\s*(\d+)")
_inflight: set[int] = set()
_pending_unroll: set[int] = set()
_inflight_guard = threading.Lock()


@dataclass
class CloseFollowUp:
    """Work to do after a window::close has been removed from the pool."""

    kind: str  # "unroll" | "kill"
    owner_id: int | None
    stand_in_pid: int | None
    stand_in_id: int | None
    x: int | None
    y: int | None


def mark_for(con_id: int) -> str:
    return f"{MARK_PREFIX}{con_id}"


def split_template(raw: str) -> list[str]:
    """Split a command template. Raises ValueError on broken quotes."""
    return shlex.split(raw, posix=True)


def render_argv(template: list[str], values: dict[str, str]) -> list[str]:
    """Substitute placeholders in an already-split argv.

    Unknown ``{names}`` raise KeyError. Values are inserted as text, so
    braces inside a window title are not parsed again.
    """

    def one(token: str) -> str:
        def sub(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                raise KeyError(key)
            return values[key]

        return _PLACEHOLDER.sub(sub, token)

    return [one(token) for token in template]


def resolve_template(command: str | None) -> str | None:
    """Request override, else ``I3_NICE_STAND_IN``, else nothing."""
    if command and command.strip():
        return command.strip()
    env = os.environ.get(ENV_STAND_IN, "")
    if env.strip():
        return env.strip()
    return None


def owner_of(
    pool: WindowPool,
    stand_in_id: int,
    marks: list[str] | None = None,
) -> WindowRecord | None:
    """Real window a stand-in stands for, if the link is still stored."""
    for rec in pool.all():
        if rec.stand_in_id == stand_in_id and rec.extra.get("rollup_kind") == ROLLUP_KIND:
            return rec
    stand = pool.get(stand_in_id)
    if stand:
        real_id = stand.extra.get("stands_for")
        if isinstance(real_id, int):
            real = pool.get(real_id)
            if real is not None and real.extra.get("rollup_kind") == ROLLUP_KIND:
                return real
    for mark in marks or []:
        if not mark.startswith(MARK_PREFIX):
            continue
        try:
            real_id = int(mark[len(MARK_PREFIX):])
        except ValueError:
            continue
        real = pool.get(real_id)
        if real is not None and real.extra.get("rollup_kind") == ROLLUP_KIND and real.rolled_up:
            return real
    return None


def wants_altui(
    pool: WindowPool,
    i3: Connection,
    con_id: int | None,
    stand_in: bool,
) -> bool:
    """True when this toggle is the stand-in variant.

    ``--stand-in`` always selects it. A later toggle of a window that was
    rolled this way (or of the stand-in itself) selects it too, so a click
    command without the flag still restores the real window.
    """
    if stand_in:
        return True
    target = _focused_id(i3, con_id)
    if target is None:
        return False
    rec = pool.get(target)
    if rec is not None and rec.rolled_up and rec.extra.get("rollup_kind") == ROLLUP_KIND:
        return True
    return owner_of(pool, target) is not None


def note_close(pool: WindowPool, con) -> CloseFollowUp | None:
    """Decide follow-up for a window that is about to leave the pool.

    Called under the pool lock, before the record is removed. i3 commands
    happen later, outside the lock.
    """
    owner = owner_of(pool, con.id, list(con.marks) if getattr(con, "marks", None) else None)
    if owner is not None and owner.rolled_up:
        rect = getattr(con, "rect", None)
        return CloseFollowUp(
            kind="unroll",
            owner_id=owner.id,
            stand_in_pid=_pid_of(owner),
            stand_in_id=con.id,
            x=rect.x if rect is not None else None,
            y=rect.y if rect is not None else None,
        )
    rec = pool.get(con.id)
    if rec is not None and rec.rolled_up and rec.extra.get("rollup_kind") == ROLLUP_KIND:
        pid = _pid_of(rec)
        if pid or rec.stand_in_id:
            return CloseFollowUp(
                kind="kill",
                owner_id=rec.id,
                stand_in_pid=pid,
                stand_in_id=rec.stand_in_id,
                x=None,
                y=None,
            )
    return None


def after_close(
    i3: Connection,
    pool: WindowPool,
    follow: CloseFollowUp,
    lock: threading.Lock | None = None,
    ender: Callable[[int], None] | None = None,
) -> None:
    """Run the close follow-up outside the pool lock."""
    end = ender or end_process
    if follow.kind == "kill":
        if follow.stand_in_pid:
            end(follow.stand_in_pid)
        elif follow.stand_in_id:
            i3.command(f'[con_id="{follow.stand_in_id}"] kill')
        return
    if follow.owner_id is None:
        return
    held = _acquire(lock)
    try:
        rec = pool.get(follow.owner_id)
    finally:
        _release_lock(held)
    if rec is None:
        if follow.stand_in_pid:
            end(follow.stand_in_pid)
        return
    at = (follow.x, follow.y) if follow.x is not None and follow.y is not None else None
    _unroll(i3, pool, rec, lock, at=at, ender=end)


def toggle(
    i3: Connection,
    pool: WindowPool,
    con_id: int | None = None,
    command: str | None = None,
    lock: threading.Lock | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    pid_of: Callable[[int], int | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    ender: Callable[[int], None] | None = None,
    timeout: float = MATCH_TIMEOUT_S,
) -> dict[str, Any]:
    """Toggle the stand-in roll-up for the focused window, or ``con_id``."""
    target = _focused_id(i3, con_id)
    if target is None:
        return {"ok": False, "error": "no focused window"}

    held = _acquire(lock)
    try:
        owner = owner_of(pool, target)
        rec = pool.get(target)
    finally:
        _release_lock(held)
    if owner is not None:
        return _unroll(i3, pool, owner, lock, ender=ender or end_process)

    if rec is None:
        rec, err = _load_record(i3, pool, target, lock)
        if err:
            return {"ok": False, "error": err}
        assert rec is not None

    if rec.rolled_up and rec.extra.get("rollup_kind") == ROLLUP_KIND:
        return _unroll(i3, pool, rec, lock, ender=ender or end_process)
    if rec.rolled_up:
        return {
            "ok": False,
            "error": "window is rolled up by the geometry command",
            "id": rec.id,
        }
    return _roll(
        i3,
        pool,
        rec,
        command,
        lock,
        popen=popen,
        pid_of=pid_of or net_wm_pid,
        sleep=sleep,
        clock=clock,
        ender=ender or end_process,
        timeout=timeout,
    )


def end_process(pid: int) -> None:
    """SIGTERM the stand-in. Spawned with start_new_session, so the pid is the group."""
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            log.info("could not signal stand-in pid=%s", pid)


def net_wm_pid(x_window: int) -> int | None:
    """Read ``_NET_WM_PID`` from an X window. None when xprop cannot say."""
    try:
        out = subprocess.run(
            ["xprop", "-id", str(int(x_window)), "_NET_WM_PID"],
            capture_output=True,
            text=True,
            timeout=0.4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _PID_VALUE.search(out.stdout or "")
    if not match:
        return None
    return int(match.group(1))


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _pid_of(rec: WindowRecord) -> int | None:
    pid = rec.extra.get("stand_in_pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def _acquire(lock: threading.Lock | None):
    if lock is None:
        return None
    lock.acquire()
    return lock


def _release_lock(held) -> None:
    if held is not None:
        held.release()


def _claim(con_id: int) -> bool:
    with _inflight_guard:
        if con_id in _inflight:
            return False
        _inflight.add(con_id)
        return True


def _unclaim(con_id: int) -> None:
    with _inflight_guard:
        _inflight.discard(con_id)


def _defer_unroll(con_id: int) -> None:
    with _inflight_guard:
        _pending_unroll.add(con_id)


def _take_deferred(con_id: int) -> bool:
    with _inflight_guard:
        if con_id not in _pending_unroll:
            return False
        _pending_unroll.discard(con_id)
        return True


def _focused_id(i3: Connection, con_id: int | None) -> int | None:
    if con_id is not None:
        return con_id
    focused = i3.get_tree().find_focused()
    if not focused or not focused.window:
        return None
    return focused.id


def _commands_ok(replies) -> bool:
    if not replies:
        return True
    return all(getattr(reply, "success", True) for reply in replies)


def _load_record(
    i3: Connection,
    pool: WindowPool,
    con_id: int,
    lock: threading.Lock | None,
) -> tuple[WindowRecord | None, str | None]:
    held = _acquire(lock)
    try:
        rec = pool.get(con_id)
    finally:
        _release_lock(held)
    if rec is not None:
        return rec, None
    con = i3.get_tree().find_by_id(con_id)
    if not con:
        return None, f"container {con_id} not found"
    from .daemon import con_to_record

    fresh = con_to_record(con)
    held = _acquire(lock)
    try:
        existing = pool.get(con_id)
        if existing is not None:
            return existing, None
        if fresh.geometry:
            fresh.push_geometry(fresh.geometry)
        pool.insert(fresh)
        return fresh, None
    finally:
        _release_lock(held)


def _placeholder_values(rec: WindowRecord, geo: Geometry, title_h: int) -> dict[str, str]:
    return {
        "id": str(rec.id),
        "name": rec.name or "",
        "x": str(int(geo.x)),
        "y": str(int(geo.y)),
        "width": str(int(geo.width)),
        "height": str(int(title_h)),
        "class": rec.window_class or "",
        "instance": rec.instance or "",
        "workspace": rec.workspace or "",
    }


def _refresh_geometry(
    i3: Connection,
    pool: WindowPool,
    rec: WindowRecord,
    lock: threading.Lock | None,
) -> tuple[WindowRecord | None, str | None]:
    """Re-read the live rectangle so the template and the cache match."""
    con = i3.get_tree().find_by_id(rec.id)
    live: Geometry | None = None
    workspace: str | None = None
    if con is not None and con.rect:
        live = Geometry(
            x=int(con.rect.x),
            y=int(con.rect.y),
            width=int(con.rect.width),
            height=int(con.rect.height),
        )
        ws = con.workspace() if hasattr(con, "workspace") else None
        if ws is not None and getattr(ws, "name", None):
            workspace = ws.name
    held = _acquire(lock)
    try:
        current = pool.get(rec.id)
        if current is None:
            return None, "window closed"
        if live is not None and live.width > 0 and live.height > 0:
            current.push_geometry(live)
        if workspace:
            current.workspace = workspace
        if current.geometry is None or current.geometry.width <= 0:
            return None, "no geometry available to cache"
        return current, None
    finally:
        _release_lock(held)


def _window_nodes(tree) -> list:
    nodes = []
    for con in tree.descendants():
        if getattr(con, "window", None):
            nodes.append(con)
    return nodes


def _is_title_class(con) -> bool:
    return con.window_class == WM_CLASS and con.window_instance == WM_INSTANCE


def _match_stand_in(
    i3: Connection,
    pid: int,
    before_ids: set[int],
    pid_of: Callable[[int], int | None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout: float,
    proc,
) -> tuple[Any | None, str | None]:
    """Poll the tree until the spawned pid's window shows, or time runs out.

    Pid match wins. A single new ``i3-nice-gui`` / ``title`` window whose
    pid property never becomes readable is the backup. Two such windows
    are not guessed between.
    """
    deadline = clock() + timeout
    backups: set[int] = set()
    while True:
        tree = i3.get_tree()
        backups = set()
        for con in _window_nodes(tree):
            is_new = con.id not in before_ids
            is_title = _is_title_class(con)
            if not is_new and not is_title:
                continue
            wpid = pid_of(int(con.window))
            if wpid == pid and (is_title or is_new):
                return con, None
            if is_new and is_title and wpid is None:
                backups.add(con.id)
        if proc.poll() is not None:
            return None, "stand-in process exited before its window mapped"
        if clock() >= deadline:
            break
        sleep(MATCH_POLL_S)
    if len(backups) == 1:
        con = i3.get_tree().find_by_id(next(iter(backups)))
        if con is not None:
            log.info("stand-in matched by WM_CLASS; _NET_WM_PID was unreadable pid=%s", pid)
            return con, None
    if len(backups) > 1:
        log.info("stand-in WM_CLASS backup is ambiguous pid=%s ids=%s", pid, sorted(backups))
        return None, "stand-in window match is ambiguous"
    return None, "stand-in window did not appear"


def _place_stand_in(i3: Connection, stand_id: int, geo: Geometry, title_h: int, mark: str):
    cmd = (
        f'[con_id="{stand_id}"] floating enable, border none, '
        f"move absolute position {int(geo.x)} {int(geo.y)}, "
        f"resize set {int(geo.width)} {int(title_h)}, "
        f"mark --add {mark}"
    )
    return i3.command(cmd), cmd


def _link_stand_in(
    pool: WindowPool,
    rec: WindowRecord,
    stand_id: int,
    pid: int,
    mark: str,
    stand_con,
) -> None:
    rec.rolled_up = True
    rec.stand_in_id = stand_id
    rec.extra["rollup_kind"] = ROLLUP_KIND
    rec.extra["rollup_mark"] = mark
    rec.extra["stand_in_pid"] = pid
    stand = pool.get(stand_id)
    if stand is None and stand_con is not None:
        rect = getattr(stand_con, "rect", None)
        geo = None
        if rect is not None:
            geo = Geometry(
                x=int(rect.x),
                y=int(rect.y),
                width=int(rect.width),
                height=int(rect.height),
            )
        stand = WindowRecord(
            id=stand_con.id,
            window=getattr(stand_con, "window", None),
            name=getattr(stand_con, "name", None) or "",
            window_class=getattr(stand_con, "window_class", None),
            instance=getattr(stand_con, "window_instance", None),
            marks=list(getattr(stand_con, "marks", None) or []),
            floating=getattr(stand_con, "floating", None) or "user_on",
            geometry=geo,
        )
        pool.insert(stand)
    if stand is not None:
        stand.extra["stands_for"] = rec.id
        stand.extra["role"] = ROLLUP_KIND
        if mark not in stand.marks:
            stand.marks.append(mark)


def _drop_failed_roll(pool: WindowPool, con_id: int, lock: threading.Lock | None) -> None:
    """Undo a link when the stand-in was spawned but the window was not hidden."""
    held = _acquire(lock)
    try:
        current = pool.get(con_id)
        if current is not None and current.extra.get("rollup_kind") == ROLLUP_KIND:
            _clear_link(pool, current)
            pool.save_snapshot()
    finally:
        _release_lock(held)


def _clear_link(pool: WindowPool, rec: WindowRecord) -> tuple[int | None, int | None]:
    pid = _pid_of(rec)
    sid = rec.stand_in_id
    rec.rolled_up = False
    rec.stand_in_id = None
    rec.extra.pop("stand_in_pid", None)
    rec.extra.pop("rollup_mark", None)
    rec.extra.pop("rollup_kind", None)
    if sid is not None:
        stand = pool.get(sid)
        if stand is not None:
            stand.extra.pop("stands_for", None)
            stand.extra.pop("role", None)
    return pid, sid


def _roll(
    i3: Connection,
    pool: WindowPool,
    rec: WindowRecord,
    command: str | None,
    lock: threading.Lock | None,
    popen: Callable[..., Any],
    pid_of: Callable[[int], int | None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    ender: Callable[[int], None],
    timeout: float,
) -> dict[str, Any]:
    con_id = rec.id
    if not _claim(con_id):
        return {"ok": False, "error": "roll-up already in progress", "id": con_id}
    proc = None
    keep = False
    try:
        rec, err = _refresh_geometry(i3, pool, rec, lock)
        if err or rec is None or rec.geometry is None:
            return {"ok": False, "error": err or "no geometry available to cache", "id": con_id}
        template = resolve_template(command)
        if template is None:
            return {
                "ok": False,
                "error": f"no stand-in command (set {ENV_STAND_IN} or pass stand_in_cmd)",
                "id": rec.id,
            }
        try:
            argv_t = split_template(template)
        except ValueError as exc:
            return {"ok": False, "error": f"stand-in command: {exc}", "id": rec.id}
        if not argv_t:
            return {"ok": False, "error": "stand-in command is empty", "id": rec.id}
        try:
            argv = render_argv(argv_t, _placeholder_values(rec, rec.geometry, DEFAULT_ROLLED_HEIGHT))
        except KeyError as exc:
            return {"ok": False, "error": f"unknown stand-in placeholder {exc}", "id": rec.id}

        before = {node.id for node in _window_nodes(i3.get_tree())}

        try:
            proc = popen(argv, start_new_session=True)
        except OSError as exc:
            log.info("stand-in spawn failed id=%s: %s", rec.id, exc)
            return {"ok": False, "error": f"stand-in command failed: {exc}", "id": rec.id}

        matched, why = _match_stand_in(
            i3, proc.pid, before, pid_of, sleep, clock, timeout, proc,
        )
        if matched is None:
            log.info("stand-in match failed id=%s pid=%s: %s", rec.id, proc.pid, why)
            return {"ok": False, "error": why, "id": rec.id, "pid": proc.pid}

        geo = rec.geometry
        replies, _cmd = _place_stand_in(i3, matched.id, geo, DEFAULT_ROLLED_HEIGHT, mark_for(rec.id))
        if not _commands_ok(replies):
            return {"ok": False, "error": "could not place the stand-in", "id": rec.id, "pid": proc.pid}

        still = i3.get_tree().find_by_id(matched.id)
        if still is None or proc.poll() is not None:
            return {
                "ok": False,
                "error": "stand-in window closed before the real window was hidden",
                "id": rec.id,
                "pid": proc.pid,
            }

        held = _acquire(lock)
        try:
            current = pool.get(rec.id)
            if current is None:
                return {"ok": False, "error": "window closed", "id": rec.id, "pid": proc.pid}
            _link_stand_in(pool, current, matched.id, proc.pid, mark_for(rec.id), still)
            pool.save_snapshot()
            rec = current
        finally:
            _release_lock(held)

        hide = i3.command(f'[con_id="{rec.id}"] move scratchpad')
        if not _commands_ok(hide):
            return {"ok": False, "error": "could not move the window to the scratchpad", "id": rec.id}

        keep = True
        log.info(
            "rolled up con_id=%s (%s) stand_in=%s pid=%s",
            rec.id, rec.name, matched.id, proc.pid,
        )
        return {
            "ok": True,
            "action": "rolled",
            "id": rec.id,
            "stand_in_id": matched.id,
            "pid": proc.pid,
            "rolled_height": DEFAULT_ROLLED_HEIGHT,
        }
    finally:
        if proc is not None and not keep:
            _drop_failed_roll(pool, con_id, lock)
            ender(proc.pid)
        _unclaim(con_id)
        _drain_unroll(con_id, i3, pool, lock, ender)


def _unroll(
    i3: Connection,
    pool: WindowPool,
    rec: WindowRecord,
    lock: threading.Lock | None,
    at: tuple[int, int] | None = None,
    ender: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    end = ender or end_process
    if not _claim(rec.id):
        # A roll (or another unroll) holds this window. It drains the
        # request when it releases, so a stand-in that dies mid-roll
        # still restores the real window if that roll hid it.
        _defer_unroll(rec.id)
        return {"ok": True, "action": "in-progress", "id": rec.id}
    try:
        held = _acquire(lock)
        try:
            current = pool.get(rec.id) or rec
            if not current.rolled_up or current.extra.get("rollup_kind") != ROLLUP_KIND:
                return {"ok": True, "action": "idle", "id": current.id}
            full = current.geometry
            sid = current.stand_in_id
        finally:
            _release_lock(held)

        xy = at
        if sid is not None:
            stand = i3.get_tree().find_by_id(sid)
            if stand is not None and stand.rect:
                xy = (int(stand.rect.x), int(stand.rect.y))

        shown = i3.command(f'[con_id="{rec.id}"] scratchpad show')
        if not _commands_ok(shown):
            log.error("scratchpad show failed con_id=%s", rec.id)
            return {"ok": False, "error": "scratchpad show failed", "id": rec.id}

        if full is not None:
            x = xy[0] if xy is not None else int(full.x)
            y = xy[1] if xy is not None else int(full.y)
            i3.command(
                f'[con_id="{rec.id}"] '
                f"move absolute position {x} {y}, "
                f"resize set {int(full.width)} {int(full.height)}"
            )
            held = _acquire(lock)
            try:
                current = pool.get(rec.id)
                if current is not None and current.geometry is not None:
                    current.push_geometry(Geometry(
                        x=x, y=y, width=int(full.width), height=int(full.height),
                    ))
            finally:
                _release_lock(held)

        held = _acquire(lock)
        try:
            current = pool.get(rec.id) or rec
            pid, stand_id = _clear_link(pool, current)
            pool.save_snapshot()
        finally:
            _release_lock(held)

        if pid:
            end(pid)
        elif stand_id is not None:
            i3.command(f'[con_id="{stand_id}"] kill')

        log.info("unrolled con_id=%s", rec.id)
        return {"ok": True, "action": "unrolled", "id": rec.id}
    finally:
        _unclaim(rec.id)
        _drain_unroll(rec.id, i3, pool, lock, end)


def _drain_unroll(con_id, i3, pool, lock, ender) -> None:
    if not _take_deferred(con_id):
        return
    held = _acquire(lock)
    try:
        current = pool.get(con_id)
    finally:
        _release_lock(held)
    if current is None:
        return
    _unroll(i3, pool, current, lock, ender=ender)
