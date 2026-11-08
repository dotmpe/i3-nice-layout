"""Stand-in roll-up: argv template, spawn/match, and unroll. No running i3."""

import subprocess
from types import SimpleNamespace

import pytest

from i3_nice_layout.cli import _rollup_payload
from i3_nice_layout.pool import Geometry, WindowPool, WindowRecord
from i3_nice_layout.rollup_altui import (
    MATCH_TIMEOUT_S,
    WM_CLASS,
    WM_INSTANCE,
    _drain_unroll,
    _inflight,
    _pending_unroll,
    _unroll,
    mark_for,
    net_wm_pid,
    note_close,
    render_argv,
    split_template,
    toggle,
    wants_altui,
)
from i3_nice_layout.rollup_geom import DEFAULT_ROLLED_HEIGHT

TEMPLATE = (
    "i3-nice-gui title --for {id} --title {name} --x {x} --y {y} "
    "--width {width} --height {height} --class {class} --instance {instance} "
    "--workspace {workspace}"
)


@pytest.fixture(autouse=True)
def _clear_roll_guards():
    yield
    _inflight.clear()
    _pending_unroll.clear()


class Rect:
    def __init__(self, x, y, width, height):
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class Node:
    def __init__(self, con_id, window, rect, name="", window_class=None,
                 window_instance=None, workspace=None, marks=None):
        self.id = con_id
        self.window = window
        self.rect = rect
        self.name = name
        self.window_class = window_class
        self.window_instance = window_instance
        self.marks = list(marks or [])
        self.floating = "user_on"
        self._workspace = workspace

    def workspace(self):
        if self._workspace is None:
            return None
        return SimpleNamespace(name=self._workspace)


class Tree:
    def __init__(self, nodes, focused=None):
        self.nodes = nodes
        self._focused = focused

    def descendants(self):
        return list(self.nodes)

    def find_by_id(self, con_id):
        for node in self.nodes:
            if node.id == con_id:
                return node
        return None

    def find_focused(self):
        return self._focused


class FakeI3:
    def __init__(self, tree, fail_substr=None):
        self.tree = tree
        self.commands = []
        self.fail_substr = fail_substr

    def get_tree(self):
        return self.tree

    def command(self, cmd):
        self.commands.append(cmd)
        failed = self.fail_substr and self.fail_substr in cmd
        return [SimpleNamespace(success=not failed, error="no" if failed else None)]


class Proc:
    def __init__(self, pid=4242, dead=False):
        self.pid = pid
        self.returncode = None
        self.dead = dead

    def poll(self):
        if self.dead:
            self.returncode = 1
            return 1
        return None


class World:
    def __init__(self):
        self.real = Node(
            7, 1000, Rect(10, 20, 200, 100),
            name="My Notes", window_class="URxvt", window_instance="urxvt", workspace="3",
        )
        self.old_title = Node(
            70, 7000, Rect(1, 1, 80, 28),
            name="other", window_class=WM_CLASS, window_instance=WM_INSTANCE,
        )
        self.stand = Node(
            50, 2000, Rect(10, 20, 200, 28),
            name="My Notes", window_class=WM_CLASS, window_instance=WM_INSTANCE, workspace="3",
        )
        self.tree = Tree([self.real, self.old_title], focused=self.real)
        self.i3 = FakeI3(self.tree)
        self.pool = WindowPool()
        self.rec = WindowRecord(
            id=7, window=1000, name="My Notes",
            window_class="URxvt", instance="urxvt", workspace="3",
            geometry=Geometry(10, 20, 200, 100),
        )
        self.pool.insert(self.rec)
        self.killed = []
        self.argv = None
        self.kwargs = None
        self.spawned = 0
        self.t = 0.0
        self.add_stand_in = True
        self.extra_nodes = []
        self.explode = None
        self.proc = Proc()
        self.pids = {2000: 4242, 7000: 111}
        self.lock = __import__("threading").Lock()

    def popen(self, argv, **kwargs):
        self.spawned += 1
        self.argv = list(argv)
        self.kwargs = kwargs
        if self.explode is not None:
            raise self.explode
        if self.add_stand_in:
            self.tree.nodes.append(self.stand)
        self.tree.nodes.extend(self.extra_nodes)
        return self.proc

    def pid_of(self, window):
        return self.pids.get(window)

    def ender(self, pid):
        self.killed.append(pid)

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def toggle(self, con_id=7, command=TEMPLATE, timeout=MATCH_TIMEOUT_S):
        return toggle(
            self.i3, self.pool, con_id, command=command, lock=self.lock,
            popen=self.popen, pid_of=self.pid_of, sleep=self.sleep, clock=self.clock,
            ender=self.ender, timeout=timeout,
        )


