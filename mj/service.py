"""Transactional application services. Models can propose, never approve or settle."""
from copy import deepcopy
from sqlalchemy import select, update
from .contracts import Plan, digest, shot_fingerprint, stage_hash
from .db import Project, Revision, Approval, Asset, Job, emit, uid, now

class DomainError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def fail(code, message, status=409):
    raise DomainError(code, message, status)


def project(db, pid, owner="admin", lock=False):
    query = select(Project).where(Project.id == pid, Project.owner == owner)
    if lock:
        query = query.with_for_update()
    p = db.scalar(query)
    if not p:
        fail("NOT_FOUND", "项目不存在。", 404)
    return p


def current(db, p):
    r = db.scalar(select(Revision).where(Revision.project_id == p.id, Revision.number == p.revision))
    return Plan.model_validate(r.plan)


def approved(db, pid, stage, fingerprint):
    return db.scalar(select(Approval).where(Approval.project_id == pid, Approval.stage == stage,
                     Approval.hash == fingerprint).limit(1)) is not None


def required(db, p, plan, stage):
    if not approved(db, p.id, stage, stage_hash(plan, stage)):
        fail("APPROVAL_REQUIRED", f"{stage} 关卡尚未批准，或已因内容修改失效。")


def asset_json(a):
    return {"id": a.id, "project_id": a.project_id, "kind": a.kind, "sha256": a.sha256,
            "info": a.info, "reviewed": a.reviewed, "mock": a.mock, "actual_motion": a.actual_motion,
            "rights": a.rights, "shot_id": a.shot_id, "fingerprint": a.fingerprint, "created": a.created}


def owned_asset(db, pid, aid):
    a = db.get(Asset, aid)
    if not a or a.project_id != pid:
        fail("ASSET_NOT_FOUND", "素材不存在或不属于该项目。", 404)
    return a


def references(db, pid, plan):
    for e in plan.entities:
        for aid in e.reference_ids:
            a = owned_asset(db, pid, aid)
            if a.kind != "image" or not a.reviewed:
                fail("REFERENCE_UNREVIEWED", "参考图必须先人工审核。", 422)
    for s in plan.shots:
        for aid in s.reference_ids:
            a = owned_asset(db, pid, aid)
            if a.kind != "image" or not a.reviewed:
                fail("REFERENCE_UNREVIEWED", "分镜参考图必须先审核。", 422)
        for kind, aid in [("image", s.image_id), ("video", s.video_id), ("audio", s.audio_id)]:
            if aid:
                a = owned_asset(db, pid, aid)
                if a.kind != kind or not a.reviewed:
                    fail("ASSET_UNREVIEWED", "只有审核通过的对应类型素材可以被选用。", 422)
                if a.shot_id and a.shot_id != s.id:
                    fail("SHOT_MISMATCH", "素材绑定了其他镜头。", 422)
                fp = shot_fingerprint(plan, s, "tts" if kind == "audio" else kind)
                if a.fingerprint and a.fingerprint != fp:
                    fail("STALE_ASSET", f"{s.id} 的 {kind} 候选对应旧输入，请重新选择或生成。", 422)
    for event in plan.audio:
        a = owned_asset(db, pid, event.asset_id)
        if a.kind != "audio" or not a.reviewed:
            fail("AUDIO_UNREVIEWED", "独立音轨必须是已审核音频。", 422)


def save_plan(db, p, plan, expected, actor="admin"):
    if p.revision != expected:
        fail("REVISION_CONFLICT", "另一个页面已修改项目，请刷新后合并。")
    old = current(db, p)
    old_shots = {x.id: x for x in old.shots}
    invalidated = []
    # Clear ONLY selections whose actual dependencies changed. Keep immutable history.
    for s in plan.shots:
        previous = old_shots.get(s.id)
        if not previous:
            continue
        for kind, field in [("image", "image_id"), ("tts", "audio_id"), ("video", "video_id")]:
            if (getattr(s, field) and getattr(s, field) == getattr(previous, field)
                    and shot_fingerprint(old, previous, kind) != shot_fingerprint(plan, s, kind)):
                setattr(s, field, None)
                invalidated.append({"shot": s.id, "field": field})
    references(db, p.id, plan)
    changed = db.execute(update(Project).where(Project.id == p.id, Project.revision == expected)
                         .values(revision=expected+1)).rowcount
    if changed != 1:
        fail("REVISION_CONFLICT", "版本冲突，请刷新。")
    db.add(Revision(project_id=p.id, number=expected+1, plan=plan.model_dump(), hash=digest(plan)))
    db.flush()
    emit(db, p.id, "plan.saved", {"revision": expected+1, "actor": actor, "invalidated": invalidated})
    return invalidated


