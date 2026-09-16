"""Administrator-owned API-format workflows; never accept graphs from project users."""
from __future__ import annotations

import copy
import json
import re
import secrets
from pathlib import Path

from .contracts import fingerprint

BUILTIN_NODES = {
    "CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage", "KSampler",
    "VAEDecode", "VAEEncode", "VAEEncodeForInpaint", "SaveImage", "LoadImage",
    "ImageScale", "ImageToMask", "LoadImageMask",
}
SOURCES = {"prompt", "negative_prompt", "seed", "width", "height", "denoise",
           "filename_prefix", "mask"} | {f"ref_{i}" for i in range(8)}


def check_workflow(workflow: dict, allowed: set[str]) -> None:
    if not isinstance(workflow, dict) or workflow.get("version") != 1:
        raise ValueError("Comfy workflow manifest requires version=1")
    graph = workflow.get("graph")
    if not isinstance(graph, dict) or not 1 <= len(graph) <= 128:
        raise ValueError("Expected API-format node map, not a canvas workflow export")
    if workflow.get("mode") not in ("text", "reference", "inpaint"):
        raise ValueError("Unknown Comfy image workflow mode")
    count = workflow.get("reference_count", 0)
    if type(count) is not int or not 0 <= count <= 8:
        raise ValueError("Invalid reference_count")
    if (workflow["mode"] == "text") != (count == 0):
        raise ValueError("Text workflows take no references; reference workflows require references")
    for node_id, node in graph.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", node_id):
            raise ValueError("Invalid node ID")
        if not isinstance(node, dict) or node.get("class_type") not in allowed:
            raise ValueError("Workflow contains an unapproved node class")
        if not isinstance(node.get("inputs"), dict):
            raise ValueError("Workflow node is missing inputs")
        for value in node["inputs"].values():
            if isinstance(value, list):
                if len(value) != 2 or value[0] not in graph or type(value[1]) is not int or value[1] < 0:
                    raise ValueError("Invalid workflow link")
        if "batch_size" in node["inputs"] and node["inputs"]["batch_size"] != 1:
            raise ValueError("This adapter creates exactly one image candidate per task")
    output = workflow.get("output_node")
    if output not in graph or graph[output]["class_type"] != "SaveImage":
        raise ValueError("Exactly one declared SaveImage output is required")
    if sum(n["class_type"] == "SaveImage" for n in graph.values()) != 1:
        raise ValueError("Use a single SaveImage output per image workflow")
    bindings = workflow.get("bindings", {})
    required = {"prompt", "seed", "filename_prefix"} | {f"ref_{i}" for i in range(count)}
    if workflow["mode"] == "inpaint":
        required.add("mask")
    if not isinstance(bindings, dict) or not required <= bindings.keys() or not bindings.keys() <= SOURCES:
        raise ValueError("Missing or unsupported Comfy bindings")
    expected_refs = {f"ref_{i}" for i in range(count)}
    if {k for k in bindings if k.startswith("ref_")} != expected_refs:
        raise ValueError("Reference bindings do not match reference_count")
    if ("mask" in bindings) != (workflow["mode"] == "inpaint"):
        raise ValueError("Mask binding must be exclusive to inpaint")
    targets = set()
    for source, entries in bindings.items():
        if not isinstance(entries, list) or not entries:
            raise ValueError("Each binding must contain [node_id, input_name] targets")
        for pair in entries:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("Invalid binding target")
            nid, key = pair
            if nid not in graph or key not in graph[nid]["inputs"] or isinstance(graph[nid]["inputs"][key], list):
                raise ValueError("Binding must target an existing literal node input")
            if (nid, key) in targets:
                raise ValueError("Two bindings cannot overwrite the same input")
            targets.add((nid, key))
    if bindings["filename_prefix"] != [[output, "filename_prefix"]]:
        raise ValueError("Output prefix must bind to the declared SaveImage node")
    # Reject cycles before any upload. Model/semantic validation remains server-side.
    visited, visiting = set(), set()
    def visit(nid):
        if nid in visiting:
            raise ValueError("Workflow contains a cycle")
        if nid in visited:
            return
        visiting.add(nid)
        for value in graph[nid]["inputs"].values():
            if isinstance(value, list):
                visit(value[0])
        visiting.remove(nid); visited.add(nid)
    for nid in graph:
        visit(nid)


