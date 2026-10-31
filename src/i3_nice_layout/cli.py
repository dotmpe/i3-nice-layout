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
    # drop keys whose value is None so the daemon does not see them
    payload = {k: v for k, v in kwargs.items() if v is not None}
    req = {"cmd": cmd, **payload}
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


def _print_pool(reply: dict, as_json: bool = True) -> None:
    if not reply.get("ok"):
        print(json.dumps(reply, indent=2), file=sys.stderr)
        sys.exit(1)
    windows = reply.get("windows", [])
    if as_json:
        print(json.dumps(reply, indent=2))
        return
    # compact human view
    for w in windows:
        geo = w.get("geometry") or {}
        flags = []
        if w.get("floating") != "off":
            flags.append("F")
        if w.get("rolled_up"):
            flags.append("R")
        flag_s = "".join(flags) or "-"
        print(
            f"{w['id']:6}  {flag_s:3}  "
            f"{geo.get('width', '?'):4}x{geo.get('height', '?'):<4}  "
            f"{(w.get('name') or '')[:60]}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="i3-nice-layout")
    sub = parser.add_subparsers(dest="command", required=True)

    # daemon
    p_daemon = sub.add_parser("daemon", help="run the background pool daemon")
    p_daemon.set_defaults(func=lambda _: _run_daemon())

    # pool
    p_pool = sub.add_parser("pool", help="inspect the window pool")
    pool_sub = p_pool.add_subparsers(dest="pool_cmd", required=True)

    p_list = pool_sub.add_parser("list", help="list windows (optionally filtered)")
    p_list.add_argument("--floating", action="store_true", help="only floating windows")
    p_list.add_argument("--rolled", action="store_true", help="only rolled-up windows")
    p_list.add_argument("--tag", help="filter by user tag")
    p_list.add_argument("--group", help="filter by group name")
    p_list.add_argument("--mark", help="filter by i3 mark")
    p_list.add_argument("--workspace", help="filter by workspace name")
    p_list.add_argument("--json", action="store_true", default=True, help="JSON output (default)")
    p_list.add_argument("--plain", action="store_true", help="compact human-readable lines")
    p_list.set_defaults(func=lambda a: _print_pool(
        _send(
            "pool.list",
            floating=a.floating or None,
            rolled=a.rolled or None,
            tag=a.tag,
            group=a.group,
            mark=a.mark,
            workspace=a.workspace,
        ),
        as_json=not a.plain,
    ))

    p_get = pool_sub.add_parser("get")
    p_get.add_argument("id", type=int)
    p_get.set_defaults(func=lambda a: print(json.dumps(_send("pool.get", id=a.id), indent=2)))

    # tag / group
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

    # Shift every other window in a group by the delta of one window.
    # One-shot (after a drag) is the default. --watch polls during the drag.
    p_follow = sub.add_parser(
        "follow",
        help="move the rest of a group when one window is repositioned",
    )
    p_follow.add_argument("--id", type=int, help="window that moved (default: focused)")
    p_follow.add_argument("--group", help="group name (default: the window's only group)")
    p_follow.add_argument(
        "--anchor",
        action="store_true",
        help="only this window pulls the group",
    )
    p_follow.add_argument(
        "--clear-anchor",
        action="store_true",
        help="any member may pull the group again",
    )
    p_follow.add_argument(
        "--watch",
        action="store_true",
        help="poll and apply while a window is being dragged",
    )
    p_follow.add_argument(
        "--unwatch",
        action="store_true",
        help="stop polling (one --group, or all groups)",
    )
    p_follow.add_argument(
        "--interval",
        type=int,
        help="poll period in ms for --watch (default 50, minimum 10)",
    )
    p_follow.add_argument(
        "--status",
        action="store_true",
        help="print watch state and the last poll timings",
    )
    p_follow.set_defaults(func=_cmd_follow)

    # rollup
    p_roll = sub.add_parser("rollup")
    roll_sub = p_roll.add_subparsers(dest="roll_cmd", required=True)
    p_toggle = roll_sub.add_parser("toggle", help="shrink ↔ restore geometry of focused window")
    p_toggle.add_argument("--id", type=int, default=None, help="container id (default: focused)")
    p_toggle.set_defaults(
        func=lambda a: print(json.dumps(_send("rollup.toggle", id=a.id), indent=2))
    )

    args = parser.parse_args(argv)
    args.func(args)


def _cmd_follow(a) -> None:
    status_extra = a.anchor or a.clear_anchor or a.watch or a.unwatch or a.group or a.id is not None
    if a.status and status_extra:
        print(json.dumps({"ok": False, "error": "--status does not take other arguments"}))
        sys.exit(1)
    if a.unwatch and (a.watch or a.anchor or a.clear_anchor):
        print(json.dumps({"ok": False, "error": "--unwatch only stops watching"}))
        sys.exit(1)
    if a.anchor and a.clear_anchor:
        print(json.dumps({"ok": False, "error": "--anchor and --clear-anchor conflict"}))
        sys.exit(1)
    if a.interval is not None and not a.watch:
        print(json.dumps({"ok": False, "error": "--interval is only used with --watch"}))
        sys.exit(1)
    payload = {
        "id": a.id,
        "group": a.group,
        "anchor": True if a.anchor else None,
        "clear_anchor": True if a.clear_anchor else None,
        "watch": True if a.watch else None,
        "unwatch": True if a.unwatch else None,
        "status": True if a.status else None,
        "interval_ms": a.interval,
    }
    print(json.dumps(_send("group.follow", **payload), indent=2))


def _run_daemon():
    from .daemon import main as daemon_main
    daemon_main()


if __name__ == "__main__":
    main()
