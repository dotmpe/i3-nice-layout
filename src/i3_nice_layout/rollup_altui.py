"""Roll-up: hide a floating window into the scratchpad and keep a
stand-in titlebar that can restore it.

This is intentionally minimal. The stand-in is currently just another
i3 floating window (XXX: later replace it with a real GTK/Qt bar
that has mouse callbacks).
"""

from __future__ import annotations

import logging
from typing import Any

from i3ipc import Connection

from .pool import WindowPool, WindowRecord

log = logging.getLogger("i3-nice-layout.rollup-altui")


def toggle(i3: Connection, pool: WindowPool, con_id: int | None = None) -> dict[str, Any]:
    """Toggle roll-up for the focused window (or the given container id)."""
    if con_id is None:
        focused = i3.get_tree().find_focused()
        if not focused or not focused.window:
            return {"ok": False, "error": "no focused window"}
        con_id = focused.id

    rec = pool.get(con_id)
    if rec is None:
        # fallback – ask i3 directly
        con = i3.get_tree().find_by_id(con_id)
        if not con:
            return {"ok": False, "error": f"container {con_id} not found"}
        from .daemon import con_to_record
        rec = con_to_record(con)
        pool.insert(rec)

    if rec.rolled_up:
        return _unroll(i3, pool, rec)
    return _roll(i3, pool, rec)


def _roll(i3: Connection, pool: WindowPool, rec: WindowRecord) -> dict[str, Any]:
    """Hide the real window and create a stand-in titlebar."""
    # remember current geometry
    if rec.geometry:
        rec.push_geometry(rec.geometry)

    # move to scratchpad
    i3.command(f'[con_id="{rec.id}"] move scratchpad')

    # create a very simple stand-in: a floating terminal or a dummy window.
    # For a first version we use a tiny scratchpad-friendly placeholder
    # that carries a mark linking it back to the real window.
    mark = f"rollup_standin_for_{rec.id}"
    # spawn a minimal window – user can replace this with a proper titlebar later
    # Here we just mark the concept; a real implementation would launch
    # e.g. a Python GTK bar or even `yad --text=...`.
    # For now we record the intent and leave the visual stand-in as a TODO.
    rec.rolled_up = True
    rec.extra["rollup_mark"] = mark
    pool.insert(rec)
    pool.save_snapshot()

    log.info("rolled up con_id=%s (%s)", rec.id, rec.name)
    return {
        "ok": True,
        "action": "rolled",
        "id": rec.id,
        "note": "stand-in titlebar not yet spawned – geometry is cached",
    }


def _unroll(i3: Connection, pool: WindowPool, rec: WindowRecord) -> dict[str, Any]:
    """Bring the real window back from the scratchpad and restore geometry."""
    i3.command(f'[con_id="{rec.id}"] scratchpad show')

    # restore last known geometry if we have it
    if rec.geometry:
        g = rec.geometry
        i3.command(
            f'[con_id="{rec.id}"] '
            f"move absolute position {g.x} {g.y}, "
            f"resize set {g.width} {g.height}"
        )

    rec.rolled_up = False
    rec.stand_in_id = None
    pool.insert(rec)
    pool.save_snapshot()

    log.info("unrolled con_id=%s", rec.id)
    return {"ok": True, "action": "unrolled", "id": rec.id}
