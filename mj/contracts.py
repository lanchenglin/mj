from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Spec(Contract):
    width: int = Field(default=1080, ge=256, le=2160)
    height: int = Field(default=1920, ge=256, le=3840)
    fps: int = Field(default=30, ge=12, le=60)
    total_frames: int = Field(default=3600, ge=12, le=18000)

    @model_validator(mode="after")
    def valid(self):
        if self.width % 2 or self.height % 2:
            raise ValueError("Output dimensions must be even")
        if self.total_frames / self.fps > 300:
            raise ValueError("Maximum duration is 300 seconds")
        return self


class NewProject(Contract):
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(min_length=1, max_length=20000)
    style: str = Field(default="二维漫画，暖色光影", max_length=1000)
    spec: Spec = Field(default_factory=Spec)


class Concept(Contract):
    id: str
    title: str
    logline: str
    conflict: str
    ending: str


class ScriptBeat(Contract):
    id: str
    action: str
    narration: str = ""
    speaker_id: str = "narrator"
    emotion: str = "自然"


class Entity(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str
    description: str
    reference_ids: list[str] = Field(default_factory=list, max_length=8)


class Bible(Contract):
    characters: list[Entity] = Field(default_factory=list, max_length=12)
    scenes: list[Entity] = Field(default_factory=list, max_length=20)
    props: list[Entity] = Field(default_factory=list, max_length=30)


class Shot(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    beat_id: str = ""
    scene_id: str = ""
    character_ids: list[str] = Field(default_factory=list, max_length=12)
    frames: int = Field(ge=1, le=1800)
    framing: str = "中景"
    action: str = Field(min_length=1, max_length=4000)
    before: str = ""
    after: str = ""
    motion: Literal["none", "camera", "subject_action", "lip_sync"] = "subject_action"
    narration: str = Field(default="", max_length=3000)
    reference_ids: list[str] = Field(default_factory=list, max_length=8)
    video_asset: str | None = None
    audio_asset: str | None = None
    source_in_frames: int = Field(default=0, ge=0)
    fit: Literal["contain", "cover"] = "contain"
    # Native video audio is muted unless explicitly reviewed and selected.
    original_audio: Literal["mute", "keep"] = "mute"
    original_audio_gain: float = Field(default=1, ge=0, le=2)


class AudioClip(Contract):
    asset_id: str
    start_sample: int = Field(ge=0)
    source_in_sample: int = Field(default=0, ge=0)
    samples: int = Field(ge=1)
    gain: float = Field(default=1, ge=0, le=4)
    bus: Literal["speech", "music", "sfx"] = "speech"
    fade_samples: int = Field(default=0, ge=0, le=48000)


class SubtitleStyle(Contract):
    font_size: int = Field(default=52, ge=16, le=120)
    margin_v: int = Field(default=180, ge=0, le=1000)
    line_chars: int = Field(default=16, ge=4, le=32)


class Workspace(Contract):
    concepts: list[Concept] = Field(default_factory=list, max_length=10)
    selected_concept: str = ""
    script: list[ScriptBeat] = Field(default_factory=list, max_length=100)
    bible: Bible = Field(default_factory=Bible)
    shots: list[Shot] = Field(default_factory=list, max_length=100)
    extra_audio: list[AudioClip] = Field(default_factory=list, max_length=100)
    subtitles: SubtitleStyle = Field(default_factory=SubtitleStyle)
    duck_music: bool = True

    @model_validator(mode="after")
    def links(self):
        for items in (self.concepts, self.script, self.shots, self.bible.characters, self.bible.scenes, self.bible.props):
            ids = [x.id for x in items]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate entity IDs")
        if self.selected_concept and self.selected_concept not in {x.id for x in self.concepts}:
            raise ValueError("Unknown selected concept")
        chars = {x.id for x in self.bible.characters}
        scenes = {x.id for x in self.bible.scenes}
        beats = {x.id for x in self.script}
        for s in self.shots:
            if not set(s.character_ids) <= chars:
                raise ValueError(f"{s.id}: unknown character")
            if s.scene_id and s.scene_id not in scenes:
                raise ValueError(f"{s.id}: unknown scene")
            if s.beat_id and s.beat_id not in beats:
                raise ValueError(f"{s.id}: unknown beat")
        return self


class RevisionInput(Contract):
    expected_revision: int = Field(ge=1)
    content: Workspace


class ImageOptions(Contract):
    workflow: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,64}$")
    seed: int | None = Field(default=None, ge=0, le=2**63-1)
    width: int | None = Field(default=None, ge=256, le=2048)
    height: int | None = Field(default=None, ge=256, le=2048)
    negative_prompt: str = Field(default="", max_length=4000)
    denoise: float | None = Field(default=None, ge=0.01, le=1)
    mask_asset_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    allow_remote_processing: bool = False
    use_shot_references: bool = True


class CloudImageOptions(Contract):
    width: int = Field(default=1024, ge=512, le=2048)
    height: int = Field(default=1536, ge=512, le=2048)
    seed: int | None = Field(default=None, ge=0, le=2147483647)
    negative_prompt: str = Field(default="", max_length=500)
    prompt_extend: bool = False
    use_shot_references: bool = True

    @model_validator(mode="after")
    def dimensions(self):
        if self.width % 16 or self.height % 16:
            raise ValueError("Cloud image dimensions must be multiples of 16")
        return self


class VideoReference(Contract):
    asset_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    role: Literal["reference_image", "reference_video", "reference_audio"]


class VideoOptions(Contract):
    mode: Literal["text", "first_frame", "last_frame", "first_last", "reference"] = "first_frame"
    first_frame_id: str | None = None
    last_frame_id: str | None = None
    references: list[VideoReference] = Field(default_factory=list, max_length=15)
    duration_seconds: int | None = Field(default=None, ge=4, le=15)
    resolution: Literal["480P", "768P", "2K"] = "768P"

    @model_validator(mode="after")
    def exclusive(self):
        frames = bool(self.first_frame_id or self.last_frame_id)
        if self.mode in ("text", "reference") and frames:
            raise ValueError("Frame inputs and reference/text modes cannot be mixed")
        if self.mode != "reference" and self.references:
            raise ValueError("References require reference mode; do not mix frame and reference inputs")
        if self.mode == "first_frame" and self.last_frame_id:
            raise ValueError("Last frame is not allowed in first_frame mode")
        if self.mode == "last_frame" and self.first_frame_id:
            raise ValueError("First frame is not allowed in last_frame mode")
        ids = [x.asset_id for x in self.references]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate video reference assets")
        return self


class SpeechOptions(Contract):
    voice_id: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,100}$")
    speed: float = Field(default=1, ge=0.5, le=2)
    volume: float = Field(default=1, gt=0, le=4)
    pitch: int = Field(default=0, ge=-12, le=12)
    pronunciation: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def dictionary(self):
        if any(not x or len(x) > 200 or "/" not in x for x in self.pronunciation):
            raise ValueError("Each pronunciation entry must be a short word/pronunciation pair")
        return self