def test_render_keeps_spaces_and_does_not_reparse_values():
    argv = render_argv(
        split_template("cmd --title {name} --for {id}"),
        {"name": "My Notes; rm {x}", "id": "7"},
    )
    assert argv == ["cmd", "--title", "My Notes; rm {x}", "--for", "7"]


def test_render_rejects_unknown_placeholder():
    with pytest.raises(KeyError):
        render_argv(["{nope}"], {"name": "a"})


def test_roll_spawns_matches_pid_then_scratchpads():
    world = World()
    reply = world.toggle(con_id=None)
    assert reply["ok"] is True
    assert reply["action"] == "rolled"
    assert reply["stand_in_id"] == 50
    assert reply["pid"] == 4242
    assert reply["rolled_height"] == DEFAULT_ROLLED_HEIGHT
    assert world.kwargs["start_new_session"] is True
    assert world.kwargs.get("shell") is not True
    assert world.argv[world.argv.index("--title") + 1] == "My Notes"
    assert world.argv[world.argv.index("--height") + 1] == "28"
    assert world.argv[world.argv.index("--width") + 1] == "200"
    assert world.argv[world.argv.index("--class") + 1] == "URxvt"
    assert world.argv[world.argv.index("--workspace") + 1] == "3"
    place = world.i3.commands[0]
    assert "floating enable" in place
    assert "border none" in place
    assert "move absolute position 10 20" in place
    assert "resize set 200 28" in place
    assert f"mark --add {mark_for(7)}" in place
    assert world.i3.commands[1] == '[con_id="7"] move scratchpad'
    assert world.killed == []
    assert world.rec.rolled_up is True
    assert world.rec.stand_in_id == 50
    assert world.rec.extra["stand_in_pid"] == 4242
    assert world.pool.get(50).extra["stands_for"] == 7


def test_spawn_failure_does_not_hide_the_window():
    world = World()
    world.explode = FileNotFoundError("i3-nice-gui")
    reply = world.toggle()
    assert reply["ok"] is False
    assert "failed" in reply["error"]
    assert world.i3.commands == []
    assert world.rec.rolled_up is False
    assert world.killed == []


def test_timeout_kills_the_child_and_leaves_the_window():
    world = World()
    world.add_stand_in = False
    reply = world.toggle()
    assert reply["ok"] is False
    assert reply["error"] == "stand-in window did not appear"
    assert world.t >= MATCH_TIMEOUT_S
    assert world.killed == [4242]
    assert world.i3.commands == []
    assert world.rec.rolled_up is False


def test_dead_process_does_not_scratchpad():
    world = World()
    world.proc.dead = True
    world.add_stand_in = False
    reply = world.toggle()
    assert reply["ok"] is False
    assert "exited" in reply["error"]
    assert world.i3.commands == []
    assert world.killed == [4242]


def test_place_failure_does_not_scratchpad():
    world = World()
    world.i3.fail_substr = "floating enable"
    reply = world.toggle()
    assert reply["ok"] is False
    assert reply["error"] == "could not place the stand-in"
    assert all("scratchpad" not in cmd for cmd in world.i3.commands)
    assert world.killed == [4242]
    assert world.rec.rolled_up is False


def test_scratchpad_failure_rolls_the_link_back():
    world = World()
    world.i3.fail_substr = "move scratchpad"
    reply = world.toggle()
    assert reply["ok"] is False
    assert world.rec.rolled_up is False
    assert world.rec.stand_in_id is None
    assert "rollup_kind" not in world.rec.extra
    assert world.killed == [4242]


def test_class_backup_when_pid_is_unreadable():
    world = World()
    world.pids.clear()
    reply = world.toggle(timeout=0)
    assert reply["ok"] is True
    assert reply["stand_in_id"] == 50
    assert world.t == 0


