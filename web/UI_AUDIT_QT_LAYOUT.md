# Audit UI — Organisation Qt vs Web (OpenDCC)

Date: 2026-03-22

## Références code Qt

- `src/python/opendcc/layouts.py`
  - `default_layout()` :
    - `viewport` (centre)
    - `outliner` (dock gauche)
    - `attribute_editor` (dock sous outliner)
  - `scripting_layout()` :
    - `viewport` (centre)
    - `outliner` (gauche)
    - `script_editor` (droite)
    - zone basse: `Layers`, `usd_details_view`, `logger`

- `src/lib/opendcc/app/ui/main_window_timeline.cpp`
  - Timeline gérée en barres d'outils **en bas de fenêtre**
  - séparée des panneaux de propriétés

- `src/packages/opendcc.anim_engine/lib/opendcc/anim_engine/anim_engine_entry_point.cpp`
  - `graph_editor` et `channel_editor` enregistrés comme **panneaux dédiés**
  - non intégrés dans l'Attribute/Properties editor

## Constat

Le paradigme Qt est orienté **panneaux spécialisés**, dockables, avec layouts:
- Properties/Attribute Editor = inspection/édition de prim
- Graph/Curve Editor = panneau séparé (animation)
- Timeline = zone basse dédiée

## Écart côté Web (avant correction)

- Curve preview avait été injecté dans `Properties`.
- Les sections lourdes (Geometry) étaient ouvertes par défaut.

## Ajustements appliqués côté Web

- Curve Editor déplacé dans un **panel séparé** (`#curve-editor`), toggle via bouton `Curve Editor`.
- UV Editor MVP ajouté en **workbench dédié** (`#uv-workbench`) avec modes `Split/Detached/Fullscreen`.
- Properties conserve uniquement les infos d'inspection prim/material.
- Sections `Geometry` et `Other` dans Properties: **repliées par défaut**.
- Bouton `↻ Refresh` dans Properties (rafraîchissement on-demand).

## Direction recommandée (suite)

1. Conserver la séparation stricte:
   - Properties = data inspection/édition
   - Curve Editor = animation data + key editing
2. Ajouter un petit gestionnaire de layout web (Default / Scripting) inspiré de `layouts.py`.
3. Éviter les panel-in-panel permanents; privilégier des panneaux activables.
