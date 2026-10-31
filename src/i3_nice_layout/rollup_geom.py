"""Roll-up (geometry variant): shrink a window to a titlebar-sized strip
and later restore the previous geometry.

No scratchpad, no stand-in window, no extra toolkit. The pool remembers
the full geometry so unroll is exact.
"""

from __future__ import annotations

import logging
from typing import Any

from i3ipc import Connection

from .pool import Geometry, WindowPool, WindowRecord

log = logging.getLogger("i3-nice-layout.rollup-geom")

# Default height of a “rolled” window (pixels). Overridable later via config.
DEFAULT_ROLLED_HEIGHT = 28


def toggle(
    i3: Connection,
    pool: WindowPool,
    con_id: int | None = None,
    rolled_height: int = DEFAULT_ROLLED_HEIGHT,
) -> dict[str, Any]:
    """Toggle roll-up for the focused window (or the given container id)."""
    if con_id is None:
        focused = i3.get_tree().find_focused()
        if not focused or not focused.window:
            return {"ok": False, "error": "no focused window"}
        con_id = focused.id

    rec = pool.get(con_id)
    if rec is None:
        con = i3.get_tree().find_by_id(con_id)
        if not con:
            return {"ok": False, "error": f"container {con_id} not found"}
        from .daemon import con_to_record
        rec = con_to_record(con)
        if rec.geometry:
            rec.push_geometry(rec.geometry)
        pool.insert(rec)

    if rec.rolled_up:
        return _unroll(i3, pool, rec)
    return _roll(i3, pool, rec, rolled_height=rolled_height)


def _roll(
    i3: Connection,
    pool: WindowPool,
    rec: WindowRecord,
    rolled_height: int = DEFAULT_ROLLED_HEIGHT,
) -> dict[str, Any]:
    """Shrink window to a titlebar strip; cache full geometry first."""
    # Prefer live geometry from i3 over possibly stale pool data
    con = i3.get_tree().find_by_id(rec.id)
    if con and con.rect:
        live = Geometry(x=con.rect.x, y=con.rect.y, width=con.rect.width, height=con.rect.height)

        rec.push_geometry(live)
    elif rec.geometry:
        rec.push_geometry(rec.geometry)
    else:
        return {"ok": False, "error": "no geometry available to cache"}

    full = rec.geometry
    assert full is not None

    # Keep x/y/width; collapse height
    i3.command(
        f'[con_id="{rec.id}"] '
        f"resize set {full.width} {rolled_height}"
    )

    rec.rolled_up = True
    rec.extra["rolled_height"] = rolled_height
    # full geometry already sits at the end of geometry_history / in .geometry
    pool.insert(rec)
    pool.save_snapshot()

    log.info("rolled up con_id=%s (%s) → h=%d from h=%d", rec.id, rec.name,
             rolled_height, rec.geometry.height)

    return {
        "ok": True,
        "action": "rolled",
        "id": rec.id,
        "full_geometry": {
            "x": full.x, "y": full.y, "width": full.width, "height": full.height
        },
        "rolled_height": rolled_height,
    }


def _unroll(i3: Connection, pool: WindowPool, rec: WindowRecord) -> dict[str, Any]:
    """Restore the last full geometry."""
    # Prefer the geometry we cached at roll time. geometry_history[-1] is the
    # state *after* the last push; we want the full size we stored before
    # shrinking. For simplicity we keep the full size in .geometry at roll
    # time and never overwrite it while rolled_up.
    full = rec.geometry
    if full is None or full.height <= (rec.extra.get("rolled_height") or DEFAULT_ROLLED_HEIGHT):
        # Fallback: look one step back in history
        if len(rec.geometry_history) >= 2:
            full = rec.geometry_history[-2][1]
        else:
            return {"ok": False, "error": "no full geometry cached"}

    i3.command(
        f'[con_id="{rec.id}"] '
        f"move absolute position {full.x} {full.y}, "
        f"resize set {full.width} {full.height}"
    )

    rec.rolled_up = False
    rec.extra.pop("rolled_height", None)
    # refresh live geometry
    rec.push_geometry(full)
    pool.insert(rec)
    pool.save_snapshot()

    log.info("unrolled con_id=%s → %dx%d", rec.id, full.width, full.height)
    return {
        "ok": True,
        "action": "unrolled",
        "id": rec.id,
        "geometry": {
            "x": full.x, "y": full.y, "width": full.width, "height": full.height
        },
    }
