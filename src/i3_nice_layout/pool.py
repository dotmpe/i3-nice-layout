"""In-memory window pool with geometry history.

The pool is the single source of truth that both the daemon and other scripts
can read from.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Geometry:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_rect(cls, rect: dict[str, int]) -> Geometry:
        return cls(
            x=rect.get("x", 0),
            y=rect.get("y", 0),
            width=rect.get("width", 0),
            height=rect.get("height", 0),
        )


@dataclass
class WindowRecord:
    id: int                          # i3 container id
    window: int | None               # X11 window id (None for pure containers)
    name: str
    window_class: str | None
    instance: str | None
    marks: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)          # user tags
    groups: list[str] = field(default_factory=list)        # group membership
    floating: str = "off"            # "off" | "user_on" | "auto_on" …
    workspace: str | None = None
    output: str | None = None
    geometry: Geometry | None = None
    geometry_history: list[tuple[float, Geometry]] = field(default_factory=list)
    rolled_up: bool = False
    stand_in_id: int | None = None   # container id of the pseudo-titlebar
    extra: dict[str, Any] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.time)

    def push_geometry(self, geo: Geometry, limit: int = 8) -> None:
        self.geometry = geo
        self.geometry_history.append((time.time(), geo))
        if len(self.geometry_history) > limit:
            self.geometry_history = self.geometry_history[-limit:]
        self.last_seen = time.time()


class WindowPool:
    """Simple dict-backed pool. Thread-safety is not required for the
    single-threaded i3ipc event loop we will use."""

    def __init__(self, snapshot_path: Path | None = None):
        self._windows: dict[int, WindowRecord] = {}
        self.snapshot_path = snapshot_path

    # ------------------------------------------------------------------
    # basic CRUD
    # ------------------------------------------------------------------
    def insert(self, rec: WindowRecord) -> None:
        self._windows[rec.id] = rec

    def get(self, con_id: int) -> WindowRecord | None:
        return self._windows.get(con_id)

    def remove(self, con_id: int) -> None:
        self._windows.pop(con_id, None)

    def all(self) -> list[WindowRecord]:
        return list(self._windows.values())

    def by_mark(self, mark: str) -> list[WindowRecord]:
        return [w for w in self._windows.values() if mark in w.marks]

    def by_tag(self, tag: str) -> list[WindowRecord]:
        return [w for w in self._windows.values() if tag in w.tags]

    def by_group(self, group: str) -> list[WindowRecord]:
        return [w for w in self._windows.values() if group in w.groups]

    def floating(self) -> list[WindowRecord]:
        return [w for w in self._windows.values() if w.floating != "off"]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save_snapshot(self) -> None:
        if not self.snapshot_path:
            return
        data = {
            "ts": time.time(),
            "windows": [asdict(w) for w in self._windows.values()],
        }
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_path.write_text(json.dumps(data, indent=2, default=str))

    def load_snapshot(self) -> None:
        if not self.snapshot_path or not self.snapshot_path.exists():
            return
        try:
            raw = json.loads(self.snapshot_path.read_text())
            for item in raw.get("windows", []):
                geo = item.get("geometry")
                if geo:
                    item["geometry"] = Geometry(**geo)
                hist = []
                for ts, g in item.get("geometry_history", []):
                    hist.append((ts, Geometry(**g) if isinstance(g, dict) else g))
                item["geometry_history"] = hist
                rec = WindowRecord(**item)
                self._windows[rec.id] = rec
        except Exception:
            # corrupt snapshot → start empty
            pass
