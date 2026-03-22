# OpenDCC Web — Handoff for Next Chat

Date: 2026-03-22
Branch: `develop`

## Latest commits (newest first)
- `16edbf2` perf(stage): speed Kitchen_set reload via used-layer preload and synthetic root
- `13c8d28` fix(material): resolve computed bindings for preview/full purposes
- `35af392` feat(material): add unbind, purpose/strength binding, and save-edits-as export
- `e0a2e2c` perf(material): avoid full viewport reload on shader parameter changes
- `05a7738` feat(usd-layering): author pxr edits in sidecar layer and scope material preview
- `cf150e8` feat(material): add create-bind workflow and texture slot connections

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
  - explicit unbind (current purpose or all purposes)
  - bind options: purpose (`allPurpose|preview|full`) and strength (`weaker/strongerThanDescendants`)
  - shader ID detection flags for MaterialX / vendor networks (read-only for now)
  - immediate viewport preview updates on numeric/color edits
  - preview scope safety: no global fallback tinting (prevents whole-stage accidental color preview)
  - low-latency updates: material edits emit `material_changed` (no full viewport stage rebuild)
- pxr mode non-destructive authoring layer:
  - on stage open, edits target a sidecar layer `*.opendcc_edits.usda`
  - root imported stage remains untouched
  - `/api/stage/save` saves edit layer (not root) in this mode
  - `Save Edits As…` UI + `/api/stage/save_edits_as` for clean overrides export
  - WASM reload speed-up: `/api/stage/assets` now uses `GetUsedLayers()` and frontend builds a synthetic root in edit-layer mode (avoids expensive flatten reloads)
- Console hygiene:
  - filters repetitive HdEmscripten warning `Unsupported interpolation type 'uniform' for primvar __faceindex`
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
  - `/api/material/bind` (create/bind material with purpose/strength)
  - `/api/material/unbind` (remove direct binding opinions)
  - `/api/material/{matPath}/set` (set preview parameter)
  - `/api/material/{matPath}/texture` (connect/clear texture slot)
- Layer/export ops:
  - `/api/stage/save_edits_as` (export current edits layer)
- Keyframe ops:
  - `/api/prim/{path}/keyframe/set`
  - `/api/prim/{path}/keyframe/delete`
  - `/api/prim/{path}/keyframe/move`
  - `/api/prim/{path}/keyframe/interpolation`
