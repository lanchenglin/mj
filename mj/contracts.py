"""Versioned, strict contracts. Seconds are derived, never a second source of truth."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
import hashlib
import json
import re

class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

class Concept(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    title: str = Field(min_length=1, max_length=100)
    logline: str = Field(min_length=1, max_length=1500)
    conflict: str = Field(default="", max_length=1500)
    ending: str = Field(default="", max_length=1500)

class Entity(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    kind: Literal["character", "scene", "prop"]
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4000)
    reference_ids: list[str] = Field(default_factory=list, max_length=8)

class Shot(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    scene_id: str = ""
    character_ids: list[str] = Field(default_factory=list, max_length=10)
    prop_ids: list[str] = Field(default_factory=list, max_length=20)
    frames: int = Field(ge=1, le=9000)
    action: str = Field(min_length=1, max_length=4000)
    framing: str = Field(default="中景", max_length=200)
    state_before: str = Field(default="", max_length=2000)
    state_after: str = Field(default="", max_length=2000)
    motion: Literal["none", "camera", "subject_action", "lip_sync"] = "subject_action"
    narration: str = Field(default="", max_length=2000)
    speaker: str = Field(default="narrator", max_length=80)
    reference_ids: list[str] = Field(default_factory=list, max_length=8)
    image_id: str | None = None
    video_id: str | None = None
    audio_id: str | None = None
    source_in_frame: int = Field(default=0, ge=0)

class AudioEvent(Contract):
    asset_id: str
    lane: Literal["voice", "music", "sfx"]
    start_sample: int = Field(ge=0)
    source_in_sample: int = Field(default=0, ge=0)
    samples: int = Field(gt=0)
    gain_db: float = Field(default=0, ge=-60, le=12)
    loop: bool = False
    @model_validator(mode="after")
    def validate_loop(self):
        if self.loop and self.lane == "voice":
            raise ValueError("Speech cannot be looped.")
        return self

class Caption(Contract):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=1000)
    @model_validator(mode="after")
    def valid_range(self):
        if self.end_frame <= self.start_frame:
            raise ValueError("Invalid caption interval")
        return self

class Plan(Contract):
    schema_version: Literal["mj.plan.v1"] = "mj.plan.v1"
    brief: str = Field(default="", max_length=20000)
    style: str = Field(default="二维漫画，统一角色造型", max_length=2000)
    concepts: list[Concept] = Field(default_factory=list, max_length=10)
    selected_concept: str | None = None
    script: str = Field(default="", max_length=30000)
    entities: list[Entity] = Field(default_factory=list, max_length=50)
    shots: list[Shot] = Field(default_factory=list, max_length=120)
    sample_shots: list[str] = Field(default_factory=list, max_length=6)
    fps: Literal[24, 25, 30] = 30
    total_frames: int = Field(default=3600, ge=24, le=18000)
    width: int = Field(default=1080, ge=160, le=1920, multiple_of=2)
    height: int = Field(default=1920, ge=160, le=1920, multiple_of=2)
    voice: str = Field(default="alloy", max_length=100)
    voice_profiles: dict[str, str] = Field(default_factory=dict)
    music_ducking: bool = True
    audio: list[AudioEvent] = Field(default_factory=list, max_length=240)
    captions: list[Caption] = Field(default_factory=list, max_length=1000)
    subtitle_size: int = Field(default=54, ge=20, le=100)
    subtitle_margin: int = Field(default=180, ge=20, le=600)
    @model_validator(mode="after")
    def relationships(self):
        for values in [self.entities, self.shots, self.concepts]:
            if len({x.id for x in values}) != len(values):
                raise ValueError("Duplicate stable ID")
        entities = {x.id: x.kind for x in self.entities}
        for shot in self.shots:
            if shot.scene_id and entities.get(shot.scene_id) != "scene":
                raise ValueError(f"Unknown scene in {shot.id}")
            if any(entities.get(x) != "character" for x in shot.character_ids):
                raise ValueError(f"Unknown character in {shot.id}")
            if any(entities.get(x) != "prop" for x in shot.prop_ids):
                raise ValueError(f"Unknown prop in {shot.id}")
            if shot.speaker != "narrator" and entities.get(shot.speaker) != "character":
                raise ValueError(f"Unknown speaker in {shot.id}")
        if len(set(self.sample_shots)) != len(self.sample_shots):
            raise ValueError("Duplicate sample shot")
        if not set(self.sample_shots) <= {x.id for x in self.shots}:
            raise ValueError("Unknown sample shot")
        if self.selected_concept and self.selected_concept not in {x.id for x in self.concepts}:
            raise ValueError("Unknown selected concept")
        total_samples = self.total_frames * 48000 // self.fps
        if any(x.start_sample + x.samples > total_samples for x in self.audio):
            raise ValueError("Audio exceeds the episode")
        if any(x.end_frame > self.total_frames for x in self.captions):
            raise ValueError("Caption exceeds the episode")
        return self

    def validate_timeline(self):
        if not self.shots or sum(s.frames for s in self.shots) != self.total_frames:
            raise ValueError("镜头总帧数必须等于项目目标；不能用截断或补尾帧掩盖差额。")


def digest(data) -> str:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def shot_fingerprint(plan: Plan, shot: Shot, kind: str) -> str:
    if kind == "tts":
        return digest([shot.narration, shot.speaker, plan.voice_profiles.get(shot.speaker, plan.voice)])
    excluded = {"image_id", "video_id", "audio_id", "source_in_frame", "narration", "speaker"}
    if kind == "image":
        excluded |= {"frames", "motion"}
    s = shot.model_dump(exclude=excluded)
    refs = [e.model_dump() for e in plan.entities
            if e.id in shot.character_ids or e.id == shot.scene_id or e.id in shot.prop_ids]
    return digest([s, refs, plan.style, plan.width, plan.height,
                   shot.image_id if kind == "video" else None])


def stage_hash(plan: Plan, stage: str) -> str:
    story = digest(
        [plan.brief, [x.model_dump() for x in plan.concepts], plan.selected_concept, plan.script])
    if stage == "story":
        return story
    return digest([story, plan.style, plan.voice, plan.voice_profiles, plan.fps, plan.total_frames, plan.width, plan.height,
                   [e.model_dump() for e in plan.entities], plan.sample_shots,
                   [s.model_dump(exclude={"image_id", "video_id", "audio_id", "source_in_frame"}) for s in plan.shots]])


def chinese_chunks(text: str, maximum: int = 18) -> list[str]:
    parts = re.findall(r"[^。！？；，\n]+[。！？；，]?", text)
    return [p[i:i+maximum].strip() for p in parts for i in range(0, len(p), maximum) if p[i:i+maximum].strip()]
