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
| Docker rocky9-gpu (nvidia) | ⚠️ Défini dans compose, non testé |
| 34 packages C++/Qt (plugins OpenDCC) | ✅ Code source, mais **aucun exposé côté web** |

---

## Phase 1 — Infrastructure GPU & Docker Hardening
**Tag : `milestone/v1.0-gpu-infra`**

### 1.1 — NVIDIA Container Toolkit
- [ ] Documenter les prérequis host : `nvidia-driver`, `nvidia-container-toolkit`
- [ ] Créer `docker/Dockerfile.rocky9.gpu` (hérite de `runtime`, ajoute CUDA runtime + EGL nvidia)
- [ ] Ajouter un script `scripts/setup_nvidia_docker.sh` (détecte driver, installe toolkit, teste `nvidia-smi`)
- [ ] Tester `docker compose up rocky9-gpu` avec vérification `nvidia-smi` dans le container
- [ ] Ajouter un endpoint `/health` enrichi : `gpu: true/false`, `gpu_name`, `vram`

### 1.2 — Hydra Storm headless GPU (server-side render)
- [ ] Activer `hdStorm` avec EGL dans le container GPU
- [ ] Ajouter endpoint `/api/render/snapshot` → rendu server-side Hydra → PNG (pour preview rapide)
- [ ] Fallback automatique : si pas de GPU → rendu WASM côté client (déjà en place)

### 1.3 — CI / Smoke tests
- [ ] Script `scripts/test_docker.sh` : build + up + curl `/health` + vérif `/api/stage/export.usda`
- [ ] Ajouter `docker compose --profile test` pour run automatique

---

## Phase 2 — Outliner & Scene Graph Web avancé
**Tag : `milestone/v2.0-scene-graph`**

### 2.1 — Outliner enrichi (remplace `opendcc.hydra_op.ui.scene_graph`)
- [ ] Drag & drop pour reparenting (appel `/api/prims/parent`)
- [ ] Menu contextuel (clic droit) : Create, Delete, Duplicate, Group, Rename, Visibility
- [ ] Icônes typées par prim type (Mesh, Xform, Camera, Light, Material…)
- [ ] Recherche / filtre de prims en temps réel
- [ ] Multi-sélection (Ctrl+click, Shift+click)
- [ ] Indicateur visuel actif/inactif, visible/invisible

### 2.2 — Properties Panel avancé
- [ ] Édition inline de tous les types USD (Vec3, Color, Matrix, float, int, bool, token, string)
- [ ] Widgets adaptés : color picker pour `diffuseColor`, slider pour `roughness`, etc.
- [ ] Section pliable par catégorie (Xform, Geometry, Material, Custom)
- [ ] Affichage des relations (material binding, references)

### 2.3 — Create Menu (remplace `opendcc.usd_editor.common_cmds`)
- [ ] Toolbar "Create" : tous les types de prims (Mesh primitives, Lights, Camera, Xform, Scope)
- [ ] API endpoint `/api/prims/create` étendu pour supporter les paramètres initiaux

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

### 3.3 — Modes de rendu
- [ ] Toggle wireframe / solid / shaded
- [ ] Toggle grid, axes helper
- [ ] Boutons de vue : Front, Back, Left, Right, Top, Bottom, Perspective
- [ ] Stats overlay configurable (tris, draw calls, fps)

---

## Phase 4 — Outils DCC dans le Web
**Tag : `milestone/v4.0-dcc-tools`**

### 4.1 — Material Editor web (remplace `opendcc.usd_editor.material_editor`)
- [ ] Lecture des `UsdShade` bindings et affichage en panneau dédié
- [ ] Édition des paramètres `UsdPreviewSurface` (color, metallic, roughness, opacity)
- [ ] Preview matériau temps réel dans le viewport

### 4.2 — UV Editor web (remplace `opendcc.usd_editor.uv_editor`)
- [ ] Canvas 2D (Three.js ou Canvas API) affichant les UVs
- [ ] Sélection de faces/vertices UV
- [ ] Transformations UV basiques (translate, rotate, scale)

### 4.3 — Node Editor web (remplace `opendcc.ui.node_editor` + `opendcc.usd_editor.usd_node_editor`)
- [ ] Framework de node graph en canvas (librairie : rete.js, litegraph.js, ou custom)
- [ ] Représentation des `UsdShade` networks (Shader → Material → Binding)
- [ ] Connection drag & drop entre nodes

### 4.4 — Script Editor amélioré (remplace `opendcc.ui.script_editor`)
- [ ] Syntax highlighting Python (CodeMirror ou Monaco Editor)
- [ ] Autocomplétion basique (`stage.`, `prim.`, `UsdGeom.`)
- [ ] Historique des commandes
- [ ] Accès aux globals `app`, `session`, `stage`

---

## Phase 5 — Animation & Timeline
**Tag : `milestone/v5.0-animation`**

### 5.1 — Timeline web (remplace les timeline Qt)
- [ ] Barre de timeline avec scrubbing (lecture du `startTimeCode` / `endTimeCode` depuis le stage)
- [ ] Play / Pause / Step forward / Step backward
- [ ] Sync `_driver.SetTime(tc)` côté WASM et attributs time-sampled côté serveur
- [ ] FPS configurable

### 5.2 — Curve Editor web (remplace `opendcc.anim_engine.ui.graph_editor`)
- [ ] Canvas 2D pour afficher les courbes d'animation (AnimX ou time-sampled attributes)
- [ ] Édition de keyframes : ajout, suppression, déplacement
- [ ] Types d'interpolation : linear, bezier, step

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
