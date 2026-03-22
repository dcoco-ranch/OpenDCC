# OpenDCC — Roadmap : Migration Qt → Web & GPU Docker

> Chaque phase se termine par un **tag Git** validé (format `milestone/vX.Y`).
> Un tag n'est posé qu'après validation fonctionnelle complète par l'équipe.

---

## Résumé de l'existant (Milestone 0 — ✅ acquis)

| Composant | État |
|---|---|
| Backend FastAPI (`web/server.py`) | ✅ Opérationnel (headless, pxr, stub) |
| USD WASM + Hydra → Three.js viewport | ✅ Fonctionne (needle-tools/usd-viewer) |
| Outliner / Properties / Script Editor | ✅ Basiques, REST + WebSocket |
| Export USDA/GLB (server-side) | ✅ `usd_to_gltf.py` + `Flatten()` |
| Docker Rocky 9 (full VFX build) | ✅ `Dockerfile.rocky9` + `rocky9-web` |
| Docker rocky9-gpu (nvidia) | ✅ Dockerfile + entrypoint + GPU detection |
| 34 packages C++/Qt (plugins OpenDCC) | ✅ Code source, mais **aucun exposé côté web** |

---

## Phase 1 — Infrastructure GPU & Docker Hardening ✅
**Tag : `milestone/v1.0-gpu-infra`** — Validé

- ✅ `scripts/setup_nvidia_docker.sh` : setup NVIDIA Container Toolkit
- ✅ `docker/Dockerfile.rocky9.gpu` : image GPU (EGL + entrypoint)
- ✅ `docker/gpu-entrypoint.sh` : GPU auto-detect → `/tmp/gpu-info.json`
- ✅ `/health` enrichi avec infos GPU (nom, VRAM, driver, render_mode)
- ✅ `web/render_snapshot.py` + `/api/render/snapshot` + `/api/render/status`
- ✅ `scripts/test_docker.sh` : smoke tests CI
- ✅ `GPU_DOCKER_SETUP.md`
- ⚠️ Bouton Snapshot : visibilité Firefox macOS non résolue (accès direct URL OK)

---

## Phase 2 — Outliner & Scene Graph Web avancé — ✅ VALIDÉ
**Tag : `milestone/v2.0-scene-graph`**

### 2.1 — Outliner enrichi (remplace `opendcc.hydra_op.ui.scene_graph`)
- [x] Drag & drop pour reparenting (appel `/api/prims/parent`)
- [x] Menu contextuel (clic droit) : Create, Delete, Duplicate, Group, Rename, Visibility
- [x] Icônes typées par prim type (Mesh, Xform, Camera, Light, Material…)
- [x] Recherche / filtre de prims en temps réel
- [x] Multi-sélection (Ctrl+click, Shift+click)
- [x] Indicateur visuel actif/inactif, visible/invisible

### 2.2 — Properties Panel avancé
- [x] Édition inline de tous les types USD (Vec3, Color, Matrix, float, int, bool, token, string)
- [x] Widgets adaptés : color picker pour `diffuseColor`, slider pour `roughness`, etc.
- [x] Section pliable par catégorie (Xform, Geometry, Material, Custom)
- [ ] Affichage des relations (material binding, references) — déféré Phase 4

### 2.3 — Create Menu (remplace `opendcc.usd_editor.common_cmds`)
- [x] Toolbar "Create" : tous les types de prims (Mesh primitives, Lights, Camera, Xform, Scope)
- [x] Create/Delete opèrent sur le pxr stage (pas seulement stub)
- [x] Undo/Redo boutons + raccourcis clavier (Ctrl+Z, Ctrl+Shift+Z, Delete, Ctrl+D, Ctrl+G, H, F2)
- [ ] API endpoint `/api/prims/create` étendu pour supporter les paramètres initiaux — déféré