def load_config(raw: dict, config_dir: Path) -> dict:
    """Read/validate at API/worker start. A task freezes the selected manifest."""
    from .comfy_transport import validate_endpoint
    cfg = copy.deepcopy(raw)
    validate_endpoint(cfg)
    cfg["base_url"]=cfg["base_url"].rstrip("/")
    if cfg.get("local_models_only") is not True:
        raise ValueError("Comfy adapter requires local_models_only=true; paid Partner Nodes are unsupported")
    allowed = set(cfg.get("allowed_nodes", sorted(BUILTIN_NODES)))
    if not allowed or any(not isinstance(x, str) for x in allowed):
        raise ValueError("Invalid administrator node allowlist")
    cfg["allowed_nodes"] = sorted(allowed)
    inflight = cfg.get("max_inflight", 1)
    if type(inflight) is not int or not 1 <= inflight <= 8:
        raise ValueError("max_inflight must be 1..8")
    cfg["max_inflight"] = inflight
    remote_timeout = cfg.get("remote_timeout_seconds", 1800)
    if type(remote_timeout) is not int or not 60 <= remote_timeout <= 86400:
        raise ValueError("remote_timeout_seconds must be 60..86400")
    cfg["remote_timeout_seconds"] = remote_timeout
    root = Path(cfg.get("workflow_dir", Path(__file__).parent / "workflows" / "comfyui"))
    if not root.is_absolute():
        root = config_dir / root
    root = root.resolve()
    entries = cfg.get("workflows")
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 20:
        raise ValueError("Configure 1..20 named workflow manifest files")
    loaded = {}
    for name, filename in entries.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or not isinstance(filename, str):
            raise ValueError("Invalid workflow ID or filename")
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or path.suffix != ".json" or path.stat().st_size > 512 * 1024:
            raise ValueError("Workflow must be a bounded JSON file inside workflow_dir")
        w = json.loads(path.read_text(encoding="utf-8"))
        check_workflow(w, allowed)
        loaded[name] = {"manifest": w, "sha256": fingerprint(w)}
    if cfg.get("default_workflow") not in loaded:
        raise ValueError("default_workflow must identify a configured manifest")
    cfg["workflows"] = loaded
    cfg.pop("workflow_dir", None)  # Private server paths must not reach task exports.
    return cfg


def prepare(snapshot: dict, operation_identity: str) -> None:
    """Freeze parameters before queuing; called for dry-run without network access."""
    cfg = snapshot["provider_config"]
    r = snapshot["request"]
    if r["kind"] != "image":
        raise ValueError("ComfyUI v0.3 supports images only, not video/audio generation")
    opts = r.get("image_options") or {}
    wid = opts.get("workflow") or cfg["default_workflow"]
    if wid not in cfg["workflows"]:
        raise ValueError("Unknown administrator-approved workflow")
    definition = cfg["workflows"][wid]
    w = definition["manifest"]
    check_workflow(w, set(cfg["allowed_nodes"]))
    if fingerprint(w) != definition["sha256"]:
        raise ValueError("Workflow fingerprint mismatch")
    ids = snapshot["reference_order"]
    if len(ids) != w["reference_count"]:
        raise ValueError(f"Workflow requires exactly {w['reference_count']} ordered reference images; received {len(ids)}")
    mask = opts.get("mask_asset_id")
    if bool(mask) != (w["mode"] == "inpaint"):
        raise ValueError("Provide a mask for inpaint only; white=repaint, black=preserve")
    for aid in ids + ([mask] if mask else []):
        a = snapshot["assets"].get(aid)
        if not a or a["kind"] != "image" or a["mock"] or a["review"] != "accepted":
            raise ValueError("Every remote reference/mask must be an accepted, non-MOCK project image")
    if mask:
        a, b = snapshot["assets"][ids[0]]["info"], snapshot["assets"][mask]["info"]
        if (a["width"], a["height"]) != (b["width"], b["height"]):
            raise ValueError("Mask and first reference must have identical original dimensions")
    dims = cfg.get("default_size", [768, 1024])
    width = opts.get("width") or dims[0]
    height = opts.get("height") or dims[1]
    if any(type(n) is not int or not 256 <= n <= 2048 or n % 8 for n in (width, height)) or width * height > 2097152:
        raise ValueError("Image size must be multiples of 8, 256..2048, at most 2 megapixels")
    bindings = w["bindings"]
    for field in ("width", "height", "denoise", "negative_prompt"):
        if opts.get(field) is not None and opts.get(field) != "" and field not in bindings:
            raise ValueError(f"Workflow cannot honor requested {field}")
    if not {"width", "height"} <= bindings.keys():
        raise ValueError("Image workflow must bind width and height for bounded rendering")
    shot = next((s for s in snapshot["content"]["shots"] if s["id"] == r["shot_id"]), None)
    prompt = r["prompt"] or (shot["action"] if shot else snapshot["brief"])
    seed = opts.get("seed")
    values = {"prompt": snapshot["style"] + "\n" + prompt,
              "negative_prompt": opts.get("negative_prompt", ""),
              "seed": secrets.randbits(63) if seed is None else seed,
              "width": width, "height": height}
    if opts.get("denoise") is not None:
        values["denoise"] = opts["denoise"]
    client_id = "mj_" + operation_identity[:32]
    values["filename_prefix"] = f"mj/{client_id}/image"
    cfg["workflows"] = {wid: definition}
    cfg["default_workflow"] = wid
    snapshot["comfy"] = {"workflow_id": wid, "workflow_hash": definition["sha256"],
                         "client_id": client_id, "values": values,
                         "mask_asset_id": mask, "mode": w["mode"]}


def bind(snapshot: dict, uploads: dict[str, str]) -> dict:
    c = snapshot["comfy"]
    w = snapshot["provider_config"]["workflows"][c["workflow_id"]]["manifest"]
    graph = copy.deepcopy(w["graph"])
    values = {**c["values"], **uploads}
    for field, targets in w["bindings"].items():
        if field not in values:
            continue  # e.g. keep administrator's denoise when not overridden.
        for nid, key in targets:
            graph[nid]["inputs"][key] = values[field]
    return graph
