"""
usd_to_gltf.py
==============
USD Stage → GLB (binary glTF 2.0) converter.

Converts an open UsdStage into a self-contained GLB file that Three.js can
load with GLTFLoader.

Supported:
  - UsdGeomMesh: points, normals, face-varying / vertex UVs (primvars:st)
  - UsdGeomXform / UsdGeomCamera / any xformable: local transform → node matrix
  - UsdPreviewSurface → glTF PBR metallic-roughness (constant values, no textures yet)
  - Fan-triangulation of n-gon faces
  - Prim path stored in node extras → Three.js userData (enables click-to-select)
  - Automatic Z-up → Y-up rotation wrapper node

Coordinate-system note
----------------------
USD matrices use row-vector convention  (v' = v * M, translation in last ROW).
glTF uses column-vector convention      (v' = M * v, translation in last COLUMN).
They are related by transposition.

When we write the USD GfMatrix4d[row][col] values in row-major order
  flat = [m[0][0], m[0][1], ..., m[3][2], m[3][3]]
and glTF *interprets* that flat array as column-major
  gltf[col*4+row] = m[row][col]
  => gltf_M[row][col] = m[col][row]   (i.e. the transpose of USD matrix)
This is exactly the coordinate-convention flip we need, so the conversion
is trivial: just flatten USD in row-major order.
"""

from __future__ import annotations

import json
import math
import struct
import logging
from typing import Any

log = logging.getLogger(__name__)

# glTF component-type constants
_CT_FLOAT  = 5126   # 32-bit float
_CT_UINT32 = 5125   # uint32
_CT_UINT16 = 5123   # uint16


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def stage_to_glb(stage) -> bytes:
    """
    Convert *stage* (UsdStage) to GLB bytes.
    Requires ``pxr`` (OpenUSD Python bindings) to be importable.
    """
    from pxr import UsdGeom, Usd

    builder = _GltfBuilder()
    time    = Usd.TimeCode.Default()

    # Detect coordinate system: Z-up needs a wrapper rotation
    up_axis = str(UsdGeom.GetStageUpAxis(stage))
    z_up    = (up_axis.upper() == "Z")

    root_node_indices: list[int] = []
    for prim in stage.GetPseudoRoot().GetChildren():
        idx = builder.visit_prim(prim, time)
        if idx is not None:
            root_node_indices.append(idx)

    if z_up and root_node_indices:
        # Wrap everything in a -90° rotation around X: maps Z-up → Y-up
        # Column-major glTF matrix (verified above):
        wrap = {
            "name": "__z_to_y_up__",
            "matrix": [1, 0, 0, 0,  0, 0, -1, 0,  0, 1, 0, 0,  0, 0, 0, 1],
            "children": root_node_indices,
        }
        wrap_idx = len(builder.nodes)
        builder.nodes.append(wrap)
        root_node_indices = [wrap_idx]

    return builder.to_glb(root_node_indices)


# ─────────────────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────────────────

