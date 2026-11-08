"""Command socket claim. No running i3."""

import socket

import pytest

from i3_nice_layout.daemon import DaemonAlreadyRunning, acquire_command_socket


def _connect(path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.5)
    client.connect(str(path))
    client.close()


def test_live_socket_is_left_in_place(tmp_path):
    path = tmp_path / "i3-nice-layout.sock"
    holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    holder.bind(str(path))
    holder.listen(8)
    ino = path.stat().st_ino
    try:
        with pytest.raises(DaemonAlreadyRunning):
            acquire_command_socket(path)
        assert path.stat().st_ino == ino
        _connect(path)
    finally:
        holder.close()


def test_stale_socket_is_replaced(tmp_path):
    path = tmp_path / "i3-nice-layout.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()

    sock = acquire_command_socket(path)
    try:
        _connect(path)
    finally:
        sock.close()
        path.unlink(missing_ok=True)


def test_missing_path_binds(tmp_path):
    path = tmp_path / "i3-nice-layout.sock"
    sock = acquire_command_socket(path)
    try:
        assert path.exists()
        _connect(path)
    finally:
        sock.close()
        path.unlink(missing_ok=True)