### 2.4 — GPU Snapshot (bonus — résolu pendant Phase 2)
- [x] EGL headless (NVIDIA platform device, GL 4.6 Compatibility profile)
- [x] Subprocess isolé (`_render_worker.py`) — crash-safe
- [x] Snapshot matches viewport camera (eye/target/fov from Three.js)
- [x] Z-up / Y-up auto-detection
- [x] Auto dome light (intensity 0.35) quand scène sans lumières
- [x] `/health` → `snapshot_available` basé sur vrai test render subprocess
- [x] RTX 5090 / NVIDIA 595.45 / USD 24.11 validé

---

## Phase 3 — Viewport & Interaction Web
**Tag : `milestone/v3.0-viewport`**

### 3.1 — Viewport interaction avancée
- [ ] Click-to-select dans Three.js (raycasting → `primPath` via `userData.primPath` du GLB)
- [ ] Highlight / outline du prim sélectionné
- [ ] Gizmo Transform (Translate/Rotate/Scale) via Three.js TransformControls → écriture `xformOp`
- [ ] Raccourcis clavier : W (translate), E (rotate), R (scale), F (frame), Delete

### 3.2 — Intégration WASM USD native
- [ ] Évaluer `pxr.Usd` en WASM (via needle-tools) pour ops côté client sans roundtrip serveur
- [ ] Stage diff minimal via WebSocket : envoyer uniquement les deltas (pas full reload)
- [ ] Lazy loading de la hiérarchie (traversée incrémentale `/api/prims?path=...` déjà en place)
- [x] Auteur non-destructif en pxr mode: edit target vers layer d'overrides sidecar (`*.opendcc_edits.usda`)
- [x] Export d'overrides via bouton/API `Save Edits As…`

### 3.3 — Modes de rendu
- [ ] Toggle wireframe / solid / shaded
- [ ] Toggle grid, axes helper
- [ ] Boutons de vue : Front, Back, Left, Right, Top, Bottom, Perspective
- [x] Presets de panneaux viewport 3D : 1V / 2V / 3V / 4V
- [ ] Stats overlay configurable (tris, draw calls, fps)

---

## Phase 4 — Outils DCC dans le Web
**Tag : `milestone/v4.0-dcc-tools`**

### 4.1 — Material Editor web (remplace `opendcc.usd_editor.material_editor`)
- [x] Lecture des `UsdShade` bindings et affichage en panneau dédié
- [x] Édition des paramètres `UsdPreviewSurface` (color, metallic, roughness, opacity)
- [x] Entrée des textures (slots principaux) + connexion/déconnexion `UsdUVTexture`
- [x] Création + binding rapide d’un matériau sur prim sans matériau
- [x] Unbind explicite sur prim + options de binding purpose/strength
- [x] Preview matériau temps réel dans le viewport

### 4.2 — UV Editor web (remplace `opendcc.usd_editor.uv_editor`)
- [ ] Canvas 2D (Three.js ou Canvas API) affichant les UVs
- [ ] Sélection de faces/vertices UV
- [ ] Transformations UV basiques (translate, rotate, scale)

### 4.3 — Node Editor web (remplace `opendcc.ui.node_editor` + `opendcc.usd_editor.usd_node_editor`)
- [ ] Framework de node graph en canvas (librairie : rete.js, litegraph.js, ou custom)
- [ ] Représentation des `UsdShade` networks (Shader → Material → Binding)
- [ ] Connection drag & drop entre nodes

### 4.4 — Script Editor amélioré (remplace `opendcc.ui.script_editor`)
- [x] Syntax highlighting Python (CodeMirror, fallback textarea si CDN indisponible)
- [x] Autocomplétion basique (`stage.`, `prim.`, `UsdGeom.`)
- [x] Historique des commandes
- [x] Accès aux globals `app`, `session`, `stage`

---

## Phase 5 — Animation & Timeline
**Tag : `milestone/v5.0-animation`**

### 5.1 — Timeline web (remplace les timeline Qt)
- [x] Barre de timeline avec scrubbing (lecture du `startTimeCode` / `endTimeCode` depuis le stage)
- [x] Play / Pause / Step forward / Step backward
- [x] Sync `_driver.SetTime(tc)` côté WASM et attributs time-sampled côté serveur
- [x] FPS configurable

