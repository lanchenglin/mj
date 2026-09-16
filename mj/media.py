"""Local-only media ingestion and frame/sample-accurate rendering.

No model-authored shell/filter expressions or remote FFmpeg inputs are accepted.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import wave
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from .contracts import Spec, Workspace, subtitle_chunks

MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".mkv": "video/x-matroska", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}


def command(args: list[str], timeout: int = 1800) -> str:
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            import signal
            os.killpg(p.pid, signal.SIGKILL)
        else:
            p.kill()
        p.communicate()
        raise ValueError("Media process timed out") from None
    if p.returncode:
        raise ValueError("Media operation failed: " + err.decode("utf-8", "replace")[-1000:])
    return out.decode("utf-8", "replace")


def ffprobe(path: Path, count: bool = False) -> dict:
    args = ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe"]
    if count:
        args += ["-count_frames"]
    args += ["-show_streams", "-show_format", "-of", "json", str(path)]
    return json.loads(command(args, 300))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_media(path: Path) -> dict:
    ext = path.suffix.lower()
    if ext not in MIMES or not path.stat().st_size:
        raise ValueError("Unsupported extension or empty media")
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        with Image.open(path) as image:
            if image.width * image.height > 20_000_000:
                raise ValueError("Image exceeds 20 megapixels")
            image.load()
            expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}[ext]
            if image.format != expected:
                raise ValueError("Image bytes do not match extension")
            return {"kind": "image", "width": image.width, "height": image.height, "mime": MIMES[ext]}
    data = ffprobe(path)
    streams = data.get("streams", [])
    video = next((s for s in streams if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or not 0 < duration <= 1800:
        raise ValueError("Media duration must be between 0 and 1800 seconds")
    if video and video.get("width", 0) * video.get("height", 0) > 34_000_000:
        raise ValueError("Video resolution too large")
    if not video and not audio:
        raise ValueError("No audio or video stream")
    # Decode the entire file, not just the header. Uploaded playlists are not accepted.
    command(["ffmpeg", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(path), "-f", "null", "-"], 300)
    return {"kind": "video" if video else "audio", "duration": duration,
            "width": video.get("width") if video else None,
            "height": video.get("height") if video else None,
            "audio": bool(audio), "sample_rate": int(audio.get("sample_rate", 0)) if audio else 0,
            "mime": MIMES[ext]}


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)

    def path(self, blob_hash: str, extension: str) -> Path:
        import re
        if not re.fullmatch(r"[0-9a-f]{64}", blob_hash) or extension not in MIMES:
            raise ValueError("Invalid storage key")
        path = self.root / "blobs" / blob_hash[:2] / (blob_hash + extension)
        if not path.resolve().is_relative_to((self.root / "blobs").resolve()):
            raise ValueError("Storage path escape")
        return path

    def ingest(self, source: Path) -> tuple[str, str, dict]:
        info = inspect_media(source)
        h, ext = digest(source), source.suffix.lower()
        dest = self.path(h, ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix="upload-")
        os.close(fd)
        try:
            shutil.copyfile(source, tmp)
            if digest(Path(tmp)) != h:
                raise ValueError("Source changed while ingesting")
            os.replace(tmp, dest)
        finally:
            Path(tmp).unlink(missing_ok=True)
        info["bytes"] = dest.stat().st_size
        return h, ext, info

    def asset_path(self, asset: dict) -> Path:
        path = self.path(asset["blob_hash"], asset["extension"])
        if not path.is_file() or digest(path) != asset["blob_hash"]:
            raise ValueError("Asset missing or hash mismatch")
        return path


def font_path() -> str | None:
    result = subprocess.run(["fc-match", "-f", "%{file}", "Noto Sans CJK SC"], capture_output=True, text=True)
    return result.stdout.strip() or None


def placeholder(path: Path, title: str, subtitle: str, width=720, height=1280):
    image = Image.new("RGB", (width, height), "#151c2c")
    draw = ImageDraw.Draw(image)
    fp = font_path()
    font = ImageFont.truetype(fp, max(20, width // 24)) if fp else ImageFont.load_default()
    draw.rounded_rectangle((width*.08, height*.18, width*.92, height*.78), radius=30, outline="#bb97ef", width=3)
    lines = ["MJ · 工程预演 / MOCK", "", *subtitle_chunks(title, 16), "", *subtitle_chunks(subtitle, 16), "", "不是 AI 生成的正式漫剧"]
    draw.multiline_text((width*.13, height*.25), "\n".join(lines), fill="#efe5ff", font=font, spacing=14)
    image.save(path)


def srt_time(seconds: float):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def audio_bus(events: list[dict], store: Store, assets: dict, out: Path, samples: int, timeout: int):
    args = ["ffmpeg", "-y", "-v", "error", "-filter_complex_threads", "1", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    filters, names = [f"[0:a]atrim=end_sample={samples}[silence]"], ["[silence]"]
    for i, ev in enumerate(events, 1):
        a = assets[ev["asset_id"]]
        p = store.asset_path(a)
        source_end = ev["source_in_sample"] + ev["samples"]
        if source_end > round(a["info"]["duration"] * 48000) + 2400:
            raise ValueError("Audio source range exceeds measured duration")
        if ev["start_sample"] + ev["samples"] > samples:
            raise ValueError("Audio exceeds timeline; edit it explicitly before render")
        args += ["-protocol_whitelist", "file,pipe", "-i", str(p)]
        fade = min(ev.get("fade_samples", 0), ev["samples"] // 2)
        f = f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=start_sample={ev['source_in_sample']}:end_sample={source_end},asetpts=PTS-STARTPTS,volume={ev['gain']}"
        if fade:
            f += f",afade=t=in:ss=0:ns={fade},afade=t=out:ss={ev['samples']-fade}:ns={fade}"
        f += f",adelay={ev['start_sample']}S:all=1[a{i}]"
        filters.append(f)
        names.append(f"[a{i}]")
    filters.append("".join(names) + f"amix=inputs={len(names)}:duration=first:normalize=0,atrim=end_sample={samples}[out]")
    args += ["-filter_complex", ";".join(filters), "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "48000", str(out)]
    command(args, timeout)


def render(snapshot: dict, store: Store, directory: Path, timeout: int = 1800) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    ws = Workspace.model_validate(snapshot["content"])
    spec = Spec.model_validate(snapshot["spec"])
    params = snapshot["request"]
    animatic = params["kind"] == "animatic"
    assets = snapshot["assets"]
    shots = ws.shots
    if not shots or sum(s.frames for s in shots) != spec.total_frames:
        raise ValueError("Shot lengths must cover exactly the project timeline")
    sample_offset = 0
    if params.get("sample"):
        ids = params.get("sample_shot_ids", [])
        indexes = [i for i,s in enumerate(shots) if s.id in ids]
        if not indexes or len(indexes) != len(ids) or indexes != list(range(indexes[0], indexes[-1]+1)):
            raise ValueError("Sample must use consecutive, existing shots")
        sample_offset = sum(s.frames for s in shots[:indexes[0]])
        shots = [shots[i] for i in indexes]
        spec.total_frames = sum(s.frames for s in shots)
        if not 10 <= spec.total_frames/spec.fps <= 20:
            raise ValueError("Representative sample must be 10–20 seconds")
    samples = round(spec.total_frames * 48000 / spec.fps)
    offset, clips, audio_events, captions, any_mock = 0, [], [], [], animatic
    for n, shot in enumerate(shots):
        target = directory / f"shot-{n:03}.mp4"
        asset = assets.get(shot.video_asset)
        if asset is None and animatic:
            asset = next((assets.get(a) for a in shot.reference_ids if assets.get(a, {}).get("kind") == "image"), None)
        if asset is None:
            if not animatic:
                raise ValueError(f"{shot.id}: no selected visual")
            source = directory / f"card-{n:03}.png"
            placeholder(source, shot.id, shot.action, spec.width, spec.height)
            kind = "image"
        else:
            source = store.asset_path(asset)
            kind = asset["kind"]
            any_mock |= asset["mock"]
            if not animatic and asset["review"] != "accepted":
                raise ValueError(f"{shot.id}: visual not reviewed")
            if shot.motion in ("subject_action", "lip_sync") and not animatic:
                if kind != "video" or asset["mock"] or asset["motion"] != shot.motion:
                    raise ValueError(f"{shot.id}: candidate does not satisfy action contract")
        if kind not in ("image", "video"):
            raise ValueError("Selected visual must be image or video")
        args = ["ffmpeg", "-y", "-v", "error", "-threads", "2", "-filter_threads", "1", "-protocol_whitelist", "file,pipe"]
        if kind == "image":
            args += ["-loop", "1", "-framerate", str(spec.fps)]
        else:
            required = (shot.source_in_frames + shot.frames) / spec.fps
            if asset["info"]["duration"] + 1/spec.fps < required:
                raise ValueError(f"{shot.id}: selected video is too short")
            args += ["-ss", str(shot.source_in_frames / spec.fps)]
        args += ["-i", str(source)]
        if shot.fit == "cover":
            vf = f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=increase,crop={spec.width}:{spec.height}"
        else:
            vf = f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=decrease,pad={spec.width}:{spec.height}:(ow-iw)/2:(oh-ih)/2"
        if kind == "video":
            stream = next(s for s in ffprobe(source)["streams"] if s["codec_type"] == "video")
            if stream.get("color_transfer") in ("smpte2084", "arib-std-b67"):
                vf = "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p," + vf
        vf += f",setsar=1,fps={spec.fps},settb=expr=1/{spec.fps},setpts=N"
        if kind == "image" and shot.motion == "camera":
            vf += f",zoompan=z='min(1.05,1+on*0.00015)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={spec.width}x{spec.height}:fps={spec.fps}"
        if animatic or (asset and asset["mock"]):
            vf += ",drawtext=text='MJ MOCK / PREVIEW ONLY':fontcolor=white:fontsize=20:x=12:y=12:box=1:boxcolor=black@0.6"
        args += ["-vf", vf, "-frames:v", str(shot.frames), "-r", str(spec.fps), "-fps_mode", "cfr", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "21", "-pix_fmt", "yuv420p", str(target)]
        command(args, timeout)
        clips.append(target)
        cap_duration = shot.frames/spec.fps
        if shot.audio_asset:
            a = assets[shot.audio_asset]
            if a["kind"] != "audio" or (not animatic and a["review"] != "accepted"):
                raise ValueError("Speech must be reviewed audio")
            any_mock |= a["mock"]
            cap_duration = a["info"]["duration"]
            if cap_duration > shot.frames/spec.fps + 0.02:
                raise ValueError(f"{shot.id}: narration is longer than shot; revise timing")
            audio_events.append(dict(asset_id=shot.audio_asset, start_sample=round(offset*48000/spec.fps), source_in_sample=0, samples=min(round(cap_duration*48000), round(shot.frames*48000/spec.fps)), gain=1, bus="speech", fade_samples=0))
        elif shot.narration and not animatic:
            raise ValueError(f"{shot.id}: narration has no audio asset")
        chunks = subtitle_chunks(shot.narration, ws.subtitles.line_chars)
        chars = max(1, sum(len(c) for c in chunks))
        local = offset/spec.fps
        for text in chunks:
            end = local + cap_duration * len(text)/chars
            captions.append((local, end, text))
            local = end
        offset += shot.frames
    for ev in ws.extra_audio:
        item = ev.model_dump()
        if params.get("sample"):
            start = item["start_sample"] - round(sample_offset * 48000/spec.fps)
            end = start + item["samples"]
            if end <= 0 or start >= samples:
                continue
            item["source_in_sample"] += max(0, -start)
            item["start_sample"] = max(0, start)
            item["samples"] = min(samples, end) - item["start_sample"]
        a = assets[item["asset_id"]]
        if a["kind"] != "audio" or (not animatic and a["review"] != "accepted"):
            raise ValueError("Extra audio must be reviewed audio")
        any_mock |= a["mock"]
        audio_events.append(item)
    concat = directory / "concat.txt"
    concat.write_text("".join(f"file '{p.name}'\n" for p in clips), encoding="utf-8")
    visual = directory / "visual.mp4"
    command(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "1", "-i", str(concat), "-c", "copy", str(visual)], timeout)
    for bus in ("speech", "music", "sfx"):
        audio_bus([e for e in audio_events if e["bus"] == bus], store, assets, directory/f"{bus}.wav", samples, timeout)
    has_speech = any(e["bus"] == "speech" for e in audio_events)
    if ws.duck_music and has_speech:
        graph = "[0:a]asplit=2[voice][control];[1:a][control]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=250[music];[voice][music][2:a]amix=inputs=3:normalize=0"
    else:
        graph = "[0:a][1:a][2:a]amix=inputs=3:normalize=0"
    graph += f",alimiter=limit=0.95:latency=1,atrim=end_sample={samples}[mix]"
    mixed = directory/"mix.wav"
    command(["ffmpeg", "-y", "-v", "error", "-i", str(directory/"speech.wav"), "-i", str(directory/"music.wav"), "-i", str(directory/"sfx.wav"), "-filter_complex", graph, "-map", "[mix]", "-c:a", "pcm_s16le", str(mixed)], timeout)
    clean = directory/"final_clean.mp4"
    command(["ffmpeg", "-y", "-v", "error", "-i", str(visual), "-i", str(mixed), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(clean)], timeout)
    srt = directory/"captions.srt"
    srt.write_text("\n".join(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{t}\n" for i,(a,b,t) in enumerate(captions,1)), encoding="utf-8")
    final = directory/"final_subtitled.mp4"
    if captions:
        # All paths are server-generated, never source filenames or user paths.
        subtitle_path = str(srt.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        style = f"FontName=Noto Sans CJK SC,FontSize={round(ws.subtitles.font_size*288/spec.height)},MarginV={round(ws.subtitles.margin_v*288/spec.height)},Outline=2,Alignment=2"
        command(["ffmpeg", "-y", "-v", "error", "-filter_threads", "1", "-i", str(clean), "-vf", f"subtitles='{subtitle_path}':force_style='{style}'", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "21", "-threads", "2", "-c:a", "copy", str(final)], timeout)
    else:
        shutil.copyfile(clean, final)
    probe = ffprobe(final, count=True)
    video_stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    frames = int(video_stream["nb_read_frames"])
    measured_video_duration = float(video_stream["duration"])
    if abs(measured_video_duration - spec.total_frames/spec.fps) > 0.5/spec.fps:
        raise ValueError("Video timestamps do not cover the planned timeline")
    with wave.open(str(mixed)) as wav:
        measured_samples = wav.getnframes()
    if frames != spec.total_frames or measured_samples != samples:
        raise ValueError(f"Rendered frame/sample count mismatch: frames={frames}/{spec.total_frames}, samples={measured_samples}/{samples}")
    command(["ffmpeg", "-v", "error", "-xerror", "-i", str(final), "-f", "null", "-"], timeout)
    qa = {"technical_pass": True, "frames": frames, "samples": measured_samples, "fps": spec.fps,
          "duration_seconds": frames/spec.fps, "measured_video_duration_seconds": measured_video_duration, "mock": bool(any_mock), "creative_review": "pending",
          "subtitle_timing": "measured-audio-duration-proportional; human review required", "real_model_quality_verified": False}
    timeline = {"schema_version": "mj.timeline.v1", "spec": spec.model_dump(), "shots": [s.model_dump() for s in shots], "audio": audio_events, "revision": snapshot["revision"]}
    for name, data in (("qa.json",qa),("timeline.json",timeline),("workspace.json",snapshot["content"])):
        (directory/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"directory": directory.name, "qa": qa, "files": ["final_clean.mp4", "final_subtitled.mp4", "captions.srt", "speech.wav", "music.wav", "sfx.wav", "mix.wav", "qa.json", "timeline.json", "workspace.json"] + [p.name for p in sorted(directory.glob("card-*.png"))], "mock": bool(any_mock)}


def export_bundle(directory: Path, result: dict, cost: dict, assets: dict, store: Store) -> Path:
    """Export only explicit deliverables and referenced CAS media; never .env or raw provider data."""
    dest = directory/"project.zip"
    files = {name: digest(directory/name) for name in result["files"]}
    manifest = {"schema_version":"mj.release.v1", "files":files,"assets":[],"cost":cost,"mock":result["mock"]}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for name in files:
            z.write(directory/name, name)
        for aid, a in assets.items():
            key = f"assets/{aid}{a['extension']}"
            z.write(store.asset_path(a), key)
            manifest["assets"].append({"id":aid,"path":key,"sha256":a["blob_hash"],"mock":a["mock"]})
        z.writestr("manifest.json", json.dumps(manifest,ensure_ascii=False,indent=2))
    return dest
