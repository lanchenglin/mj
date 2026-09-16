"""Native API contracts for Qwen images, MiniMax H3 and independent TTS.

No GPU/runtime coupling. Administrator origins/models are frozen in each task;
credentials are resolved at execution. A result is a candidate, never an approval.
Protocol sources and unsupported features: docs/10-api-first.md.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from . import api_transport as net
from .contracts import CloudImageOptions, VideoOptions, SpeechOptions, fingerprint
from .media import inspect_media, ffprobe, digest, command
from .providers import UnknownSubmission

TYPES = {"qwen_image": "image", "minimax_h3": "video", "minimax_tts": "tts"}
RATIOS = {"21:9": 21/9, "16:9": 16/9, "4:3": 4/3, "1:1": 1, "3:4": 3/4, "9:16": 9/16}
ID = r"[A-Za-z0-9_-]{1,150}"


def validate_config(cfg):
    p = net.validate_url(cfg.get("base_url", ""), cfg.get("hosts", []))
    if p.query or p.path not in ("", "/"):
        raise ValueError("Native API base_url must be an origin; routes are supplied by the adapter")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", cfg.get("key_env", "")):
        raise ValueError("Native API requires key_env, never a literal credential")
    kind = TYPES[cfg["type"]]
    models = cfg.get("models", {})
    if not models.get(kind):
        raise ValueError("Configure the native API model")
    if cfg["type"] == "minimax_h3" and models[kind] not in ("MiniMax-H3", "MiniMax-H3-Max"):
        raise ValueError("Unsupported H3 model; do not silently substitute another protocol")
    if cfg["type"] == "qwen_image" and not models[kind].startswith("qwen-image-2.0"):
        raise ValueError("This Qwen adapter targets the unified Qwen-Image-2.0 synchronous API")
    if cfg["type"] == "minimax_tts" and models[kind] not in ("speech-2.8-hd", "speech-2.8-turbo"):
        raise ValueError("This TTS adapter targets Speech 2.8")
    if not 60 <= cfg.get("remote_timeout_seconds", 1800) <= 86400:
        raise ValueError("Remote timeout must be 60..86400 seconds")
    if not 1 <= cfg.get("max_inflight", 1) <= 8:
        raise ValueError("Native API max_inflight must be 1..8")
    for group in (cfg.get("reserve_micros",{}),cfg.get("pricing",{})):
        if not isinstance(group,dict) or any(not isinstance(v,int) or isinstance(v,bool) or v<0 or v>10**12 for v in group.values()):
            raise ValueError("Reservation/rate values must be non-negative integer micro-units")
    for host in cfg.get("download_hosts", []):
        net.validate_url("https://" + host, [host])
        if "*" in host or "/" in host:
            raise ValueError("Result hosts must be exact names, not wildcard domains")


def references(req, shot, cfg):
    """Ordered logical asset references. Never accept arbitrary user URLs."""
    if cfg["type"] == "minimax_tts":
        if req.reference_ids:
            raise ValueError("Independent narration does not upload visual references")
        return []
    if cfg["type"] == "minimax_h3":
        o = req.video_options or VideoOptions()
        if req.reference_ids:
            raise ValueError("Use explicit H3 frame IDs or typed reference roles")
        first = o.first_frame_id
        if o.mode == "first_frame" and not first:
            choices = (shot or {}).get("reference_ids", [])
            if len(choices) == 1:
                first = choices[0]
        if o.mode == "text": return []
        if o.mode == "reference": return [x.asset_id for x in o.references]
        if o.mode == "last_frame": return [o.last_frame_id] if o.last_frame_id else []
        return [x for x in (first, o.last_frame_id) if x]
    o = req.cloud_image_options or CloudImageOptions()
    return list(dict.fromkeys(req.reference_ids + ((shot or {}).get("reference_ids", []) if o.use_shot_references else [])))


def prepare(snapshot, store):
    """Local-only admission: freeze all non-secret request facts before budget spend."""
    cfg, r = snapshot["provider_config"], snapshot["request"]
    validate_config(cfg)
    kind = r["kind"]
    if kind != TYPES[cfg["type"]]:
        raise ValueError("Provider does not support this operation")
    if r.get("image_options"):
        raise ValueError("Comfy workflow options cannot be sent to a cloud model")
    if r.get("cloud_image_options") and kind != "image" or r.get("video_options") and kind != "video" or r.get("speech_options") and kind != "tts":
        raise ValueError("Options do not match the native operation")
    shot = next((s for s in snapshot["content"]["shots"] if s["id"] == r["shot_id"]), None)
    refs = snapshot["reference_order"]
    assets = snapshot["assets"]
    for aid in refs:
        a = assets[aid]
        if a["review"] != "accepted" or a["mock"] or not a.get("rights", "").strip():
            raise ValueError("Only reviewed, non-MOCK, authorized references may leave MJ")
    prompt = r["prompt"] or (shot["action"] if shot else snapshot["brief"])
    # Explicit shot context improves continuity without inventing identity guarantees.
    if shot:
        prompt += "\n景别：" + shot["framing"] + "\n进入状态：" + shot["before"] + "\n结束状态：" + shot["after"]
    prompt = snapshot["style"] + "\n" + prompt
    if not prompt.strip() or len(prompt) > 6000:
        raise ValueError("Prompt must be non-empty and at most 6000 characters")
    plan = {"protocol": cfg["type"], "model": cfg["models"][kind], "references": [], "prompt": prompt}
    if kind == "image":
        o = CloudImageOptions.model_validate(r.get("cloud_image_options") or {})
        if len(prompt.encode("utf-8")) > 1300:
            raise ValueError("Qwen prompt exceeds MJ's conservative 1300 UTF-8-byte limit; shorten it explicitly to avoid provider truncation")
        if len(refs) > 3:
            raise ValueError("Qwen image editing accepts at most 3 ordered references")
        for aid in refs:
            a = assets[aid]
            if a["kind"] != "image" or a["info"]["bytes"] > 10*1024*1024:
                raise ValueError("Qwen references must be images at most 10 MiB")
        # Derive a stable seed from the operation's identity, not runtime randomness.
        seed = o.seed if o.seed is not None else int(snapshot["operation_id"][:8], 16) % 2147483648
        plan.update(parameters={"n": 1, "size": f"{o.width}*{o.height}", "seed": seed,
                                "negative_prompt": o.negative_prompt, "prompt_extend": o.prompt_extend},
                    references=[{"asset_id": i, "role": "image"} for i in refs], mode="edit" if refs else "generate")
    elif kind == "video":
        o = VideoOptions.model_validate(r.get("video_options") or {})
        if shot is None: raise ValueError("H3 needs a shot")
        required = (shot["frames"] + shot["source_in_frames"]) / snapshot["spec"]["fps"]
        lower = 5 if plan["model"] == "MiniMax-H3-Max" else 4
        duration = o.duration_seconds if o.duration_seconds is not None else max(lower, math.ceil(required))
        if not lower <= duration <= 15 or duration < required:
            raise ValueError("H3 duration must cover the shot plus source in-point within the model's 4/5..15s limits")
        allowed = ("480P", "768P") if lower == 5 else ("768P", "2K")
        if o.resolution not in allowed: raise ValueError("Resolution not supported by this H3 model")
        if r.get("sample"):
            ids=r.get("sample_shot_ids",[])
            all_shots=snapshot["content"]["shots"]
            indexes=[i for i,x in enumerate(all_shots) if x["id"] in ids]
            if not indexes or len(indexes)!=len(ids) or indexes!=list(range(indexes[0],indexes[-1]+1)) or shot["id"] not in ids:
                raise ValueError("H3 sample generation requires an explicit consecutive sample group containing this shot")
            seconds=sum(all_shots[i]["frames"] for i in indexes)/snapshot["spec"]["fps"]
            if not 10<=seconds<=20: raise ValueError("H3 representative sample group must total 10..20 seconds")
        roles = {"text": [], "first_frame": ["first_frame"], "last_frame": ["last_frame"],
                 "first_last": ["first_frame", "last_frame"], "reference": [x.role for x in o.references]}[o.mode]
        if len(refs) != len(roles) or (o.mode == "reference" and not refs):
            raise ValueError("Choose the exact frame inputs or at least one H3 reference")
        if o.mode == "first_last" and not (o.first_frame_id and o.last_frame_id):
            raise ValueError("First & last frame requires both explicit frame IDs")
        counts, durations, sizes = {}, {"video": 0, "audio": 0}, 0
        for aid, role in zip(refs, roles):
            a = assets[aid]; k = a["kind"]; info = a["info"]
            expected = "image" if role in ("first_frame", "last_frame", "reference_image") else role.removeprefix("reference_")
            counts[k] = counts.get(k, 0) + 1
            max_bytes = {"image": 30, "video": 50, "audio": 15}[expected] * 1024*1024
            if k != expected or info["bytes"] > max_bytes:
                raise ValueError("H3 reference type/size does not meet its declared role")
            if k == "image" and a["extension"] not in (".png", ".jpg", ".jpeg", ".webp"):
                raise ValueError("Unsupported H3 image format")
            if k != "audio":
                w, h = info["width"], info["height"]
                if not (256 <= w <= 5760 and 256 <= h <= 5760 and .4 <= w/h <= 2.5):
                    raise ValueError("H3 reference dimensions/aspect are out of range")
            if k in ("video", "audio"):
                d = info["duration"]; durations[k] += d
                if not 2 <= d <= 15: raise ValueError("H3 reference duration must be 2..15s")
                if a["extension"] not in ({"video": (".mp4", ".mov"), "audio": (".mp3", ".wav")}[k]):
                    raise ValueError("Unsupported H3 reference container")
                if k == "video":
                    streams = ffprobe(store.asset_path(a))["streams"]
                    v = next(x for x in streams if x["codec_type"] == "video")
                    from fractions import Fraction
                    if v["codec_name"] not in ("h264", "hevc") or not 23.976 <= float(Fraction(v["avg_frame_rate"])) <= 60:
                        raise ValueError("H3 reference video requires H264/HEVC at 23.976..60 fps")
                    if any(x.get("codec_name") not in ("aac", "mp3") for x in streams if x["codec_type"] == "audio"):
                        raise ValueError("H3 reference video's audio requires AAC or MP3")
            sizes += 4 * ((info["bytes"] + 2)//3)
            plan["references"].append({"asset_id": aid, "role": role})
        if counts.get("image", 0) > 9 or counts.get("video", 0) > 3 or counts.get("audio", 0) > 3 or any(d > 15 for d in durations.values()) or sizes > 63*1024*1024:
            raise ValueError("H3 reference count, cumulative duration or inline request size exceeds limits")
        ratio = next((name for name, val in RATIOS.items() if abs(val - snapshot["spec"]["width"]/snapshot["spec"]["height"]) < .001), None)
        if not ratio: raise ValueError("Project aspect ratio is not supported by H3")
        frame_mode = o.mode in ("first_frame", "last_frame", "first_last")
        if frame_mode:
            shapes = [(assets[i]["info"]["width"], assets[i]["info"]["height"]) for i in refs]
            if any(abs(w/h - RATIOS[ratio]) > .02 for w,h in shapes):
                raise ValueError("H3 frame inputs determine aspect ratio; prepare matching project-aspect reference frames")
        plan.update(mode=o.mode, duration=duration, resolution=o.resolution, ratio="adaptive" if frame_mode else ratio,
                    audio_policy="native_audio_muted_by_default", input_video_seconds=durations["video"])
    else:
        if shot is None or not shot["narration"].strip(): raise ValueError("Independent narration text is empty")
        o = SpeechOptions.model_validate(r.get("speech_options") or {})
        voice = o.voice_id or cfg.get("default_voice", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", voice):
            raise ValueError("Select a verified MiniMax voice ID; do not reuse an OpenAI voice name")
        if cfg.get("voices") and voice not in cfg["voices"]:
            raise ValueError("Voice ID is not on the administrator's approved voice list")
        if refs: raise ValueError("Independent TTS never uploads references")
        plan.update(text=shot["narration"], voice_setting={"voice_id": voice, "speed": o.speed, "vol": o.volume, "pitch": o.pitch},
                    pronunciation_dict={"tone": o.pronunciation}, mode="narration")
        # No cloned voice, no prompt text standing in for the approved narration.
        plan.pop("prompt")
    snapshot["api_plan"] = plan
    return plan


def minimum_reserve(cfg, plan):
    """Admin-entered conservative allowance; not an invoice or live price feed."""
    kind = TYPES[cfg["type"]]
    floor = int(cfg.get("reserve_micros", {}).get(kind, 0))
    rates = cfg.get("pricing", {})
    variable = 0
    if kind == "image": variable = int(rates.get("per_image_micros", 0))
    if kind == "video":
        variable = plan["duration"] * int(rates.get("per_output_second_micros", 0))
        variable += math.ceil(plan["input_video_seconds"] * int(rates.get("per_input_second_micros", 0)))
    if kind == "tts": variable = math.ceil(len(plan["text"]) * int(rates.get("per_1000_characters_micros", 0)) / 1000)
    return max(floor, variable)


def public_config(name, cfg):
    kind = TYPES[cfg["type"]]
    return {"id": name, "type": cfg["type"], "kinds": [kind], "models": cfg["models"],
            "currency": cfg.get("currency"), "reserve_micros": cfg.get("reserve_micros", {}),
            "default_voice": cfg.get("default_voice", ""), "voices": cfg.get("voices", []),
            "key_present": bool(os.getenv(cfg["key_env"])), "quality_verified": False}


class Native:
    def __init__(self, snapshot, store):
        self.s, self.store = snapshot, store
        self.cfg = snapshot["provider_config"]
        validate_config(self.cfg)
        self.plan = snapshot["api_plan"]
        self.kind = TYPES[self.cfg["type"]]
        key = os.getenv(self.cfg["key_env"], "")
        if not key or "\n" in key or "\r" in key:
            raise net.ApiFailure("API key environment variable is missing/invalid; no request sent")
        self.headers = {"Authorization": "Bearer " + key}

    def call(self, method, path, body=None):
        raw = net.request(method, self.cfg["base_url"].rstrip("/")+path, self.cfg["hosts"], headers=self.headers, body=body)
        try: data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise UnknownSubmission("Provider returned invalid JSON; retain reservation and reconcile") from exc
        if not isinstance(data, dict): raise UnknownSubmission("Provider response is not an object")
        if data.get("error") or data.get("code") or (data.get("base_resp") or {}).get("status_code", 0) != 0:
            raise net.ApiFailure("Provider reported an API error; inspect billing before any new attempt")
        return data

    def inline(self, aid):
        a = self.s["assets"][aid]; p = self.store.asset_path(a)
        return f"data:{a['info']['mime']};base64," + base64.b64encode(p.read_bytes()).decode()

    def payload(self):
        p = self.plan
        if self.kind == "video":
            content = [{"type": "text", "text": p["prompt"]}]
            for ref in p["references"]:
                k = self.s["assets"][ref["asset_id"]]["kind"] + "_url"
                content.append({"type": k, k: {"url": self.inline(ref["asset_id"])}, "role": ref["role"]})
            return {"model": p["model"], "content": content, "duration": p["duration"], "resolution": p["resolution"], "ratio": p["ratio"]}
        if self.kind == "image":
            content = [{"image": self.inline(x["asset_id"])} for x in p["references"]] + [{"text": p["prompt"]}]
            return {"model": p["model"], "input": {"messages": [{"role": "user", "content": content}]}, "parameters": p["parameters"]}
        body = {"model": p["model"], "text": p["text"], "stream": False, "output_format": "hex", "language_boost": "Chinese",
                "voice_setting": p["voice_setting"],
                "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1}}
        if p["pronunciation_dict"]["tone"]: body["pronunciation_dict"] = p["pronunciation_dict"]
        return body

    def submit(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        body = self.payload()
        if len(json.dumps(body, ensure_ascii=False).encode()) > 64*1024*1024:
            raise net.ApiFailure("Inline request exceeds 64 MiB; split/reduce references")
        route = {"video": "/v2/video_generation", "image": "/api/v1/services/aigc/multimodal-generation/generation", "tts": "/v1/t2a_v2"}[self.kind]
        data = self.call("POST", route, body)
        # Persist acknowledged result identity BEFORE downloading/decoding.
        try:
            if self.kind == "video":
                rid = data["task_id"]
                if not isinstance(rid, str) or not re.fullmatch(ID, rid): raise ValueError()
                return {"remote": {"protocol": "minimax_h3", "request_id": rid}}
            if self.kind == "image":
                choices = data["output"]["choices"]
                urls = [x["image"] for ch in choices for x in ch["message"]["content"] if "image" in x]
                if len(urls) != 1: raise ValueError()
                # Unknown CDN is held for administrator review, never followed blindly.
                return {"remote": {"protocol": "qwen_image", "request_id": str(data.get("request_id", "")),
                                   "result_url": urls[0], "usage": self.usage(data.get("usage", {}))}}
            d = data["data"]
            if d["status"] != 2 or not isinstance(d["audio"], str) or not re.fullmatch(r"[0-9a-fA-F]+", d["audio"]): raise ValueError()
            p = directory/"speech-source.mp3"
            p.write_bytes(bytes.fromhex(d["audio"]))
            return {"remote": {"protocol": "minimax_tts", "request_id": str(data.get("trace_id", "")),
                               "staged": str(p.relative_to(self.store.root)), "sha256": digest(p),
                               "usage": self.usage(data.get("extra_info", {}))}}
        except (KeyError, TypeError, ValueError) as exc:
            raise UnknownSubmission("Success response lacks a usable result identity; no automatic regeneration") from exc

    @staticmethod
    def usage(data):
        keys = {"input_tokens", "output_tokens", "characters", "image_count", "width", "height", "total_seconds", "input_seconds", "output_seconds", "input_image_count", "input_audio_seconds", "total_tokens", "prompt_tokens", "completion_tokens", "usage_characters", "word_count", "audio_length"}
        return {k: v for k, v in data.items() if k in keys and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0}

    def poll(self, remote, directory):
        if remote.get("protocol") != self.cfg["type"]:
            raise UnknownSubmission("Remote identity protocol mismatch")
        directory.mkdir(parents=True, exist_ok=True)
        usage = remote.get("usage", {})
        if self.kind == "video":
            rid = remote.get("request_id", "")
            if not re.fullmatch(ID, rid): raise UnknownSubmission("Invalid original H3 task ID")
            data = self.call("GET", "/v2/query/video_generation/"+rid)
            task = data.get("task", {})
            if task.get("id") != rid or task.get("model") != self.plan["model"]:
                raise UnknownSubmission("Query response does not match the original H3 task/model")
            status = task.get("status")
            if status in ("queued", "running"):
                return {"pending": True, "progress": {"stage": status}}
            if status in ("failed", "cancelled"):
                raise net.ApiFailure("H3 task ended without usable output; fee remains pending reconciliation")
            if status != "succeeded": raise UnknownSubmission("Unknown H3 task status; never create a replacement automatically")
            url = task.get("content", {}).get("url")
            usage = self.usage(task.get("usage", {}))
        elif self.kind == "image":
            url = remote.get("result_url")
        else:
            p = (self.store.root/remote.get("staged", "")).resolve()
            if not p.is_relative_to((self.store.root/"jobs").resolve()) or not p.is_file() or digest(p) != remote.get("sha256"):
                raise UnknownSubmission("Staged speech missing or changed; do not call TTS again automatically")
            inspect_media(p)
            out = directory/"speech.wav"
            command(["ffmpeg", "-y", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(p), "-vn", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(out)], 120)
            return self.result(out, usage, remote)
        out = directory/("video.mp4" if self.kind == "video" else "image.png")
        try:
            net.validate_url(url or "", self.cfg.get("download_hosts", []))
        except Exception as exc:
            raise UnknownSubmission("Result CDN is not approved; retain original result and ask administrator to review download_hosts") from exc
        net.request("GET", url, self.cfg.get("download_hosts", []), limit=200*1024*1024 if self.kind == "video" else 32*1024*1024, destination=out)
        return self.result(out, usage, remote)

    def result(self, path, usage, remote):
        info = inspect_media(path)
        expected = "audio" if self.kind == "tts" else self.kind
        if info["kind"] != expected: raise net.ApiFailure("API result has the wrong media type")
        if self.kind == "video" and info["duration"] + .05 < self.plan["duration"]:
            raise net.ApiFailure("H3 returned a shorter clip than requested; review it before any paid regeneration")
        return {"path": path, "mock": False, "motion": "none", "usage": usage,
                "provenance": {"adapter": self.cfg["type"], "model": self.plan["model"],
                               "request_id": remote.get("request_id", ""), "plan_hash": fingerprint(self.plan),
                               "mode": self.plan["mode"], "usage": usage, "invoice_verified": False,
                               "native_audio_default": "mute" if self.kind == "video" else None,
                               "narration_hash": fingerprint(self.plan["text"]) if self.kind == "tts" else None}}
