## Quick start (development)

```bash
cd ~/project/i3-nice-layout
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the daemon in a terminal for looking at the log ouput directly and manual
restart after updates:
```bash
$ i3-nice-layout daemon &
```

A second daemon exits when that socket is already accepting connections. A leftover socket file from a crash is removed.

Use the client to access commands for layout, to access the pool data:
```bash
$ i3-nice-layout pool list
```

See [config/](config/) for example files and snippets. The section on tooling
below has parts on using the layout commands, as well the i3 session setup and
other tools.

## Tooling

### Session startup and state
The interactive UI in i3wm is limited to window geometry manipulation and a
visual bar component. You bring your own apps and tools, and those can be
launched from keyboard shurtcuts (``bindsym``) and through i3 `exec`.

The entire state is serialized by `i3-msg -t get_tree`, into a JSON of all the
nested container nodes. The script `i3-save-tree` is the official Perl tool to
retrieve tree segments per output or workspace (and turn those into commented
JSON with example swallow rules) to use with the `append_layout` command.

None of that works as a launcher and it does not start programs but it prepares
the containers for when the programs start. Until then placeholder windows
stand-in for visual surfaces, and i3 completely tracks them as container nodes
in the tree (with layout, and marks, but also with the swallows list that has
the rules for the class/instance/title/window_role/machine matching).

i3-nice-layout is also not a launcher. Also its command work on i3-nice-layout
groups, and not on i3 marks, but i3 bindings do. So if you have enough
bindings, you can do this:

```i3config
for_window [<class/...=...>] mark role:stats-float, floating enable
binsym <keycombo> [con_mark="role:stats-float"] exec i3-nice-layout <rollup or follow>
```

Or, you can script the for_window (add `exec` to i3-nice-layout or a command
shell script) to do the group for the window and start a layout.

### Mouse configuration

`bindsym [--release] button<N>`

1. Left mouse
2. Middle mouse
3. Right mouse

4-7 scroll wheel actions

8-9 side thumb actions

Click or release bindings work on the i3 title bar, other select chrome or
the entire window.

To bind for a `i3bar` surface including its buttons, use the binding commands inside a `bar { .. }` block.
This may override normal button1 behavior and not trigger the normal workspace switch.

In addition to the bars, a border and whole window can be bound (`--whole-window` or `--border`, exclusive).
Both still include the titlebar as well normally unless `--exclude-titlebar` is given.

This too may override normal behaviors.
[XXX: and --release is not doing anything for me at either of two hosts/versions]

### Rollup windows

[TODO-7, DONE-6]
Trigger and undo roll-ups. With no flag this shrinks the window.
`--stand-in` hides it on the scratchpad and leaves a title window:
```
$ i3-nice-layout rollup toggle
$ i3-nice-layout rollup toggle --stand-in
```

The stand-in command is `I3_NICE_STAND_IN`. The client sends its value
with the toggle; when that is unset, the daemon uses its own. i3's
`set $name` does not put the variable in either environment. A binding
picks the new value up after i3 is reloaded and the daemon is restarted.
From a shell, exporting it before the client command is enough.

```
export I3_NICE_STAND_IN='i3-nice-gui title --for {id} --title {name} --x {x} --y {y} --width {width} --height {height} --on-click "i3-nice-layout rollup toggle --id {id}"'
```

Placeholders: `{id}` `{name}` `{x}` `{y}` `{width}` `{height}` `{class}`
`{instance}` `{workspace}`. `{height}` is the 28px strip. The daemon matches the
new window by `_NET_WM_PID` (`xprop`), and uses `WM_CLASS` `i3-nice-gui` /
`title` when that property cannot be read. It waits up to 2s, and leaves the
original window alone when no stand-in appears. Closing the stand-in restores
the real window at the stand-in's x, y and the cached size. i3 will not map a
floating window shorter than `floating_minimum_size` (default 75 x 50).

### Follow other windows

[TODO-5]
Follow groups/group-leader of group 'notes' on call (copy relative position
changes) or with watch loop:
```
$ i3-nice-layout group [--id=...] notes
$ i3-nice-layout follow [--watch]
```

### Interactive commands

- `xdotool selectwindow` returns an X window Id that can mapped to container Id with i3 `get_tree` JSON.

  The file config/i3-nice-util.sh has a small helper left for manual testing.