def test_two_title_windows_without_a_pid_are_not_guessed():
    world = World()
    world.pids.clear()
    world.extra_nodes.append(Node(
        51, 2001, Rect(0, 0, 10, 10),
        window_class=WM_CLASS, window_instance=WM_INSTANCE,
    ))
    reply = world.toggle(timeout=0)
    assert reply["ok"] is False
    assert reply["error"] == "stand-in window match is ambiguous"
    assert all("scratchpad" not in cmd for cmd in world.i3.commands)
    assert world.rec.rolled_up is False


def test_missing_command_and_bad_template_do_not_spawn(monkeypatch):
    monkeypatch.delenv("I3_NICE_STAND_IN", raising=False)
    world = World()
    missing = world.toggle(command=None)
    assert missing["ok"] is False
    assert "I3_NICE_STAND_IN" in missing["error"]
    unknown = world.toggle(command="echo {nope}")
    assert unknown["ok"] is False
    assert "placeholder" in unknown["error"]
    quoted = world.toggle(command='echo "unterminated')
    assert quoted["ok"] is False
    assert world.spawned == 0
    assert world.rec.rolled_up is False


def test_request_template_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("I3_NICE_STAND_IN", "from-env {id}")
    world = World()
    reply = world.toggle(command=TEMPLATE)
    assert reply["ok"] is True
    assert world.argv[0] == "i3-nice-gui"


def test_environment_template_is_used_when_the_request_has_none(monkeypatch):
    monkeypatch.setenv("I3_NICE_STAND_IN", TEMPLATE)
    world = World()
    reply = world.toggle(command=None)
    assert reply["ok"] is True
    assert world.argv[0] == "i3-nice-gui"


def test_no_geometry_does_not_spawn():
    world = World()
    world.rec.geometry = None
    world.tree.nodes = [node for node in world.tree.nodes if node.id != 7]
    reply = world.toggle()
    assert reply["ok"] is False
    assert "geometry" in reply["error"]
    assert world.spawned == 0


def test_unroll_uses_stand_in_origin_and_cached_size():
    world = World()
    world.stand.rect = Rect(30, 40, 200, 28)
    world.tree.nodes.append(world.stand)
    world.rec.rolled_up = True
    world.rec.stand_in_id = 50
    world.rec.extra.update(rollup_kind="stand-in", stand_in_pid=99, rollup_mark=mark_for(7))
    world.pool.insert(WindowRecord(
        id=50, window=2000, name="My Notes",
        window_class=WM_CLASS, instance=WM_INSTANCE,
        extra={"stands_for": 7, "role": "stand-in"},
    ))
    reply = world.toggle(command=None)
    assert reply["action"] == "unrolled"
    show, move = world.i3.commands
    assert show == '[con_id="7"] scratchpad show'
    assert "move absolute position 30 40" in move
    assert "resize set 200 100" in move
    assert world.rec.geometry.x == 30 and world.rec.geometry.y == 40
    assert world.rec.geometry.height == 100
    assert world.rec.rolled_up is False
    assert world.rec.stand_in_id is None
    assert world.killed == [99]
    again = _unroll(world.i3, world.pool, world.rec, world.lock, ender=world.ender)
    assert again["action"] == "idle"
    assert world.killed == [99]
    assert len(world.i3.commands) == 2


def test_toggle_on_the_stand_in_unrolls_the_real_window():
    world = World()
    world.tree.nodes.append(world.stand)
    world.rec.rolled_up = True
    world.rec.stand_in_id = 50
    world.rec.extra.update(rollup_kind="stand-in", stand_in_pid=99)
    reply = world.toggle(con_id=50, command=None)
    assert reply["action"] == "unrolled"
    assert reply["id"] == 7
    assert world.spawned == 0
    assert world.rec.rolled_up is False


def test_geometry_roll_is_not_scratchpadded_by_stand_in():
    world = World()
    world.rec.rolled_up = True
    world.rec.extra["rolled_height"] = 28
    reply = world.toggle()
    assert reply["ok"] is False
    assert "geometry" in reply["error"]
    assert world.spawned == 0
    assert world.i3.commands == []