class TaskInput(Contract):
    kind: Literal["concepts", "script", "bible", "storyboard", "image", "video", "tts", "animatic", "render"]
    provider: str = "mock"
    image_options: ImageOptions | None = None
    cloud_image_options: CloudImageOptions | None = None
    video_options: VideoOptions | None = None
    speech_options: SpeechOptions | None = None
    shot_id: str = ""
    reference_ids: list[str] = Field(default_factory=list, max_length=8)
    prompt: str = Field(default="", max_length=10000)
    budget_id: str | None = None
    cap_micros: int = Field(default=0, ge=0, le=10**12)
    expected_revision: int = Field(ge=1)
    dry_run: bool = False
    sample: bool = False
    sample_shot_ids: list[str] = Field(default_factory=list, max_length=10)
    voice: str = Field(default="alloy", pattern=r"^[A-Za-z0-9_-]{1,100}$")


class BudgetInput(Contract):
    currency: Literal["CNY", "USD"] = "USD"
    limit_micros: int = Field(gt=0, le=10**12)
    per_task_micros: int = Field(gt=0, le=10**12)
    providers: list[str] = Field(min_length=1, max_length=20)
    kinds: list[str] = Field(min_length=1, max_length=10)
    expires_in_hours: int = Field(default=24, ge=1, le=720)
    allow_reference_upload: bool = False


class ApprovalInput(Contract):
    gate: Literal["G1", "G2", "G3", "G4"]
    expected_revision: int
    task_id: str | None = None
    note: str = Field(min_length=2, max_length=2000)
    checks: list[str] = Field(default_factory=list)


def gate_hash(gate: str, content: dict) -> str:
    keys = {"G1": ["concepts", "selected_concept", "script"],
            "G2": ["script", "bible", "shots", "extra_audio"],
            "G3": ["script", "bible", "shots", "extra_audio"],
            "G4": list(content)}[gate]
    values = {k: content.get(k) for k in keys}
    if gate in ("G2", "G3"):
        # Candidate selection is independent from approval of the shot contract.
        values["shots"] = [{k: v for k, v in s.items() if k not in ("video_asset", "audio_asset")} for s in values.get("shots", [])]
    return fingerprint(values)


def subtitle_chunks(text: str, maximum: int = 16) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.findall(r"[^，。！？；\n]+[，。！？；]?", text)
    result = []
    for part in parts:
        while len(part) > maximum:
            at = part.rfind(" ", 0, maximum + 1)
            at = at if at > maximum // 2 else maximum
            result.append(part[:at].strip())
            part = part[at:].strip()
        if part:
            result.append(part)
    return result
