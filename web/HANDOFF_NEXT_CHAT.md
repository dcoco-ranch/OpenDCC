# OpenDCC Web — Handoff for Next Chat

Date: 2026-03-22
Branch: `develop`

## Latest commits (newest first)
- `56f3a32` fix(web-ui): prevent grid/statusbar overflow in panel layout
- `eae05f5` refactor(web-ui): separate curve editor panel and declutter properties
- `2f377fd` feat(web): add curve editor preview for time-sampled attributes
- `69ad126` feat(web): sync timeline with server time-sampled attribute queries
- `1dbecdd` feat(web): add timeline stepping controls and FPS input
- `09563b5` feat(web): improve timeline range from stage start/end TimeCode

## Major delivered features
- Timeline reads stage start/end/fps metadata.
- Play/pause + step + fps input.
- Time-sampled server queries synced with timeline.
- Curve Editor in separate panel (not in Properties).
- Basic keyframe editing API + UI:
  - add key @ current
  - delete selected key
  - move selected key -> current
- Interpolation mode API + UI:
  - linear / step / bezier mode persisted in `customData['opendcc:interp']`
  - server-side `step` evaluation for attribute query at time.

## Current UI organization
- Left: Outliner
- Center: Viewport
- Right: Properties
- Bottom (toggle): Curve Editor panel
- Status bar timeline at bottom

## Pending work (high priority)
1. Resizable panes (width/height) + persistent sizes
   - left/right width drag handles
   - curve panel height drag handle
2. Viewport panel presets (1/2/3/4) for 3D view area
3. Roadmap cleanup (Phase 3/4 items are stale and partially already done)
4. Production hardening:
   - tests for new keyframe/interp endpoints
   - UX polish for curve editor interactions.

## VM deployment notes
- VM: `ssh -p 51804 ranch@remote.ranchcomputing.com`
- Repo: `/home/ranch/devs/aidd/ShapeFX/OpenDCC`
- GPU container: `opendcc-rocky9-gpu`
- Hot update:
  - `docker cp web/static/index.html opendcc-rocky9-gpu:/opt/opendcc/web/static/index.html`
  - `docker cp web/server.py opendcc-rocky9-gpu:/opt/opendcc/web/server.py`
  - restart only when server.py changed.

## Quick validation endpoints
- Health: `http://localhost:8081/health`
- Stage info: `/api/stage/info`
- Prim at time: `/api/prim/{path}?time=...`
- Samples: `/api/prim_samples/{path}`
- Keyframe ops:
  - `/api/prim/{path}/keyframe/set`
  - `/api/prim/{path}/keyframe/delete`
  - `/api/prim/{path}/keyframe/move`
  - `/api/prim/{path}/keyframe/interpolation`