def create_project(db, name, plan):
    if any([plan.audio, any(e.reference_ids for e in plan.entities),
            any(s.image_id or s.video_id or s.audio_id or s.reference_ids for s in plan.shots)]):
        fail("INITIAL_REFERENCES", "请先创建项目再上传并绑定素材。", 422)
    p = Project(id=uid(), name=name)
    db.add(p); db.flush()
    db.add(Revision(project_id=p.id, number=1, plan=plan.model_dump(), hash=digest(plan)))
    emit(db, p.id, "project.created", {"revision": 1})
    return p


def snapshot(db, p):
    plan = current(db, p)
    gates = {stage: approved(db, p.id, stage, stage_hash(plan, stage)) for stage in ["story", "design"]}
    gates["sample"] = approved(db, p.id, "sample", sample_hash(plan))
    return {"id": p.id, "name": p.name, "revision": p.revision, "plan": plan.model_dump(), "gates": gates,
            "budget": {"currency": "USD", "limit_micros": p.budget_limit, "used_micros": p.budget_used,
                       "remaining_micros": max(0, p.budget_limit-p.budget_used), "policy": p.budget_policy}}


def sample_hash(plan):
    selected = [s.model_dump() for s in plan.shots if s.id in plan.sample_shots]
    return digest([stage_hash(plan, "design"), selected, [x.model_dump() for x in plan.audio]])


def approve(db, p, stage, expected, evidence):
    if p.revision != expected:
        fail("REVISION_CONFLICT", "审批必须绑定当前版本。")
    plan = current(db, p)
    fingerprint = stage_hash(plan, stage)
    if stage == "story":
        if not plan.selected_concept or not plan.script:
            fail("STORY_INCOMPLETE", "请先选择方案并完善剧本。", 422)
    elif stage == "design":
        required(db, p, plan, "story")
        plan.validate_timeline()
        if len(plan.sample_shots) < 2:
            fail("SAMPLE_MISSING", "请选择代表性样片镜头。", 422)
        for e in plan.entities:
            if e.kind in {"character", "scene"} and not e.reference_ids:
                fail("REFERENCE_MISSING", f"{e.name} 尚未绑定参考图。", 422)
        if not any(e.kind == "character" for e in plan.entities):
            fail("CHARACTER_REQUIRED", "原创漫剧至少需要一个角色设定。", 422)
        references(db, p.id, plan)
    elif stage in {"sample", "release"}:
        references(db, p.id, plan)
        required(db, p, plan, "design")
        jid = evidence.get("job_id")
        job = db.get(Job, jid) if jid else None
        wanted = "sample" if stage == "sample" else "episode"
        if not job or job.project_id != p.id or job.state != "succeeded" or job.result.get("mode") != wanted:
            fail("RENDER_REQUIRED", "请先完成对应模式的渲染。", 422)
        if job.spec["plan_hash"] != digest(plan):
            fail("RENDER_STALE", "该渲染不对应当前计划。")
        if not all(evidence.get(x) is True for x in ["story", "continuity", "motion", "sound", "subtitles"]):
            fail("REVIEW_REQUIRED", "请连续观看并逐项确认故事、连续性、动作、声音和字幕。", 422)
        if stage == "sample":
            if not any(s.motion in {"subject_action", "lip_sync"} for s in plan.shots if s.id in plan.sample_shots):
                fail("ACTION_REQUIRED", "代表性样片必须检验实际人物动作。", 422)
            fingerprint = sample_hash(plan)
        else:
            fingerprint = digest([jid, job.result["sha256"], digest(plan)])
    else:
        fail("INVALID_GATE", "未知审批关卡。", 422)
    db.add(Approval(project_id=p.id, stage=stage, hash=fingerprint, actor="admin", evidence=evidence))
    emit(db, p.id, "approval.created", {"stage": stage, "hash": fingerprint, "revision": p.revision})


def authorize_budget(db, p, limit, policy):
    if limit < p.budget_used:
        fail("BUDGET_COMMITTED", "预算不能低于已结算与尚待核对的占用。")
    if policy["expires"] <= now():
        fail("BUDGET_EXPIRED", "授权已过期。", 422)
    p.budget_limit = limit
    p.budget_policy = policy
    emit(db, p.id, "budget.authorized", {"limit_micros": limit, "policy": policy, "actor": "admin"})


def job_json(j):
    return {"id": j.id, "project_id": j.project_id, "kind": j.kind, "provider": j.provider,
            "state": j.state, "remote_id": j.remote_id, "result": j.result, "error": j.error,
            "reserved_micros": j.reserved, "charge_state": j.charge_state, "actual_cost_micros": j.actual_cost,
            "cancel_requested": j.cancel_requested, "created": j.created, "revision": j.spec.get("revision")}