def test_stand_in_close_unrolls_and_real_close_signals():
    world = World()
    world.stand.rect = Rect(30, 40, 200, 28)
    world.tree.nodes.append(world.stand)
    world.rec.rolled_up = True
    world.rec.stand_in_id = 50
    world.rec.extra.update(rollup_kind="stand-in", stand_in_pid=99)
    follow = note_close(world.pool, world.stand)
    assert follow is not None
    assert follow.kind == "unroll"
    assert follow.owner_id == 7
    assert (follow.x, follow.y) == (30, 40)
    from i3_nice_layout.rollup_altui import after_close
    after_close(world.i3, world.pool, follow, world.lock, ender=world.ender)
    assert world.rec.rolled_up is False
    assert "move absolute position 30 40" in world.i3.commands[1]
    assert world.killed == [99]

    world.rec.rolled_up = True
    world.rec.stand_in_id = 50
    world.rec.extra.update(rollup_kind="stand-in", stand_in_pid=99)
    world.i3.commands.clear()
    world.killed.clear()
    killing = note_close(world.pool, world.real)
    assert killing is not None and killing.kind == "kill"
    after_close(world.i3, world.pool, killing, world.lock, ender=world.ender)
    assert world.i3.commands == []
    assert world.killed == [99]


def test_close_uses_the_mark_when_the_pool_link_is_gone():
    world = World()
    world.rec.rolled_up = True
    world.rec.stand_in_id = None
    world.rec.extra["rollup_kind"] = "stand-in"
    world.stand.marks = [mark_for(7)]
    follow = note_close(world.pool, world.stand)
    assert follow is not None
    assert follow.kind == "unroll"
    assert follow.owner_id == 7


def test_deferred_unroll_runs_when_the_roll_releases():
    world = World()
    world.tree.nodes.append(world.stand)
    world.rec.rolled_up = True
    world.rec.stand_in_id = 50
    world.rec.extra.update(rollup_kind="stand-in", stand_in_pid=99)
    from i3_nice_layout.rollup_altui import _claim, _defer_unroll, _unclaim
    assert _claim(7)
    _defer_unroll(7)
    _unclaim(7)
    _drain_unroll(7, world.i3, world.pool, world.lock, world.ender)
    assert world.rec.rolled_up is False
    assert world.killed == [99]
    assert any("scratchpad show" in cmd for cmd in world.i3.commands)


def test_wants_altui_only_for_the_stand_in_variant():
    world = World()
    assert wants_altui(world.pool, world.i3, 7, False) is False
    assert wants_altui(world.pool, world.i3, None, True) is True
    world.rec.rolled_up = True
    world.rec.extra["rollup_kind"] = "stand-in"
    assert wants_altui(world.pool, world.i3, 7, False) is True


def test_no_focused_window():
    world = World()
    world.tree._focused = Node(1, None, Rect(0, 0, 1, 1))
    reply = world.toggle(con_id=None)
    assert reply == {"ok": False, "error": "no focused window"}
    assert world.spawned == 0


def test_net_wm_pid_parses_xprop(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv[:2] == ["xprop", "-id"]
        return SimpleNamespace(stdout="_NET_WM_PID(CARDINAL) = 42\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert net_wm_pid(255) == 42

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("xprop")

    monkeypatch.setattr(subprocess, "run", missing)
    assert net_wm_pid(255) is None


def test_rollup_payload_forwards_env_only_for_stand_in(monkeypatch):
    monkeypatch.setenv("I3_NICE_STAND_IN", "i3-nice-gui title --for {id}")
    stand_in = SimpleNamespace(id=4, stand_in=True, stand_in_cmd=None)
    payload = _rollup_payload(stand_in)
    assert payload["stand_in"] is True
    assert payload["stand_in_cmd"] == "i3-nice-gui title --for {id}"

    plain = SimpleNamespace(id=None, stand_in=False, stand_in_cmd=None)
    payload = _rollup_payload(plain)
    assert payload["stand_in"] is None
    assert payload["stand_in_cmd"] is None

    override = SimpleNamespace(id=1, stand_in=False, stand_in_cmd="echo {id}")
    payload = _rollup_payload(override)
    assert payload["stand_in"] is True
    assert payload["stand_in_cmd"] == "echo {id}"
