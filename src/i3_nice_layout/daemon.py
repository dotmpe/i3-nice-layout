"""Long-running process that keeps the window pool fresh."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path

from i3ipc import Connection, Event

from .pool import Geometry, WindowPool, WindowRecord

log = logging.getLogger("i3-nice-layout.daemon")

DEFAULT_SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "i3-nice-layout.sock"
DEFAULT_SNAPSHOT = Path.home() / ".cache" / "i3-nice-layout" / "pool.json"


def _rect_to_geo(rect) -> Geometry:
    return Geometry(x=rect.x, y=rect.y, width=rect.width, height=rect.height)


def con_to_record(con) -> WindowRecord:
    """Best-effort conversion from an i3ipc Con to our WindowRecord."""
    marks = list(con.marks) if con.marks else []
    ws = con.workspace()
    output = con.ipc_data.get("output")  # may be absent
    return WindowRecord(
        id=con.id,
        window=con.window,
        name=con.name or "",
        window_class=con.window_class,
        instance=con.window_instance,
        marks=marks,
        floating=con.floating or "off",
        workspace=ws.name if ws else None,
        output=output,
        geometry=_rect_to_geo(con.rect) if con.rect else None,
    )


class Daemon:
    def __init__(
        self,
        socket_path: Path = DEFAULT_SOCKET,
        snapshot_path: Path = DEFAULT_SNAPSHOT,
    ):
        self.socket_path = socket_path
        self.pool = WindowPool(snapshot_path=snapshot_path)
        self.i3 = Connection()
        self._server: socket.socket | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # i3 event handlers
    # ------------------------------------------------------------------
    def _on_window(self, i3, event):
        change = event.change
        con = event.container

        if change in ("new", "focus", "title", "move", "floating", "fullscreen_mode"):
            rec = con_to_record(con)
            existing = self.pool.get(con.id)
            if existing:
                # preserve user tags / groups / roll-up state
                rec.tags = existing.tags
                rec.groups = existing.groups
                rec.rolled_up = existing.rolled_up
                rec.stand_in_id = existing.stand_in_id
                rec.extra = existing.extra
                if rec.geometry:
                    existing.push_geometry(rec.geometry)
                    rec.geometry_history = existing.geometry_history
            else:
                if rec.geometry:
                    rec.push_geometry(rec.geometry)
            self.pool.insert(rec)

        elif change == "close":
            self.pool.remove(con.id)

        # cheap periodic snapshot
        if change in ("new", "close", "move"):
            self.pool.save_snapshot()

    def _seed_pool(self):
        """Populate pool from current tree on startup."""
        tree = self.i3.get_tree()
        for con in tree.descendants():
            if con.window:  # only real windows for now
                rec = con_to_record(con)
                if rec.geometry:
                    rec.push_geometry(rec.geometry)
                self.pool.insert(rec)
        log.info("seeded pool with %d windows", len(self.pool.all()))

    # ------------------------------------------------------------------
    # command socket (very small JSON protocol)
    # ------------------------------------------------------------------
    def _handle_client(self, conn: socket.socket):
        try:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            line = data.decode().strip()
            if not line:
                return
            req = json.loads(line)
            cmd = req.get("cmd")
            reply = self._dispatch(cmd, req)
            conn.sendall((json.dumps(reply) + "\n").encode())
        except Exception as e:
            log.exception("client error")
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            conn.close()

    def _dispatch(self, cmd: str, req: dict):
        if cmd == "ping":
            return {"ok": True, "pong": True}

        if cmd == "pool.list":
            windows = self.pool.all()
            # optional filters (all ANDed)
            if req.get("floating"):
                windows = [w for w in windows if w.floating != "off"]
            if req.get("rolled"):
                windows = [w for w in windows if w.rolled_up]
            if req.get("tag"):
                windows = [w for w in windows if req["tag"] in w.tags]
            if req.get("group"):
                windows = [w for w in windows if req["group"] in w.groups]
            if req.get("mark"):
                windows = [w for w in windows if req["mark"] in w.marks]
            if req.get("workspace"):
                windows = [w for w in windows if w.workspace == req["workspace"]]
            return {
                "ok": True,
                "windows": [self._record_to_dict(w) for w in windows],
            }

        if cmd == "pool.get":
            rec = self.pool.get(req["id"])
            return {"ok": bool(rec), "window": self._record_to_dict(rec) if rec else None}

        if cmd == "pool.by_mark":
            return {
                "ok": True,
                "windows": [self._record_to_dict(w) for w in self.pool.by_mark(req["mark"])],
            }

        if cmd == "tag.add":
            rec = self.pool.get(req["id"])
            if not rec:
                return {"ok": False, "error": "unknown id"}
            tag = req["tag"]
            if tag not in rec.tags:
                rec.tags.append(tag)
            return {"ok": True}

        if cmd == "group.add":
            rec = self.pool.get(req["id"])
            if not rec:
                return {"ok": False, "error": "unknown id"}
            g = req["group"]
            if g not in rec.groups:
                rec.groups.append(g)
            return {"ok": True}

        if cmd == "rollup.toggle":
            from . import rollup_geom
            return rollup_geom.toggle(self.i3, self.pool, req.get("id"))

        return {"ok": False, "error": f"unknown cmd {cmd}"}

    @staticmethod
    def _record_to_dict(rec: WindowRecord | None) -> dict | None:
        if rec is None:
            return None
        from dataclasses import asdict
        return asdict(rec)

    def _serve_socket(self):
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self._server.listen(8)
        self._server.settimeout(1.0)
        log.info("command socket at %s", self.socket_path)
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
                t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except OSError:
                break

    # ------------------------------------------------------------------
    def run(self):
        self.pool.load_snapshot()
        self._seed_pool()

        self.i3.on(Event.WINDOW, self._on_window)

        sock_thread = threading.Thread(target=self._serve_socket, daemon=True)
        sock_thread.start()

        def _shutdown(*_):
            log.info("shutting down")
            self._stop.set()
            self.pool.save_snapshot()
            if self._server:
                self._server.close()
            if self.socket_path.exists():
                self.socket_path.unlink(missing_ok=True)
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        log.info("daemon running")
        self.i3.main()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    Daemon().run()


if __name__ == "__main__":
    main()