### 5.2 — Curve Editor web (remplace `opendcc.anim_engine.ui.graph_editor`)
- [x] Canvas 2D pour afficher les courbes d'animation (preview time-sampled attributes)
- [x] Édition de keyframes : ajout, suppression, déplacement (version basique)
- [x] Types d'interpolation : linear, bezier, step (preview + mode persistant)

---

## Phase 6 — Outils spécialisés & HydraOps
**Tag : `milestone/v6.0-advanced-tools`**

### 6.1 — Sculpt Tool web (remplace `opendcc.usd_editor.sculpt_tool`)
- [ ] Brush interactif sur mesh dans le viewport Three.js
- [ ] Modification server-side des `points` via endpoint dédié
- [ ] Modes : push/pull, smooth, flatten

### 6.2 — Point Instancer Tool web (remplace `opendcc.usd_editor.point_instancer_tool`)
- [ ] Scatter points sur surface
- [ ] Édition des proto-indices, positions, orientations, scales

### 6.3 — Paint Primvar / Texture web
- [ ] Projection de peinture sur mesh → écriture primvar ou texture
- [ ] Color picker + brush size/opacity

### 6.4 — Bezier Tool web (remplace `opendcc.usd_editor.bezier_tool`)
- [ ] Dessin interactif de courbes BasisCurves dans le viewport
- [ ] Édition des handles

### 6.5 — HydraOps web (remplace `opendcc.hydra_op.*`)
- [ ] Interface node graph pour les scene indices Hydra
- [ ] Configuration des render settings

---

## Phase 7 — Production Readiness
**Tag : `milestone/v7.0-production`**

### 7.1 — Multi-utilisateur & Live Share
- [ ] Implémentation du `USD Delta IPC Syncing` via WebSocket (remplace `opendcc.usd_editor.live_share`)
- [ ] Curseurs multi-utilisateurs dans le viewport
- [ ] Locking de prims en édition

### 7.2 — Performance & Scalabilité
- [ ] Profiling des scènes lourdes (100K+ prims)
- [ ] Streaming progressif du stage (pagination de la hiérarchie)
- [ ] Web Workers pour les opérations CPU-intensive côté client
- [ ] Compression WebSocket (permessage-deflate)

### 7.3 — Sécurité & Déploiement
- [ ] Authentification (JWT ou OAuth2) sur les endpoints REST
- [ ] HTTPS / WSS avec certificats
- [ ] Rate limiting
- [ ] Documentation API (Swagger auto via FastAPI, déjà disponible sur `/docs`)

### 7.4 — Tests & Documentation
- [ ] Tests unitaires Python (pytest) pour tous les endpoints
- [ ] Tests E2E navigateur (Playwright)
- [ ] Documentation utilisateur (guide de démarrage, screenshots)
- [ ] Documentation développeur (architecture, API, extension de plugins)

---

## Récapitulatif des tags

| Tag | Phase | Description |
|---|---|---|
| `milestone/v0.0-baseline` | Phase 0 | État actuel — web server + WASM viewport fonctionnels |
| `milestone/v1.0-gpu-infra` | Phase 1 | Docker GPU + Hydra headless + CI |
| `milestone/v2.0-scene-graph` | Phase 2 | Outliner avancé + Properties + Create menu |
| `milestone/v3.0-viewport` | Phase 3 | Viewport interactif + gizmos + render modes |
| `milestone/v4.0-dcc-tools` | Phase 4 | Material/UV/Node editors + Script editor |
| `milestone/v5.0-animation` | Phase 5 | Timeline + Curve editor |
| `milestone/v6.0-advanced-tools` | Phase 6 | Sculpt, Paint, Instancer, Bezier, HydraOps |
| `milestone/v7.0-production` | Phase 7 | Multi-user, performance, sécurité, tests |

---

*Ce document est vivant. Chaque phase sera détaillée en issues/tickets au fur et à mesure de l'avancement.*
