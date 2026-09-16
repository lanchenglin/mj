"""Self-hosted ComfyUI image adapter. Protocol only; no ComfyUI/GPL code copied.

POST /prompt happens at most once per MJ task. Lost acknowledgements are searched
by a durable client marker in queue/history; an empty search is NOT a safe retry.
"""
from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path

import httpx
from PIL import Image, ImageOps

from .comfy_transport import Client, ComfyFailure
from .comfy_workflows import bind, check_workflow
from .contracts import fingerprint
from .providers import UnknownSubmission


class ComfyUI:
    def __init__(self, snapshot: dict, store):
        self.s, self.store = snapshot, store
        self.cfg, self.c = snapshot["provider_config"], snapshot["comfy"]
        self.client = Client(self.cfg)
        self.w = self.cfg["workflows"][self.c["workflow_id"]]["manifest"]
        check_workflow(self.w, set(self.cfg["allowed_nodes"]))
        if fingerprint(self.w) != self.c["workflow_hash"]:
            raise ComfyFailure("Frozen workflow hash mismatch")

    def get_json(self, route: str, **kwargs):
        try:
            value = json.loads(self.client.request("GET", route, **kwargs))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ComfyFailure("Comfy returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise ComfyFailure("Comfy JSON response must be an object")
        return value

    def preflight(self) -> dict:
        """GET-only capability check, before any reference upload or generation."""
        objects = self.get_json("/object_info")
        missing = sorted({n["class_type"] for n in self.w["graph"].values()} - objects.keys())
        if missing:
            raise ComfyFailure("Comfy workflow has missing node classes: " + ", ".join(missing))
        for node in self.w["graph"].values():
            definition = objects[node["class_type"]]
            fields = {**definition.get("input", {}).get("required", {}),
                      **definition.get("input", {}).get("optional", {})}
            for field, value in node["inputs"].items():
                kind = fields.get(field)
                # Check models/checkpoints/samplers when node metadata exposes a combo.
                if isinstance(value, str) and kind and isinstance(kind[0], list) and value not in kind[0]:
                    if node["class_type"] not in ("LoadImage", "LoadImageMask"):
                        raise ComfyFailure(f"Comfy node {node['class_type']} has unavailable option/model: {field}")
        return {"nodes_present": True, "workflow_hash": self.c["workflow_hash"],
                "quality_verified": False, "generation_calls": 0}

    def _upload(self, aid: str, index: str) -> str:
        a = self.s["assets"][aid]
        source = self.store.asset_path(a)
        if source.stat().st_size > 16 * 1024 * 1024:
            raise ComfyFailure("Reference exceeds 16 MiB")
        with Image.open(source) as im:
            if im.width * im.height > 16777216 or getattr(im, "n_frames", 1) != 1:
                raise ComfyFailure("Reference must be a single image at most 16 megapixels")
            im = ImageOps.exif_transpose(im)
            if index == "mask":
                if im.mode in ("RGBA", "LA") and im.getchannel("A").getextrema() != (255, 255):
                    raise ComfyFailure("Use an opaque black/white mask; transparent masks are ambiguous")
                im = im.convert("L")
            else:
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG")  # strips source metadata; originals remain untouched.
        if buf.tell() > 16 * 1024 * 1024:
            raise ComfyFailure("Normalized reference exceeds 16 MiB")
        name = f"{index}-{a['blob_hash'][:16]}.png"
        subfolder = "mj/" + self.c["client_id"]
        data = self.client.request("POST", "/upload/image", data={"type": "input", "subfolder": subfolder, "overwrite": "false"},
                                   files={"image": (name, buf.getvalue(), "image/png")})
        result = json.loads(data)
        if result.get("type") != "input" or result.get("subfolder") != subfolder or result.get("name") != name:
            raise ComfyFailure("Comfy upload returned an unexpected storage name; no prompt submitted")
        return subfolder + "/" + name

    def submit(self, directory: Path) -> dict:
        try:
            self.preflight()
            uploads = {f"ref_{i}": self._upload(aid, f"ref_{i}") for i, aid in enumerate(self.s["reference_order"])}
            if self.c.get("mask_asset_id"):
                uploads["mask"] = self._upload(self.c["mask_asset_id"], "mask")
            graph = bind(self.s, uploads)
        except Exception as exc:
            # No generation POST has occurred in this block, though uploads may exist.
            raise ComfyFailure("Comfy preparation failed before generation: " + (str(exc) if isinstance(exc, ComfyFailure) else type(exc).__name__)) from exc
        payload = {"prompt": graph, "client_id": self.c["client_id"],
                   "extra_data": {"mj_client_id": self.c["client_id"], "mj_workflow_hash": self.c["workflow_hash"]}}
        try:
            result = json.loads(self.client.request("POST", "/prompt", json=payload))
            rid = result.get("prompt_id")
            if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", rid):
                raise UnknownSubmission("Comfy submission acknowledgement missing; reconcile, never resubmit")
            return {"remote": {"request_id": rid, "client_id": self.c["client_id"], "submitted_at": time.time()}}
        except ComfyFailure as exc:
            if getattr(exc, "status_code", 0) in (400, 401, 403, 404, 413, 422, 429):
                raise
            raise UnknownSubmission("Comfy may have accepted the workflow; reconcile before any new generation") from exc
        except (httpx.TransportError, ValueError, AttributeError) as exc:
            raise UnknownSubmission("Comfy submission response unknown; reconcile the original task") from exc

    def _owned(self, entry) -> bool:
        if not isinstance(entry, list) or len(entry) < 4 or not isinstance(entry[3], dict):
            return False
        extra = entry[3]
        return extra.get("mj_client_id") == self.c["client_id"] and extra.get("mj_workflow_hash") == self.c["workflow_hash"]

    def lookup(self) -> dict:
        queue = self.get_json("/queue")
        found = set()
        for state in ("queue_running", "queue_pending"):
            for entry in queue.get(state, []):
                if self._owned(entry):
                    found.add(entry[1])
        history = self.get_json("/history", params={"max_items": 1000})
        for rid, item in history.items():
            if isinstance(item, dict) and self._owned(item.get("prompt")):
                found.add(rid)
        if len(found) != 1:
            raise UnknownSubmission("No unique original Comfy task found (history may have expired); manual verification required, not safe to resubmit")
        rid = found.pop()
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", rid):
            raise UnknownSubmission("Comfy returned invalid original task identity")
        return {"remote": {"request_id": rid, "client_id": self.c["client_id"], "recovered": True, "recovered_at": time.time()}}

    def _pending(self, remote: dict, phase: str) -> dict:
        started = remote.get("submitted_at", remote.get("recovered_at", 0))
        if started and time.time() - started > self.cfg.get("remote_timeout_seconds", 1800):
            raise UnknownSubmission("Comfy remote wait exceeded limit; task may still be running. Reconcile without resubmitting")
        return {"pending": True, "progress": {"phase": phase}}

    def poll(self, remote: dict, directory: Path) -> dict:
        if remote.get("lookup"):
            return self.lookup()
        rid = remote.get("request_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", rid) or remote.get("client_id") != self.c["client_id"]:
            raise ComfyFailure("Invalid frozen Comfy task identity")
        history = self.get_json("/history/" + rid)
        item = history.get(rid)
        if not item:
            queue = self.get_json("/queue")
            for state in ("queue_running", "queue_pending"):
                if any(isinstance(e, list) and len(e) > 1 and e[1] == rid and self._owned(e) for e in queue.get(state, [])):
                    return self._pending(remote, "running" if state == "queue_running" else "queued")
            raise UnknownSubmission("Comfy task is absent from queue/history; GPU restart or history cleanup requires reconciliation")
        if not isinstance(item, dict) or not self._owned(item.get("prompt")):
            raise UnknownSubmission("Comfy history identity mismatch; result not imported")
        status = item.get("status", {})
        if status.get("status_str") in ("error", "failed"):
            raise ComfyFailure("Comfy workflow execution failed; inspect GPU logs, references and model configuration")
        if status.get("completed") is not True or status.get("status_str") != "success":
            return self._pending(remote, "running")
        outputs = item.get("outputs", {}).get(self.w["output_node"], {}).get("images", [])
        if len(outputs) != 1:
            raise ComfyFailure("Expected exactly one image at the declared SaveImage output")
        image = outputs[0]
        name, folder = image.get("filename", ""), image.get("subfolder", "")
        if image.get("type") != "output" or folder != "mj/" + self.c["client_id"] or not re.fullmatch(r"image_[A-Za-z0-9_-]+\.(png|jpg|jpeg|webp)", name):
            raise ComfyFailure("Comfy output is outside this task's isolated output prefix")
        raw = self.client.request("GET", "/view", params={"filename": name, "subfolder": folder, "type": "output"})
        directory.mkdir(parents=True, exist_ok=True)
        tmp, target = directory / "comfy.download", directory / "comfy.png"
        tmp.write_bytes(raw)
        try:
            with Image.open(tmp) as im:
                if im.format not in ("PNG", "JPEG", "WEBP") or getattr(im, "n_frames", 1) != 1:
                    raise ComfyFailure("Comfy returned non-image or animated output")
                if (im.width, im.height) != (self.c["values"]["width"], self.c["values"]["height"]):
                    raise ComfyFailure("Comfy output dimensions do not match the frozen request")
                im.load()
                im.convert("RGB").save(target, "PNG")
        finally:
            tmp.unlink(missing_ok=True)
        return {"path": target, "mock": False, "motion": "none", "provenance": {
            "engine": "comfyui", "workflow_id": self.c["workflow_id"], "workflow_hash": self.c["workflow_hash"],
            "request_id": rid, "seed": self.c["values"]["seed"], "mode": self.c["mode"],
            "reference_hashes": [self.s["assets"][i]["blob_hash"] for i in self.s["reference_order"]],
            "mask_hash": self.s["assets"][self.c["mask_asset_id"]]["blob_hash"] if self.c["mask_asset_id"] else None,
            "compute_cost": "unmetered; no per-image API invoice inferred", "creative_review": "pending"}}


def check_server(cfg: dict) -> dict:
    """No generation, no reference upload, no automatic installation."""
    results = []
    try:
        raw = Client(cfg).request("GET", "/system_stats")
        if not isinstance(json.loads(raw), dict):
            raise ComfyFailure("Invalid Comfy system response")
        for wid, definition in cfg["workflows"].items():
            snapshot = {"provider_config": cfg, "comfy": {"workflow_id": wid, "workflow_hash": definition["sha256"]}}
            try:
                result = ComfyUI(snapshot, None).preflight()
                results.append({"workflow": wid, "ok": True, **result})
            except Exception as exc:
                results.append({"workflow": wid, "ok": False, "error": str(exc) if isinstance(exc, ComfyFailure) else type(exc).__name__})
        return {"ok": all(x["ok"] for x in results), "workflows": results, "generation_calls": 0, "quality_verified": False}
    except Exception as exc:
        return {"ok": False, "error": str(exc) if isinstance(exc, ComfyFailure) else type(exc).__name__, "generation_calls": 0, "quality_verified": False}