class _GltfBuilder:
    def __init__(self) -> None:
        self.nodes:        list[dict] = []
        self.meshes:       list[dict] = []
        self.materials:    list[dict] = []
        self.accessors:    list[dict] = []
        self.buffer_views: list[dict] = []
        self._bin         = bytearray()
        self._mat_cache:  dict[tuple, int] = {}

    # ── Prim tree traversal ───────────────────────────────────────────────

    def visit_prim(self, prim, time) -> int | None:
        """Recursively build a glTF node from a USD prim and its children."""
        from pxr import UsdGeom

        if not prim.IsActive():
            return None

        node: dict[str, Any] = {
            "name": prim.GetName(),
            "extras": {"primPath": str(prim.GetPath())},
        }

        # Local transform
        xformable = UsdGeom.Xformable(prim)
        if xformable:
            try:
                result = xformable.GetLocalTransformation(time)
                # Python API returns either GfMatrix4d or (GfMatrix4d, bool)
                mat4 = result[0] if isinstance(result, tuple) else result
                node["matrix"] = _gf_matrix_to_gltf(mat4)
            except Exception as exc:
                log.debug("transform error on %s: %s", prim.GetPath(), exc)

        # Mesh geometry
        if prim.IsA(UsdGeom.Mesh):
            mesh_idx = self._add_mesh(prim, time)
            if mesh_idx is not None:
                node["mesh"] = mesh_idx

        # Children
        children: list[int] = []
        for child in prim.GetChildren():
            child_idx = self.visit_prim(child, time)
            if child_idx is not None:
                children.append(child_idx)
        if children:
            node["children"] = children

        idx = len(self.nodes)
        self.nodes.append(node)
        return idx

    # ── Mesh geometry ─────────────────────────────────────────────────────

    def _add_mesh(self, prim, time) -> int | None:
        from pxr import UsdGeom

        mesh = UsdGeom.Mesh(prim)

        pts_attr = mesh.GetPointsAttr()
        if not pts_attr or not pts_attr.HasValue():
            return None

        raw_pts   = pts_attr.Get(time)
        raw_fvc   = mesh.GetFaceVertexCountsAttr().Get(time)
        raw_fvi   = mesh.GetFaceVertexIndicesAttr().Get(time)

        if raw_pts is None or raw_fvc is None or raw_fvi is None:
            return None

        pts = [list(p) for p in raw_pts]
        fvc = list(raw_fvc)
        fvi = list(raw_fvi)

        # ── Normals
        normals, norm_interp = None, "faceVarying"
        nrm_attr = mesh.GetNormalsAttr()
        if nrm_attr and nrm_attr.HasValue():
            nrm_val = nrm_attr.Get(time)
            if nrm_val:
                normals      = [list(n) for n in nrm_val]
                norm_interp  = str(mesh.GetNormalsInterpolation())

        # ── UVs (primvars:st or primvars:map1)
        uvs, uv_interp, uv_indices = None, "faceVarying", None
        primvars_api = UsdGeom.PrimvarsAPI(prim)
        for uv_name in ("st", "map1", "uv"):
            pv = primvars_api.GetPrimvar(uv_name)
            if pv and pv.HasValue():
                uv_val = pv.Get(time)
                if uv_val:
                    uvs        = [list(uv) for uv in uv_val]
                    uv_interp  = str(pv.GetInterpolation())
                    if pv.IsIndexed():
                        raw_idx = pv.GetIndices(time)
                        if raw_idx:
                            uv_indices = list(raw_idx)
                break

        # ── Expand to indexed triangle soup
        out_pos, out_nrm, out_uv, out_idx = _expand_mesh(
            pts, normals, uvs, fvc, fvi, uv_indices, norm_interp, uv_interp
        )

        if not out_idx:
            return None

        # Flat normals if none were provided
        if not out_nrm:
            out_nrm = _compute_flat_normals(out_pos, out_idx)

        # ── Material
        mat_idx = self._get_material(prim)

        # ── Pack into glTF
        pos_acc = self._add_vec3_accessor(out_pos, is_position=True)
        nrm_acc = self._add_vec3_accessor(out_nrm, is_position=False)
        idx_acc = self._add_index_accessor(out_idx)

        primitive: dict = {
            "attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc},
            "indices": idx_acc,
            "material": mat_idx,
        }
        if out_uv:
            uv_acc = self._add_vec2_accessor(out_uv)
            primitive["attributes"]["TEXCOORD_0"] = uv_acc

        mesh_idx = len(self.meshes)
        self.meshes.append({
            "name": prim.GetName(),
            "primitives": [primitive],
        })
        return mesh_idx

    # ── Materials ─────────────────────────────────────────────────────────

    def _get_material(self, prim) -> int:
        """Return a glTF material index for the material bound to *prim*."""
        from pxr import UsdShade, Gf

        try:
            binding = UsdShade.MaterialBindingAPI(prim)
            mat     = binding.ComputeBoundMaterial()[0]
            if mat:
                surface_output = mat.GetSurfaceOutput()
                if surface_output:
                    connected, src_name, src_type = surface_output.GetConnectedSource()
                    if connected:
                        shader = UsdShade.Shader(connected.GetPrim())
                        shader_id = shader.GetIdAttr().Get()
                        if str(shader_id) == "UsdPreviewSurface":
                            def _get_float(name: str, default: float) -> float:
                                inp = shader.GetInput(name)
                                v   = inp.Get() if inp else None
                                return float(v) if v is not None else default

                            def _get_color(name: str, default) -> list:
                                inp = shader.GetInput(name)
                                v   = inp.Get() if inp else None
                                return [float(v[0]), float(v[1]), float(v[2])] if v else default

                            diffuse   = _get_color("diffuseColor",   [0.8, 0.8, 0.8])
                            emissive  = _get_color("emissiveColor",  [0.0, 0.0, 0.0])
                            metallic  = _get_float("metallic",       0.0)
                            roughness = _get_float("roughness",      0.5)
                            opacity   = _get_float("opacity",        1.0)

                            key = (
                                round(diffuse[0], 4), round(diffuse[1], 4), round(diffuse[2], 4),
                                round(metallic, 4), round(roughness, 4), round(opacity, 4),
                            )
                            if key in self._mat_cache:
                                return self._mat_cache[key]

                            mat_def: dict = {
                                "name": str(mat.GetPath()),
                                "pbrMetallicRoughness": {
                                    "baseColorFactor":      diffuse + [opacity],
                                    "metallicFactor":       metallic,
                                    "roughnessFactor":      roughness,
                                },
                                "emissiveFactor": emissive,
                            }
                            if opacity < 1.0:
                                mat_def["alphaMode"]   = "BLEND"
                                mat_def["alphaCutoff"] = 0.5

                            idx = len(self.materials)
                            self.materials.append(mat_def)
                            self._mat_cache[key] = idx
                            return idx
        except Exception as exc:
            log.debug("material error on %s: %s", prim.GetPath(), exc)

        return self._default_material()

    def _default_material(self) -> int:
        """Return (creating once) a plain grey default material."""
        if not hasattr(self, "_default_mat_idx"):
            self._default_mat_idx = len(self.materials)
            self.materials.append({
                "name": "__default__",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.8, 0.8, 0.8, 1.0],
                    "metallicFactor":  0.0,
                    "roughnessFactor": 0.5,
                },
            })
        return self._default_mat_idx

    # ── Buffer / accessor helpers ─────────────────────────────────────────

    def _append(self, data: bytes) -> int:
        """Append *data* to the binary buffer (4-byte aligned). Returns offset."""
        offset = len(self._bin)
        self._bin.extend(data)
        # Pad to 4-byte boundary
        pad = (4 - len(self._bin) % 4) % 4
        self._bin.extend(b"\x00" * pad)
        return offset

    def _add_buffer_view(self, byte_length: int, byte_offset: int,
                         target: int | None = None) -> int:
        bv: dict = {"buffer": 0, "byteOffset": byte_offset, "byteLength": byte_length}
        if target is not None:
            bv["target"] = target
        idx = len(self.buffer_views)
        self.buffer_views.append(bv)
        return idx

    def _add_vec3_accessor(self, vecs: list[list[float]],
                           is_position: bool = False) -> int:
        n     = len(vecs)
        data  = struct.pack(f"{n * 3}f", *(c for v in vecs for c in v))
        off   = self._append(data)
        bv    = self._add_buffer_view(len(data), off, target=34962)  # ARRAY_BUFFER
        acc: dict = {
            "bufferView":    bv,
            "componentType": _CT_FLOAT,
            "count":         n,
            "type":          "VEC3",
        }
        if is_position and vecs:
            xs = [v[0] for v in vecs]
            ys = [v[1] for v in vecs]
            zs = [v[2] for v in vecs]
            acc["min"] = [min(xs), min(ys), min(zs)]
            acc["max"] = [max(xs), max(ys), max(zs)]
        idx = len(self.accessors)
        self.accessors.append(acc)
        return idx

    def _add_vec2_accessor(self, vecs: list[list[float]]) -> int:
        n    = len(vecs)
        data = struct.pack(f"{n * 2}f", *(c for v in vecs for c in v))
        off  = self._append(data)
        bv   = self._add_buffer_view(len(data), off, target=34962)
        idx  = len(self.accessors)
        self.accessors.append({
            "bufferView":    bv,
            "componentType": _CT_FLOAT,
            "count":         n,
            "type":          "VEC2",
        })
        return idx

    def _add_index_accessor(self, indices: list[int]) -> int:
        n  = len(indices)
        if max(indices) < 65536:
            fmt, ct, sz = "H", _CT_UINT16, 2
        else:
            fmt, ct, sz = "I", _CT_UINT32, 4
        data  = struct.pack(f"{n}{fmt}", *indices)
        off   = self._append(data)
        bv    = self._add_buffer_view(len(data), off, target=34963)  # ELEMENT_ARRAY_BUFFER
        idx   = len(self.accessors)
        self.accessors.append({
            "bufferView":    bv,
            "componentType": ct,
            "count":         n,
            "type":          "SCALAR",
        })
        return idx

    # ── GLB packing ───────────────────────────────────────────────────────

    def to_glb(self, root_nodes: list[int]) -> bytes:
        gltf: dict = {
            "asset":       {"version": "2.0", "generator": "OpenDCC Web"},
            "scene":       0,
            "scenes":      [{"nodes": root_nodes}],
            "nodes":       self.nodes,
        }
        if self.meshes:
            gltf["meshes"] = self.meshes
        if self.materials:
            gltf["materials"] = self.materials
        if self.accessors:
            gltf["accessors"] = self.accessors
        if self.buffer_views:
            gltf["bufferViews"] = self.buffer_views
        if self._bin:
            gltf["buffers"] = [{"byteLength": len(self._bin)}]

        return _pack_glb(gltf, bytes(self._bin))


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _expand_mesh(
    pts:         list[list[float]],
    normals:     list[list[float]] | None,
    uvs:         list[list[float]] | None,
    fvc:         list[int],
    fvi:         list[int],
    uv_indices:  list[int] | None,
    norm_interp: str,
    uv_interp:   str,
) -> tuple[list, list, list, list]:
    """
    Fan-triangulate an n-gon mesh and expand to a deduplicated indexed
    triangle list compatible with glTF (one index per unique
    (position, normal, uv) tuple).

    Returns (out_pos, out_nrm, out_uv, out_idx).
    out_nrm / out_uv may be empty lists if the corresponding input is None.
    """
    vertex_map: dict[tuple, int] = {}
    out_pos: list[list[float]] = []
    out_nrm: list[list[float]] = []
    out_uv:  list[list[float]] = []
    out_idx: list[int]         = []

    face_start = 0
    fv_idx     = 0   # monotonically increasing "face-vertex" counter

    for count in fvc:
        # Fan triangulation: first vertex + each consecutive pair
        for tri in range(count - 2):
            corners = [0, tri + 1, tri + 2]
            for c in corners:
                fi      = face_start + c          # index into fvi
                pos_idx = fvi[fi]

                # ── Normal index
                if normals is None:
                    nrm_idx = -1
                elif norm_interp == "faceVarying":
                    nrm_idx = fi
                elif norm_interp == "vertex":
                    nrm_idx = pos_idx
                else:                              # uniform / constant
                    nrm_idx = 0

                # ── UV index
                if uvs is None:
                    uv_i = -1
                elif uv_interp == "faceVarying":
                    raw_i = fi
                    uv_i  = uv_indices[raw_i] if uv_indices else raw_i
                elif uv_interp == "vertex":
                    uv_i  = uv_indices[pos_idx] if uv_indices else pos_idx
                else:
                    uv_i  = 0

                key = (pos_idx, nrm_idx, uv_i)
                if key not in vertex_map:
                    new_i              = len(out_pos)
                    vertex_map[key]    = new_i
                    out_pos.append(pts[pos_idx])
                    if normals and nrm_idx >= 0 and nrm_idx < len(normals):
                        out_nrm.append(normals[nrm_idx])
                    if uvs and uv_i >= 0 and uv_i < len(uvs):
                        out_uv.append(uvs[uv_i])

                out_idx.append(vertex_map[key])

        face_start += count
        fv_idx     += count

    return out_pos, out_nrm, out_uv, out_idx


