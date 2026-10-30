"""Tiny helper for other Python code (your packing library) to talk to the pool."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

DEFAULT_SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "i3-nice-layout.sock"


def call(cmd: str, **kwargs) -> dict[str, Any]:
    req = {"cmd": cmd, **kwargs}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(DEFAULT_SOCKET))
        s.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
    return json.loads(data.decode().strip())


def list_windows() -> list[dict]:
    r = call("pool.list")
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "unknown error"))
    return r["windows"]


def get_window(con_id: int) -> dict | None:
    r = call("pool.get", id=con_id)
    return r.get("window")
