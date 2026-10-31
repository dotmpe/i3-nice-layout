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

Use the program as client to run commands:
```bash
$ i3-nice-layout pool list
```

See/copy i3 config from [config/](config/) snippets.

Trigger and undo roll-ups:
```
$ i3-nice-layout rollup toggle
```

Follow groups/group-leader of group 'notes' on call (copy relative position
changes):
```
$ i3-nice-layout group <id> notes
$ i3-nice-layout follow
```

## Tooling

- `xdotool selectwindow` returns the X window Id; the i3 Id can be picked from `get_tree` JSON.

These are some example Bash functions:

```bash
i3wm_window_id() {
    i3wm_window_info "$@" | jq '.[0]'
}
i3wm_window_info() {
: "${1:?X window Id required}"
    < <(i3-msg -t get_tree) \
    jq -r '
        .. |
        select(.window? == '"${1}"') |
        [ .id, .layout, .border, .marks, .rect, .geometry, .output ]'
}
i3wm_window_select_info() {
    i3wm_window_info "$(xdotool selectwindow)"
}
```

Such script can be used/adapted for session helpers or keybinding handlers, ie. to mark or group windows by point-and-click.