def _compute_flat_normals(
    positions: list[list[float]],
    indices:   list[int],
) -> list[list[float]]:
    """Compute per-vertex averaged face normals (flat shading)."""
    normals = [[0.0, 0.0, 0.0] for _ in range(len(positions))]
    counts  = [0] * len(positions)

    for i in range(0, len(indices), 3):
        i0, i1, i2 = indices[i], indices[i + 1], indices[i + 2]
        p0, p1, p2 = positions[i0], positions[i1], positions[i2]

        # Edge vectors
        e1 = [p1[j] - p0[j] for j in range(3)]
        e2 = [p2[j] - p0[j] for j in range(3)]

        # Cross product
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        nz = e1[0] * e2[1] - e1[1] * e2[0]

        # Normalise
        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        nx, ny, nz = nx / length, ny / length, nz / length

        for idx in (i0, i1, i2):
            normals[idx][0] += nx
            normals[idx][1] += ny
            normals[idx][2] += nz
            counts[idx]     += 1

    # Average and renormalise
    for i, (n, c) in enumerate(zip(normals, counts)):
        if c:
            length = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2) or 1.0
            normals[i] = [n[j] / length for j in range(3)]

    return normals


# ─────────────────────────────────────────────────────────────────────────────
# USD → glTF matrix conversion
# ─────────────────────────────────────────────────────────────────────────────

