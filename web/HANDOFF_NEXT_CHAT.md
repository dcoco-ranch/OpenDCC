# OpenDCC Web — Handoff for Next Chat

Date: 2026-03-22
Branch: `develop`

## Latest commits (newest first)
- `8f7e8bc` feat(material-editor): add dedicated inspector tab with UsdPreviewSurface controls
- `b455984` docs(handoff): refresh latest commit list after script editor upgrade
- `6d4f953` feat(script-editor): add CodeMirror python highlighting and autocomplete
- `320d28f` feat(viewport): add 1/2/3/4 panel presets with multi-view rendering
- `9b78f46` feat(web-ui): add resizable panel splitters and layout persistence
- `859fc72` feat(curve): add interpolation modes (linear/step/bezier)

## Major delivered features
- Timeline reads stage start/end/fps metadata.
- Play/pause + step + fps input.
- Time-sampled server queries synced with timeline.
- Curve Editor in separate panel (not in Properties).
- Resizable layout splitters + persisted panel sizes.
- Viewport panel presets: 1V / 2V / 3V / 4V (multi-camera scissor render).
- Script Editor upgraded with CodeMirror Python highlighting + basic autocomplete + fallback textarea mode.
- Material Editor tab in inspector (dedicated panel) with:
  - bound material (`UsdShade`) inspection
  - UsdPreviewSurface param editing (color/roughness/metallic/opacity/etc.)
  - texture slot entry (connect/disconnect `UsdUVTexture` per slot)
  - one-click create+bind material for prims with no material
  - shader ID detection flags for MaterialX / vendor networks (read-only for now)
  - immediate viewport preview updates on numeric/color edits
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
1. Node Editor web MVP (Phase 4.3)
   - graph canvas and minimal UsdShade graph visualization
2. UV Editor web MVP (Phase 4.2)
   - basic UV display and transform tools
3. MaterialX / vendor material workflows
   - dedicated editor(s) beyond UsdPreviewSurface
   - conversion/bridging strategy where possible
4. Viewport panel presets (1/2/3/4) interaction polish
   - render/picking works in all panels
   - orbit/transform control still driven by main perspective camera
5. Production hardening:
   - tests for keyframe/interp/material endpoints
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
- Material ops:
  - `/api/material/{primPath}` (inspect binding + shader ids + preview params + texture connections)
  - `/api/material/bind` (create/bind material)
  - `/api/material/{matPath}/set` (set preview parameter)
  - `/api/material/{matPath}/texture` (connect/clear texture slot)
- Keyframe ops:
  - `/api/prim/{path}/keyframe/set`
  - `/api/prim/{path}/keyframe/delete`
  - `/api/prim/{path}/keyframe/move`
  - `/api/prim/{path}/keyframe/interpolation`