def freeze_assets(db, pid, plan):
    ids = {aid for s in plan.shots for aid in [s.image_id, s.video_id, s.audio_id, *s.reference_ids] if aid}
    ids.update(a.asset_id for a in plan.audio)
    ids.update(aid for e in plan.entities for aid in e.reference_ids)
    result = {}
    for aid in ids:
        a = owned_asset(db, pid, aid)
        result[aid] = {**asset_json(a), "blob": a.blob}
    return result


def enqueue(db, p, settings, body, key):
    request_hash = digest(body)
    existing = db.scalar(select(Job).where(Job.project_id == p.id, Job.idempotency_key == key))
    if existing:
        if existing.request_hash != request_hash:
            fail("IDEMPOTENCY_CONFLICT", "同一幂等键不可用于不同请求。")
        return existing
    if p.revision != body["expected_revision"]:
        fail("REVISION_CONFLICT", "生成请求基于旧版本，请刷新。")
    plan = current(db, p)
    kind = body["kind"]
    provider = "local" if kind == "render" else body["provider"]
    cfg = deepcopy(settings.providers().get(provider, {}))
    if kind == "render":
        cfg = {"type": "local", "max_cost_micros": 0}
        plan.validate_timeline()
        mode = body.get("mode", "animatic")
        if mode != "animatic":
            required(db, p, plan, "design")
        if mode == "episode" and not approved(db, p.id, "sample", sample_hash(plan)):
            fail("SAMPLE_REQUIRED", "批量成片前必须通过同一设计的真实样片。")
    else:
        if not cfg.get("enabled") or kind not in cfg.get("kinds", []):
            fail("PROVIDER_UNAVAILABLE", "供应商未配置或不支持此任务。", 422)
        if kind in {"image", "tts"}:
            required(db, p, plan, "story")
        if kind == "video":
            required(db, p, plan, "design")
            if body.get("shot_id") not in plan.sample_shots and not approved(db, p.id, "sample", sample_hash(plan)):
                fail("SAMPLE_REQUIRED", "请先完成选定样片再批量生成。")
    shot = next((s for s in plan.shots if s.id == body.get("shot_id")), None)
    entity = next((e for e in plan.entities if e.id == body.get("entity_id")), None)
    if kind in {"video", "tts"} and not shot:
        fail("SHOT_REQUIRED", "此操作需要具体镜头。", 422)
    if kind == "image" and not (shot or entity):
        fail("TARGET_REQUIRED", "请选择镜头或设定实体。", 422)
    if kind == "tts" and not shot.narration:
        fail("SPEECH_EMPTY", "镜头没有台词或旁白。", 422)
    if kind == "video" and shot.motion == "lip_sync":
        fail("UNSUPPORTED_LIP_SYNC", "首版没有通过验证的精确口型路径，请明确修改分镜。", 422)
    if kind == "video" and not shot.image_id:
        fail("FRAME_REQUIRED", "请先选择分镜参考图。", 422)
    if kind == "storyboard" and not plan.selected_concept:
        fail("CONCEPT_REQUIRED", "请先选择故事方案。", 422)
    references(db, p.id, plan)
    cost = 0
    paid = cfg.get("type") not in {"local", "mock"}
    if paid:
        policy = p.budget_policy or {}
        if not settings.paid_enabled:
            fail("PAID_DISABLED", "服务端付费开关未启用。", 403)
        if not policy.get("outbound") or policy.get("expires", 0) <= now():
            fail("BUDGET_AUTH_REQUIRED", "需要有效预算及素材外发授权。", 403)
        if provider not in policy.get("providers", []) or kind not in policy.get("kinds", []):
            fail("BUDGET_SCOPE", "任务超出授权的供应商/能力范围。", 403)
        cost = cfg.get("max_cost_micros", {}).get(kind, 0) if isinstance(cfg.get("max_cost_micros"), dict) else cfg.get("max_cost_micros", 0)
        if not isinstance(cost, int) or cost <= 0 or cost > policy.get("per_task_micros", 0):
            fail("QUOTE_REQUIRED", "缺少在单次授权上限内的保守费用配置。", 422)
        # Admission and task creation are one DB transaction, not a check-then-spend race.
        if db.execute(update(Project).where(Project.id == p.id, Project.budget_used + cost <= Project.budget_limit)
                      .values(budget_used=Project.budget_used+cost)).rowcount != 1:
            fail("BUDGET_EXCEEDED", "剩余预算不足，未发送任何生成请求。")
    spec = {"plan": plan.model_dump(), "plan_hash": digest(plan), "revision": p.revision,
            "provider_config": cfg, "assets": freeze_assets(db, p.id, plan), "request": body,
            "fingerprint": shot_fingerprint(plan, shot, kind) if shot else None,
            "scope": {"outbound": paid, "provider": provider, "kind": kind}}
    if kind == "render":
        spec["cost_snapshot"] = [{"job_id": j.id, "provider": j.provider, "kind": j.kind,
                  "state": j.state, "charge_state": j.charge_state,
                  "reserved_micros": j.reserved, "actual_micros": j.actual_cost}
                  for j in db.scalars(select(Job).where(Job.project_id == p.id))]
    job = Job(id=uid(), project_id=p.id, kind=kind, provider=provider, spec=spec,
              request_hash=request_hash, idempotency_key=key, reserved=cost,
              charge_state="reserved" if paid else "released")
    db.add(job); db.flush()
    emit(db, p.id, "task.queued", {"job_id": job.id, "kind": kind, "reserved_micros": cost})
    return job