def _gf_matrix_to_gltf(mat4) -> list[float]:
    """
    Convert a USD GfMatrix4d to a glTF column-major 16-element array.

    USD: row-vector convention (v' = v * M), translation in last ROW.
    glTF: column-vector convention (v' = M * v), column-major storage.

    Proof that USD row-major flat == correct glTF column-major:
      gltf_M = USD_M^T
      gltf_array[col*4+row] = gltf_M[row][col] = USD_M[col][row]
      => gltf_array[i] = USD_M[i//4][i%4]   (row-major flat of USD)
    """
    return [float(mat4[row][col]) for row in range(4) for col in range(4)]


# ─────────────────────────────────────────────────────────────────────────────
# GLB binary packing
# ─────────────────────────────────────────────────────────────────────────────

def _pack_glb(gltf_json: dict, bin_data: bytes) -> bytes:
    """Pack glTF JSON dict + binary buffer into a GLB file (spec §3)."""
    # JSON chunk — pad with spaces to 4-byte boundary
    json_bytes  = json.dumps(gltf_json, separators=(",", ":")).encode("utf-8")
    json_pad    = (4 - len(json_bytes) % 4) % 4
    json_bytes += b" " * json_pad

    # BIN chunk — pad with zeros
    bin_pad  = (4 - len(bin_data) % 4) % 4 if bin_data else 0
    bin_data = bin_data + b"\x00" * bin_pad

    # Chunk headers: [uint32 chunkLength, uint32 chunkType, chunkData...]
    json_chunk = struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes  # "JSON"
    bin_chunk  = struct.pack("<II", len(bin_data),  0x004E4942) + bin_data     # "BIN\0"

    total = 12 + len(json_chunk) + (len(bin_chunk) if bin_data else 0)
    header = struct.pack("<III", 0x46546C67, 2, total)                          # "glTF", v2

    return header + json_chunk + (bin_chunk if bin_data else b"")
