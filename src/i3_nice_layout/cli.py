"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

DEFAULT_SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "i3-nice-layout.sock"


def _send(cmd: str, **kwargs) -> dict:
    req = {"cmd": cmd, **kwargs}
    try:
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
    except (FileNotFoundError, ConnectionRefusedError) as e:
        return {"ok": False, "error": f"daemon not running ({e})"}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="i3-nice-layout")
    sub = parser.add_subparsers(dest="command", required=True)

    # daemon
    p_daemon = sub.add_parser("daemon", help="run the background pool daemon")
    p_daemon.set_defaults(func=lambda _: _run_daemon())

    # pool
    p_pool = sub.add_parser("pool", help="inspect the window pool")
    pool_sub = p_pool.add_subparsers(dest="pool_cmd", required=True)
    pool_sub.add_parser("list").set_defaults(
        func=lambda _: print(json.dumps(_send("pool.list"), indent=2))
    )
    p_get = pool_sub.add_parser("get")
    p_get.add_argument("id", type=int)
    p_get.set_defaults(func=lambda a: print(json.dumps(_send("pool.get", id=a.id), indent=2)))

    # tag / group (minimal)
    p_tag = sub.add_parser("tag")
    p_tag.add_argument("id", type=int)
    p_tag.add_argument("tag")
    p_tag.set_defaults(func=lambda a: print(json.dumps(_send("tag.add", id=a.id, tag=a.tag))))

    p_group = sub.add_parser("group")
    p_group.add_argument("id", type=int)
    p_group.add_argument("group")
    p_group.set_defaults(
        func=lambda a: print(json.dumps(_send("group.add", id=a.id, group=a.group)))
    )

    # rollup
    p_roll = sub.add_parser("rollup")
    roll_sub = p_roll.add_subparsers(dest="roll_cmd", required=True)
    p_toggle = roll_sub.add_parser("toggle")
    p_toggle.add_argument("--id", type=int, default=None)
    p_toggle.set_defaults(
        func=lambda a: print(json.dumps(_send("rollup.toggle", id=a.id), indent=2))
    )

    args = parser.parse_args(argv)
    args.func(args)


def _run_daemon():
    from .daemon import main as daemon_main
    daemon_main()


if __name__ == "__main__":
    main()
