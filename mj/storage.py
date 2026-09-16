"""Content-addressed, validated media. No user-provided filesystem paths or URLs."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import signal
import time
from contextvars import ContextVar
import tempfile
from PIL import Image, UnidentifiedImageError

ALLOWED_PROTOCOLS = "file,pipe"


media_cancel = ContextVar("media_cancel", default=lambda: False)


def run_media(args: list[str], timeout: int = 180):
    """Bound both wall time and subprocess lifetime, including children."""
    child_env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "FONTCONFIG_PATH", "TMPDIR", "SYSTEMROOT") if k in os.environ}
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, env=child_env)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if media_cancel.get()():
                raise ValueError("媒体任务已取消或执行租约失效。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("媒体处理超时；已终止本次进程组。")
            try:
                out, err = proc.communicate(timeout=min(1, remaining))
                if proc.returncode:
                    raise ValueError("媒体无法解码，或不支持此编码/滤镜。")
                return subprocess.CompletedProcess(args, proc.returncode, out, err)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            if os.name == "posix":
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            else:
                proc.kill()
            proc.communicate()


def probe(path: Path, count=False) -> dict:
    args = ["ffprobe", "-v", "error", "-protocol_whitelist", ALLOWED_PROTOCOLS]
    if count:
        args += ["-count_frames"]
    args += ["-show_streams", "-show_format", "-of", "json", str(path)]
    result = json.loads(run_media(args, 60).stdout)
    video = next((s for s in result.get("streams", []) if s["codec_type"] == "video"), None)
    audio = next((s for s in result.get("streams", []) if s["codec_type"] == "audio"), None)
    return {"duration": float(result.get("format", {}).get("duration", 0)),
            "video": video, "audio": audio}


class Store:
    def __init__(self, settings):
        self.root = settings.data_dir
        self.max_bytes = settings.max_upload_bytes
        (self.root / "blobs").mkdir(exist_ok=True)
        (self.root / "work").mkdir(exist_ok=True)

    def path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("素材不存在或存储路径无效。")
        return path

    def ingest(self, data: bytes) -> dict:
        if not data or len(data) > self.max_bytes:
            raise ValueError("素材为空或超过上传限制。")
        with tempfile.TemporaryDirectory(dir=self.root / "work") as folder:
            tmp = Path(folder) / "input"
            tmp.write_bytes(data)
            try:
                with Image.open(tmp) as image:
                    if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 24_000_000:
                        raise ValueError("仅允许不超过2400万像素的 PNG/JPEG/WebP。")
                    image.verify()
                with Image.open(tmp) as image:
                    image.load()
                    info = {"width": image.width, "height": image.height}
                    suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image.format]
                kind = "image"
            except UnidentifiedImageError:
                # Strict container signatures exclude playlists/protocols and arbitrary documents.
                if not (data[:4] in {b"RIFF", b"fLaC", b"OggS"} or data[:3] == b"ID3"
                        or data[4:8] == b"ftyp" or data[:4] == bytes.fromhex("1a45dfa3")
                        or (len(data) > 2 and data[0] == 255 and data[1] & 224 == 224)):
                    raise ValueError("不支持的媒体格式。")
                p = probe(tmp)
                if not (p["video"] or p["audio"]) or not 0 < p["duration"] <= 1800:
                    raise ValueError("素材必须包含音频/视频且长度不超过30分钟。")
                v = p["video"]
                if v and (v["width"] * v["height"] > 3840 * 2160 or v["width"] > 4096 or v["height"] > 4096):
                    raise ValueError("视频尺寸超出处理上限。")
                run_media(["ffmpeg", "-v", "error", "-xerror", "-protocol_whitelist", ALLOWED_PROTOCOLS,
                           "-i", str(tmp), "-f", "null", "-"], 180)
                kind = "video" if v else "audio"
                suffix = (".webm" if data[:4] == bytes.fromhex("1a45dfa3") else ".mp4") if v else (
                    ".wav" if data[:4] == b"RIFF" else ".flac" if data[:4] == b"fLaC" else
                    ".ogg" if data[:4] == b"OggS" else ".m4a" if data[4:8] == b"ftyp" else ".mp3")
                info = {"duration": float((v or {}).get("duration") or p["duration"]), "width": v["width"] if v else None,
                        "height": v["height"] if v else None, "has_audio": bool(p["audio"])}
                if kind == "audio":
                    # Actual decoded/resampled content, not rounded container duration or AAC padding.
                    pcm = Path(folder) / "measured.wav"
                    run_media(["ffmpeg", "-v", "error", "-protocol_whitelist", ALLOWED_PROTOCOLS,
                               "-i", str(tmp), "-vn", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(pcm)])
                    import wave
                    with wave.open(str(pcm)) as wav:
                        info["samples"] = wav.getnframes()
                    info["duration"] = info["samples"] / 48000
            h = hashlib.sha256(data).hexdigest()
            key = f"blobs/{h}{suffix}"
            destination = self.root / key
            if not destination.exists():
                os.replace(tmp, destination)
            return {"kind": kind, "blob": key, "sha256": h, "info": info}

    def verify(self, key: str, sha: str) -> Path:
        path = self.path(key)
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError("素材哈希不一致，请恢复原始文件。")
        return path
