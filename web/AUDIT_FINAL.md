# OpenDCC Web — Audit Final (Go / No-Go)

Date: 2026-03-22
Branch: `develop`

## Règles de validation globale
- 0 bloquant
- 0 régression fonctionnelle critique
- Performance acceptable sur `Kitchen_set`
- Authoring USD non-destructif validé

---

## GO / NO-GO express (10 cases)
> Validation rapide avant release/cut de démo.

1. [ ] `/health` = `ok`
2. [ ] Node Workbench s’ouvre en `Split`, `Detached`, `Fullscreen` sans bug
3. [ ] `+ Create & Bind PreviewSurface` fonctionne sur prim sans matériau
4. [ ] Node `create` fonctionne (au moins `UsdUVTexture`)
5. [ ] Drag & drop `connect` node→node fonctionne
6. [ ] Drag & drop `connect` shader→material `surface` fonctionne
7. [ ] `disconnect` (unlink) fonctionne sans casser le graphe
8. [ ] `delete` node (bouton + touche `Delete`) fonctionne
9. [ ] Material Editor reste non-régressif (param + texture update live)
10. [ ] `save_edits_as` confirme l’authoring non-destructif (sidecar overrides)

### Règle de décision express
- **GO** = 10/10 vert
- **NO-GO** = ≥1 case rouge

---

## 1) Parité fonctionnelle (vs native OpenDCC)

### Timeline / Curve
- [ ] Lecture correcte `start/end/fps` stage
- [ ] Play/pause/step OK
- [ ] Keyframe set/delete/move OK
- [ ] Interpolation `linear/step/bezier` persistée (`customData['opendcc:interp']`)
- [ ] Sync time-sampled stable

### Script Editor
- [ ] Édition Python stable (CodeMirror + fallback)
- [ ] Exécution script OK + feedback utilisateur clair
- [ ] Historique/autocomplete non cassants

### Material Editor
- [ ] Inspection binding matériau OK
- [ ] Create & Bind sur prim sans matériau OK
- [ ] Unbind OK (`all/preview/full`)
- [ ] Purpose/strength appliqués correctement
- [ ] Paramètres PreviewSurface éditables sans reload lourd
- [ ] Textures connect/disconnect OK
- [ ] MatX/vendor détectés en read-only sans régression

---

## 2) USD correctness (non-destructif)
- [ ] En mode pxr, edits écrits dans `*.opendcc_edits.usda` uniquement
- [ ] Root layer importée reste intacte
- [ ] `/api/stage/save` sauve l’edit layer (pas root)
- [ ] `/api/stage/save_edits_as` exporte proprement les overrides
- [ ] Computed bindings preview/full corrects
- [ ] UpAxis correct appliqué au load (Y/Z)
- [ ] Composition layers (synthetic root) cohérente

---

## 3) Robustesse / non-régression
- [ ] Outliner : sélection multi, rename, duplicate, group, delete
- [ ] Gizmo translate/rotate/scale stable
- [ ] Picking viewport fiable
- [ ] Reload scène sans perte d’état critique
- [ ] WebSocket events (`scene_changed`, `material_changed`) cohérents
- [ ] Aucune régression visible sur scènes simples + `Kitchen_set`

---

## 4) Performance / UX
- [ ] `loadStage` `Kitchen_set` dans le budget cible
- [ ] Édition matériau sans rebuild complet perceptible
- [ ] Pas de freeze UI pendant batch d’assets
- [ ] Bruit console maîtrisé (warnings ciblés filtrés, erreurs réelles visibles)
- [ ] Pas de clignotement gênant au reload incrémental

---

## 5) Qualité code
- [ ] Pas de duplication évidente non justifiée
- [ ] Fonctions longues/fragiles identifiées pour refacto
- [ ] Logs dev propres (pas de spam)
- [ ] Gestion erreurs explicite (UI + backend)
- [ ] Handoff / roadmap à jour et fidèle au réel

---

## Cas de test minimum (à exécuter)
1. Scène légère (`new stage`)
2. Scène intermédiaire avec materials
3. `Kitchen_set` (charge lourde)
4. Cas layered pxr (import + edits + `save_edits_as`)
5. Mat read-only (MaterialX/vendor) + PreviewSurface editable

---

## Tableau de résultats
| Bloc | Statut | Evidence (logs/screens/endpoint) | Owner | Date |
|---|---|---|---|---|
| Parité fonctionnelle | ⬜ |  |  |  |
| USD correctness | ⬜ |  |  |  |
| Robustesse | ⬜ |  |  |  |
| Performance / UX | ⬜ |  |  |  |
| Qualité code | ⬜ |  |  |  |

### Décision finale
- GO / NO-GO: ⬜
- Commentaire final: 