def cancel(db, job):
    if job.state in {"succeeded", "failed", "cancelled"}:
        return
    job.cancel_requested = True
    if job.state == "queued":
        job.state = "cancelled"
        if job.charge_state == "reserved":
            db.execute(update(Project).where(Project.id == job.project_id).values(budget_used=Project.budget_used-job.reserved))
            job.charge_state = "released"
    emit(db, job.project_id, "task.cancel_requested", {"job_id": job.id, "remote_cancellation_guaranteed": False})


def settle(db, job, amount, evidence):
    if job.charge_state in {"settled", "released"}:
        fail("ALREADY_SETTLED", "此任务费用已结算或未占用付费预算。")
    if job.state not in {"succeeded", "failed", "cancelled", "reconciling"}:
        fail("TASK_ACTIVE", "任务仍在运行，不能提前释放费用。")
    if not evidence.strip():
        fail("EVIDENCE_REQUIRED", "请填写账单或核对依据。", 422)
    difference = amount - job.reserved
    db.execute(update(Project).where(Project.id == job.project_id).values(budget_used=Project.budget_used+difference))
    job.actual_cost, job.charge_state = amount, "settled"
    emit(db, job.project_id, "budget.settled", {"job_id": job.id, "actual_micros": amount, "evidence": evidence,
                                               "actor": "admin"})


def seal_release(db, p, job, expected, store):
    """Seal an immutable delivery receipt after G4, separate from render success."""
    import hashlib
    import json
    import os
    import zipfile
    if p.revision != expected:
        fail('REVISION_CONFLICT', '交付必须绑定当前修订。')
    plan = current(db, p)
    if job.state != 'succeeded' or job.result.get('mode') != 'episode' or job.spec['plan_hash'] != digest(plan):
        fail('RENDER_STALE', '请先完成当前版本的整集渲染。')
    references(db, p.id, plan)
    fingerprint = digest([job.id, job.result['sha256'], digest(plan)])
    approval = db.scalar(select(Approval).where(Approval.project_id == p.id,
                         Approval.stage == 'release', Approval.hash == fingerprint).order_by(Approval.created))
    if not approval:
        fail('RELEASE_APPROVAL_REQUIRED', '请先完成G4人工终审。')
    if job.result.get('release_id'):
        return job_json(job)
    folder = store.path(f"work/{job.result.get('storage_run', job.id)}/project.zip").parent
    source = folder / 'project.zip'
    receipt = {'schema_version':'mj.release.v1','release_id':approval.id,
               'project_id':p.id,'revision':p.revision,'plan_hash':digest(plan),
               'render_job_id':job.id,'video_sha256':job.result['sha256'],
               'approval':{'actor':approval.actor,'created':approval.created,'checklist':approval.evidence},
               'source_bundle_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
               'note':'人工创作审核与技术校验是不同证据；不构成自动发布或版权保证。'}
    temp = folder / f'release-{uid()}.zip'
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as new:
        manifest = json.loads(old.read('manifest.json'))
        checks = {name:v['sha256'] for name,v in manifest['files'].items()}
        checks.update({a['path']:a['sha256'] for a in manifest['assets'].values()})
        for name in old.namelist():
            data = old.read(name)
            if name in checks and hashlib.sha256(data).hexdigest() != checks[name]:
                fail('BUNDLE_CORRUPTED','交付包校验失败，不能封存。',422)
            new.writestr(name,data)
        new.writestr('release.json',json.dumps(receipt,ensure_ascii=False,indent=2))
    os.replace(temp,folder/'release.zip')
    (folder/'release.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    job.result = {**job.result,'release_id':approval.id,'files':[*job.result['files'],'release.json','release.zip']}
    emit(db,p.id,'release.sealed',{'release_id':approval.id,'job_id':job.id,'revision':p.revision})
    return job_json(job)
