"""Frame-accurate hard-cut renderer, independent audio bus and Chinese subtitles.

The SRT clock calculation is adapted from video-use at 9575612. See THIRD_PARTY.md.
Other code is an independent MJ implementation. Unsupported effects are rejected
by the strict Plan schema rather than being silently discarded.
"""
from pathlib import Path
import hashlib
import json
import math
import shutil
import wave
import zipfile
from .contracts import Plan, chinese_chunks
from .storage import Store, probe, run_media, ALLOWED_PROTOCOLS


def srt_time(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(cues: list[tuple], path: Path):
    path.write_text("\n".join(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{t}\n"
                              for i, (a, b, t) in enumerate(cues, 1)), encoding="utf-8")


def ffmpeg(*args):
    return run_media(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, args)], 600)


def render(plan: Plan, assets: dict, store: Store, job_id: str, mode: str, cost_snapshot=None) -> dict:
    plan.validate_timeline()
    shots = plan.shots
    if mode == "sample":
        shots = [x for x in shots if x.id in plan.sample_shots]
        positions = [plan.shots.index(x) for x in shots]
        if len(shots) < 2 or positions != list(range(min(positions), max(positions) + 1)):
            raise ValueError("样片必须选择至少两个相邻镜头。")
    frames = sum(x.frames for x in shots)
    fps, sr = plan.fps, 48000
    duration, samples = frames / fps, frames * sr // fps
    folder = store.root / "work" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    segments, cues, audio = [], [], []
    stems = []
    cursor = 0
    temporary = mode == "animatic"

    def get(asset_id, required_kind=None):
        asset = assets.get(asset_id)
        if not asset or (required_kind and asset["kind"] != required_kind):
            raise ValueError("缺少已选择的有效素材。")
        path = store.verify(asset["blob"], asset["sha256"])
        return asset, path

    for i, shot in enumerate(shots):
        aid = shot.image_id if temporary else (shot.video_id or shot.image_id)
        asset, source = get(aid)
        if not temporary:
            if asset["mock"] or not asset["reviewed"]:
                raise ValueError("正式渲染不接受模拟素材或未审核候选。")
            if shot.motion in {"subject_action", "lip_sync"} and (asset["kind"] != "video" or
                    asset["actual_motion"] not in ({"lip_sync"} if shot.motion == "lip_sync" else {"subject_action", "lip_sync"})):
                raise ValueError(f"{shot.id} 未满足人物动作合同。")
        output = folder / f"shot_{i:03}.mp4"
        filters = [f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease",
                   f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2:color=black", "setsar=1", f"fps={fps}"]
        args = ["-protocol_whitelist", ALLOWED_PROTOCOLS]
        if asset["kind"] == "image":
            if shot.source_in_frame:
                raise ValueError("静帧不允许设置视频入点。")
            args += ["-loop", "1", "-framerate", str(fps), "-i", str(source)]
            if shot.motion == "camera":
                filters += [f"zoompan=z='min(zoom+0.00035,1.06)':d=1:s={plan.width}x{plan.height}:fps={fps}"]
        elif asset["kind"] == "video":
            if asset["info"]["duration"] + 1 / fps < (shot.source_in_frame + shot.frames) / fps:
                raise ValueError(f"{shot.id} 素材时长不足，不能自动循环或定格补足。")
            args += ["-ss", str(shot.source_in_frame / fps), "-i", str(source)]
            meta = probe(source)["video"] or {}
            if meta.get("color_transfer") in {"arib-std-b67", "smpte2084"}:
                filters = ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
                           "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"] + filters
        else:
            raise ValueError("主画面必须是图片或视频。")
        if temporary:
            filters += ["drawtext=text='ANIMATIC - NOT FINAL':fontcolor=white:fontsize=24:x=20:y=20:box=1:boxcolor=black@0.7"]
        filters += ["format=yuv420p"]
        ffmpeg(*args, "-vf", ",".join(filters), "-frames:v", shot.frames, "-an", "-c:v", "libx264",
               "-preset", "ultrafast", "-crf", "22", "-threads", "2", output)
        segments.append(output)
        if shot.audio_id:
            a, _ = get(shot.audio_id, "audio")
            if not temporary and (a["mock"] or not a["reviewed"]):
                raise ValueError("正式配音尚未审核或属于模拟音频。")
            speech_samples = a["info"].get("samples", round(a["info"]["duration"] * sr))
            if speech_samples > shot.frames * sr // fps:
                raise ValueError(f"{shot.id} 配音超过镜头时长；请改稿或调整分镜。")
            n = min(speech_samples, shot.frames * sr // fps)
            audio.append({"asset_id": shot.audio_id, "lane": "voice", "start_sample": cursor * sr // fps,
                          "source_in_sample": 0, "samples": n, "gain_db": 0, "loop": False})
            if shot.narration and not plan.captions:
                chunks = chinese_chunks(shot.narration)
                count = sum(len(x) for x in chunks) or 1
                c = cursor / fps
                for text in chunks:
                    end = c + n / sr * len(text) / count
                    cues.append((c, end, text))
                    c = end
        elif shot.narration and not temporary:
            shot_a, shot_b = cursor * sr // fps, (cursor + shot.frames) * sr // fps
            full_offset = sum(x.frames for x in plan.shots[:plan.shots.index(shots[0])]) * sr // fps
            if not any(e.lane == "voice" and e.start_sample <= full_offset + shot_a
                       and e.start_sample + e.samples >= full_offset + shot_b for e in plan.audio):
                raise ValueError(f"{shot.id} 存在台词但没有配音，不能静默交付。")
        cursor += shot.frames
    offset = sum(s.frames for s in plan.shots[:plan.shots.index(shots[0])]) * sr // fps
    for event in plan.audio:
        start, end = event.start_sample, event.start_sample + event.samples
        a, b = max(start, offset), min(end, offset + samples)
        if b > a:
            e = event.model_dump()
            e.update(start_sample=a-offset, samples=b-a, source_in_sample=event.source_in_sample+a-start)
            audio.append(e)
    for cap in plan.captions:
        a, b = max(cap.start_frame / fps, offset / sr), min(cap.end_frame / fps, (offset + samples) / sr)
        if b > a:
            cues.append((a-offset/sr, b-offset/sr, cap.text))
    listing = folder / "concat.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in segments), encoding="utf-8")
    base = folder / "base.mp4"
    ffmpeg("-f", "concat", "-safe", "1", "-i", listing, "-c", "copy", base)
    # Audio is independent of visual cut boundaries; never fade continuous narration at each cut.
    inputs, parts, voice_labels, music_labels, other_labels = [], [], [], [], []
    for i, e in enumerate(audio):
        a, source = get(e["asset_id"], "audio")
        if not temporary and (a["mock"] or not a["reviewed"]):
            raise ValueError("声音轨包含模拟/未审核素材。")
        if not e["loop"] and (e["source_in_sample"] + e["samples"]) > a["info"].get("samples", round(a["info"]["duration"] * sr)):
            raise ValueError("音频使用范围超出源素材。")
        if e["loop"]:
            inputs += ["-stream_loop", "-1"]
        inputs += ["-protocol_whitelist", ALLOWED_PROTOCOLS, "-i", str(source)]
        label = f"a{i}"
        parts.append(f"[{i}:a]aresample={sr},aformat=channel_layouts=stereo,atrim=start_sample={e['source_in_sample']}:end_sample={e['source_in_sample']+e['samples']},asetpts=PTS-STARTPTS,volume={e['gain_db']}dB,adelay={e['start_sample']}S:all=1[{label}]")
        (voice_labels if e["lane"] == "voice" else music_labels if e["lane"] == "music" else other_labels).append(f"[{label}]")
        # Keep the original independent stems plus placement/gain/loop metadata in manifest.
        stem_name = f"stem_{i:03}_{e['lane']}{source.suffix}"
        shutil.copy2(source, folder / stem_name)
        stems.append({"file": stem_name, **e})
    mix = folder / "mix.wav"
    if parts:
        if plan.music_ducking and voice_labels and music_labels:
            parts.append("".join(voice_labels) + f"amix=inputs={len(voice_labels)}:normalize=0:dropout_transition=0,apad,atrim=end_sample={samples},asplit=2[voice][sidechain]")
            parts.append("".join(music_labels) + f"amix=inputs={len(music_labels)}:normalize=0:dropout_transition=0,apad,atrim=end_sample={samples}[music]")
            parts.append("[music][sidechain]sidechaincompress=threshold=0.025:ratio=6:attack=20:release=300[ducked]")
            labels = ["[voice]", "[ducked]"] + other_labels
        else:
            labels = voice_labels + music_labels + other_labels
        parts.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,alimiter=limit=0.95:latency=1,apad,atrim=end_sample={samples}[mix]")
        ffmpeg(*inputs, "-filter_complex_threads", "1", "-filter_complex", ";".join(parts), "-map", "[mix]", "-ar", sr, "-c:a", "pcm_s16le", mix)
    else:
        ffmpeg("-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=stereo", "-af", f"atrim=end_sample={samples}", "-c:a", "pcm_s16le", mix)
    clean = folder / "final_clean.mp4"
    ffmpeg("-i", base, "-i", mix, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
           "-b:a", "192k", "-movflags", "+faststart", clean)
    srt = folder / "subtitles.srt"
    write_srt(sorted(cues), srt)
    subtitled = folder / "final_subtitled.mp4"
    if cues:
        # Paths are generated by MJ; ASS style values are bounded integer contract fields.
        style = f"FontName=Noto Sans CJK SC,FontSize={plan.subtitle_size},MarginV={plan.subtitle_margin},Outline=2,Alignment=2"
        # SRT rendering's default PlayRes is not the project canvas; supply an ASS header explicitly.
        ass = folder / "subtitles.ass"
        def stamp(t):
            h, rem = divmod(int(round(t*100)), 360000); m, rem = divmod(rem, 6000); s, cs = divmod(rem, 100)
            return f"{h}:{m:02}:{s:02}.{cs:02}"
        header = f"[Script Info]\nScriptType: v4.00+\nPlayResX: {plan.width}\nPlayResY: {plan.height}\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Noto Sans CJK SC,{plan.subtitle_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,40,40,{plan.subtitle_margin},1\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        for a, b, text in sorted(cues):
            text = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", r"\N")
            header += f"Dialogue: 0,{stamp(a)},{stamp(b)},Default,,0,0,0,,{text}\n"
        ass.write_text(header, encoding="utf-8")
        escaped = str(ass).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        ffmpeg("-i", clean, "-vf", f"ass='{escaped}'", "-c:v", "libx264", "-preset", "ultrafast",
               "-threads", "2", "-crf", "22", "-c:a", "copy", subtitled)
    else:
        shutil.copy2(clean, subtitled)
    meta = probe(clean, count=True)
    actual_frames = int(meta["video"].get("nb_read_frames", 0))
    with wave.open(str(mix)) as wav:
        actual_samples = wav.getnframes()
    if actual_frames != frames or actual_samples != samples:
        raise ValueError("成片帧数/音频采样数校验失败。")
    for output in [clean, subtitled]:
        ffmpeg("-xerror", "-i", output, "-f", "null", "-")
    report = {"schema_version": "mj.render.v1", "mode": mode, "technical_pass": True,
              "creative_pass": False, "frames": actual_frames, "samples": actual_samples,
              "fps": fps, "duration": duration, "temporary": temporary,
              "caption_alignment": "manual" if plan.captions else "estimated_per_speech_segment",
              "warnings": ["创作品质必须连续观看后人工审核。", "导出配音为 AI 生成时请按发布平台要求披露。"],
              "plan": plan.model_dump(), "stems": stems,
              "assets": {aid: {"path": f"assets/{aid}{Path(a['blob']).suffix}",
                         **{k: a[k] for k in ("sha256", "kind", "info", "mock", "reviewed", "actual_motion")}}
                         for aid, a in assets.items()}, "files": {}}
    costs = {"currency": "USD", "unit": "millionth_of_usd", "items": cost_snapshot or [],
             "note": "预占/待核对不是实际账单；本地计算、电费、存储成本未计入。"}
    (folder / "costs.json").write_text(json.dumps(costs, ensure_ascii=False, indent=2), encoding="utf-8")
    for path in [clean, subtitled, srt, mix, folder / "costs.json"] + [folder / x["file"] for x in stems]:
        report["files"][path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    (folder / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    bundle = folder / "project.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename in list(report["files"]) + ["manifest.json"]:
            archive.write(folder / filename, filename)
        for asset_id, asset in assets.items():
            archive.write(store.verify(asset["blob"], asset["sha256"]), f"assets/{asset_id}{Path(asset['blob']).suffix}")
    return {"mode": mode, "frames": frames, "samples": samples, "technical_pass": True,
            "temporary": temporary, "files": list(report["files"]) + ["manifest.json", "project.zip"],
            "sha256": report["files"]["final_clean.mp4"]["sha256"], "storage_run": job_id}
