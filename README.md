# i3-nice-layout

Window pool and helpers for i3 layout

## Goals

- **roll-up** for floating windows.
- classic staggered stack or "stairs/cascade" placement for new floating
  windows.
- other auto and initial layouts, and dragging / relative updates of window
  clusters.

  - "docking" for windows in clusters (lock and align at edges, into ranks)
  - other spreads, manual or parametric; force fields, border layouts, flow
      lines

- interface/layer with integrated or separate lib for building context windows, like
  the faux title UI; but to deploy "context" windows positioned/sized along the
  main user application windows with personalized/contextual data (and perhaps
  UI) TODO: move this to new, separate graphical UI project

- Stay out of the way: pure i3ipc, optional daemon, no heavy framework.

## Methods

- Maintain a live **pool** of interesting windows (id, marks, tags, geometry, floating state, workspace…).
- Provide a small command interface so one-shot scripts and other tools can query / mutate the pool.

## Project setup

- See TODO/DONE.txt for tasks, this is the current location for dev and plan.
- State is development, so see HACKING.md for current setup notes as well as
  more dev. And also refer to the example usage in config/.

- Direct binding: `config/i3-nice.example.conf`
- Mode-based (discoverable): `config/i3-nice-mode.example.conf`

## Architecture (short)

- **Daemon**
  Subscribes to i3 window events, keeps an in-memory pool + optional on-disk snapshot of geometries.

- **Pool** 
  Dict keyed by container id. Stores geometry history, marks, user tags, group membership, roll-up state.

- **Commands**  
  Thin CLI that talks to the daemon over a Unix socket; and falls back to direct i3ipc when the daemon is not running.

- **Roll-up** functions; one geometry based, and one with stand-in UI that hides real window on scratchpad.

- **Filtered list**  
  `i3-nice-layout pool list [--floating] [--rolled] [--tag …] [--group …] [--mark …] [--workspace …] [--plain]`
