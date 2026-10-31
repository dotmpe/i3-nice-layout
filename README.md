# i3-nice-layout

Window pool and helpers for i3 layout.

## Goals

These basic, but classic layout and window UI behaviors:

- **cascade** or staggered stack or "stairs" placement for new floating
  windows [TODO-6]
- **roll-up** for floating windows [DONE-1, DONE-3, testing]

More modern X11/i3 conveniences to keep floating windows under control,
and out of the way.

- **follow**: dragging / relative updates of windows, as in visual clusters [TODO-5, testing]

  - "docking" for windows in clusters (lock and align at edges, ie. *floating ranks*)
  - other spreads, manual or parametric; (or may be in overflow project:) force fields, border layouts, other std packing algos

- Stay out **of the way**: *pure i3ipc, optional daemon*.
  No heavy framework, and no graphical dependencies; 
  [TODO-6: for a support GUI have a look at [dotmpe/i3-nice-gui](https://github.com/dotmpe/i3-nice-gui)]

## Methods

- Maintain a live **pool** of interesting windows (track id, marks, tags, geometry, floating state, workspace…).
- Provide a small command interface so one-shot scripts and other tools can query / mutate the pool.

## Project setup

- README is the main project overview.
- See TODO/DONE.txt for tasks, this is the current location for dev and plan.
- State is development, see [HACKING](HACKING.md) for install and other setup notes.

  Instructions are at [CONVENTIONS+baseline.md](doc/CONVENTIONS+baseline.md).

Example i3 config:

- a direct binding: `config/i3-nice.example.conf`
- a mode-based one: `config/i3-nice-mode.example.conf`

Invoke ``i3-nice-layout daemon`` to spawn a helper tracking the window pool.

## Architecture (short)

- **Daemon**

  Subscribes to i3 window events, keeps an in-memory pool + optional on-disk snapshot of geometries.

- **Pool**

  Dict keyed by container id. Stores geometry history, marks, user tags, group membership, roll-up state.

- **Commands**

  Thin CLI that talks to the daemon over a Unix socket; and falls back to direct i3ipc when the daemon is not running.

----

Created with Grok
