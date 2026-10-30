# i3-nice-layout

Window pool and helpers for i3 layout

## Goals

- **roll-up** for i3 windows.

    XXX: either app windows are hidden on the scratchpad, and a stand-in UI plays for faux title bar.
    or just resize...

- different types of other parameterized **layouts** and interaction modes; 
  for visual clustering, or separation (of floating windows):

    - initial posititioning for new windows in separate, cascaded stack groups
    - dragging / relative updates of windows in clustered groups
    - "docking" of windows in clusters (lock and align at edgjes, into ranks)
    - other spreads, manual or parametric; force fields, border layouts, flow
      lines

- interface with integrated or separate lib for building context windows, like
  the faux title UI; but to deploy "context" windows positioned/sized along the
  main user application windows with personalized/contextual data (and perhaps
  UI)

## Methods

- Maintain a live **pool** of interesting windows (id, marks, tags, geometry, floating state, workspace…).
- Provide a small command interface so one-shot scripts and your other tools can query / mutate the pool.
- Stay out of the way: pure i3ipc, optional daemon, no heavy framework.

## Architecture (short)

- **Daemon**
  Subscribes to i3 window events, keeps an in-memory pool + optional on-disk snapshot of geometries.

- **Pool**  
  Dict keyed by container id. Stores geometry history, marks, user tags, group membership, roll-up state.

- **Commands**  
  Thin CLI that talks to the daemon (Unix socket) or falls back to direct i3ipc when the daemon is not running.

- **Roll-up**  
  Hide real window → scratchpad, spawn/manage a tiny stand-in titlebar that can restore it.
