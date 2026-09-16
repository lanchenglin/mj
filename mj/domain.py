from __future__ import annotations

import json
import re
import time
from pathlib import Path
from sqlalchemy import select
from .contracts import (ApprovalInput, BudgetInput, NewProject, RevisionInput, TaskInput,
                        Workspace, fingerprint, gate_hash)
from .db import Approval, Asset, Budget, Database, Project, Revision, Task, log, public
from .media import Store


class Problem(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def require(ok, code, message, status=422):
    if not ok:
        raise Problem(code, message, status)


class Studio:
    def __init__(self, settings):
        self.settings = settings.validate()
        self.db = Database(settings.database_url)
        self.store = Store(settings.data_dir)
        self.providers = settings.providers()

    def project(self, s, pid, actor="admin", lock=False):
        q = select(Project).where(Project.id == pid, Project.owner == actor)
        if lock:
            q = q.with_for_update()
        p = s.scalar(q)
        require(p is not None, "not_found", "Project not found", 404)
        return p

    def revision(self, s, p):
        return s.scalar(select(Revision).where(Revision.project_id == p.id, Revision.number == p.revision))

    def save_revision(self, s, p, content, expected):
        require(p.revision == expected, "revision_conflict", "Project changed; refresh before saving", 409)
        value = Workspace.model_validate(content).model_dump()
        self.validate_references(s, p.id, value)
        p.revision += 1
        s.add(Revision(project_id=p.id, number=p.revision, content=value, content_hash=fingerprint(value)))
        log(s, p.id, "revision.saved", {"revision": p.revision, "hash": fingerprint(value)}, "admin")
        return p.revision

    def validate_references(self, s, pid, content):
        expected = []
        for group in ("characters", "scenes", "props"):
            for entity in content["bible"][group]:
                expected += [(i, "image") for i in entity["reference_ids"]]
        for shot in content["shots"]:
            expected += [(i,"image") for i in shot["reference_ids"]]
            if shot["video_asset"]:
                expected.append((shot["video_asset"], "visual"))
            if shot["audio_asset"]:
                expected.append((shot["audio_asset"], "audio"))
        expected += [(a["asset_id"], "audio") for a in content["extra_audio"]]
        for aid, kind in expected:
            a = s.get(Asset, aid)
            require(a is not None and a.project_id == pid, "asset_not_found", "Asset unavailable in project",404)
            require(a.kind in ("image", "video") if kind == "visual" else a.kind == kind, "asset_type", "Wrong asset type")

    def create_project(self, req: NewProject, actor="admin"):
        with self.db.tx() as s:
            p = Project(owner=actor, title=req.title, brief=req.brief, style=req.style, spec=req.spec.model_dump())
            s.add(p); s.flush()
            content = Workspace().model_dump()
            s.add(Revision(project_id=p.id, number=1, content=content, content_hash=fingerprint(content)))
            log(s,p.id,"project.created",{"title":p.title},actor)
            return p.id

    def snapshot(self, pid, actor="admin"):
        with self.db.sessions() as s:
            p = self.project(s,pid,actor)
            rev = self.revision(s,p)
            approvals = s.scalars(select(Approval).where(Approval.project_id==pid).order_by(Approval.created_at)).all()
            return {**public(p), "content":rev.content,"content_hash":rev.content_hash,
                    "approvals":[{**public(a),"current":self.approval_current(s,a,rev.content)} for a in approvals]}

    def update(self,pid,req:RevisionInput,actor="admin"):
        with self.db.tx() as s:
            p=self.project(s,pid,actor,True)
            self.save_revision(s,p,req.content.model_dump(),req.expected_revision)
        return self.snapshot(pid,actor)

    def approval_current(self,s,a,content):
        if a.scope_hash != gate_hash(a.gate,content):
            return False
        if a.gate not in ("G3","G4"):
            return True
        t=s.get(Task,a.task_id)
        if not t or t.state!="succeeded" or not t.result or t.result.get("mock",True):
            return False
        current={sh["id"]:sh for sh in content["shots"]}
        sample_ids=set(t.snapshot["request"].get("sample_shot_ids",[]))
        for shot in t.snapshot["content"]["shots"]:
            if a.gate=="G3" and shot["id"] not in sample_ids:
                continue
            now=current.get(shot["id"],{})
            for field in ("video_asset","audio_asset"):
                if now.get(field)!=shot.get(field):
                    return False
                if shot.get(field):
                    asset=s.get(Asset,shot[field])
                    if not asset or asset.review!="accepted" or asset.mock:
                        return False
        for ev in t.snapshot["content"]["extra_audio"]:
            asset=s.get(Asset,ev["asset_id"])
            if not asset or asset.review!="accepted" or asset.mock:
                return False
        return True

    def has_gate(self,s,p,content,gate):
        approvals=s.scalars(select(Approval).where(Approval.project_id==p.id,Approval.gate==gate)).all()
        return any(self.approval_current(s,a,content) for a in approvals)

    def approve(self,pid,req:ApprovalInput,actor="admin"):
        with self.db.tx() as s:
            p=self.project(s,pid,actor,True)
            require(p.revision==req.expected_revision,"revision_conflict","Refresh project before approving",409)
            rev=self.revision(s,p); c=rev.content
            require(bool(c["selected_concept"] and c["script"]),"missing_script","Select a concept and complete script")
            if req.gate!="G1":
                require(self.has_gate(s,p,c,"G1"),"approval_required","G1 story approval required",409)
                require(c["shots"] and sum(x["frames"] for x in c["shots"])==p.spec["total_frames"],"timeline_length","Shot timeline does not match target")
            if req.gate in ("G3","G4"):
                t=s.get(Task,req.task_id)
                require(t is not None and t.project_id==pid and t.kind=="render" and t.state=="succeeded","sample_required","An actual successful render is required")
                require(not t.result.get("mock",True),"mock_not_deliverable","Mock previews cannot pass production gates")
                require(t.result["qa"]["technical_pass"],"qa_failed","Technical QA did not pass")
                require(gate_hash(req.gate,t.snapshot["content"])==gate_hash(req.gate,c),"stale_render","Render does not match current content",409)
                require(bool(t.snapshot["request"].get("sample")) == (req.gate=="G3"),"wrong_render","G3 requires sample; G4 requires full render")
                require({"identity","motion","continuity","voice","subtitles"} <= set(req.checks),"review_incomplete","Complete identity, motion, continuity, voice and subtitle review")
            if req.gate=="G4":
                require(self.has_gate(s,p,c,"G3"),"approval_required","G3 real sample approval required",409)
            a=Approval(project_id=pid,gate=req.gate,scope_hash=gate_hash(req.gate,c),revision=p.revision,task_id=req.task_id,note=req.note,actor=actor)
            s.add(a);s.flush()
            require(self.approval_current(s,a,c),"review_stale","Media selection or review changed; render and review again",409)
            log(s,pid,"gate.approved",{"id":a.id,"gate":req.gate,"revision":p.revision,"checks":req.checks},actor)
            return public(a)

    def authorize(self,pid,req:BudgetInput,actor="admin"):
        with self.db.tx() as s:
            self.project(s,pid,actor,True)
            require(req.per_task_micros<=req.limit_micros,"invalid_budget","Per-task cap exceeds total")
            require(all(x in self.providers for x in req.providers),"provider_unknown","Unknown configured provider")
            require(all(self.providers[x].get("currency")==req.currency for x in req.providers),"currency_conflict","Every provider in this grant must bill in the grant currency; use separate grants for USD/CNY")
            b=Budget(project_id=pid,currency=req.currency,limit_micros=req.limit_micros,per_task_micros=req.per_task_micros,policy=req.model_dump(),expires=time.time()+req.expires_in_hours*3600)
            s.add(b);s.flush();log(s,pid,"budget.authorized",{"id":b.id,"limit_micros":b.limit_micros,"currency":b.currency},actor)
            return public(b)

    def add_asset(self,pid,source:Path,name:str,rights:str,actor="admin",mock=False,task_id=None,motion="none"):
        require(len(rights.strip())>=2,"rights_required","Record ownership or permission before importing")
        with self.db.sessions() as s:
            self.project(s,pid,actor)
        h,ext,info=self.store.ingest(source)
        with self.db.tx() as s:
            a=Asset(project_id=pid,name=Path(name).name[:200],kind=info["kind"],blob_hash=h,extension=ext,info=info,mock=mock,task_id=task_id,motion=motion,rights=rights)
            s.add(a);s.flush();log(s,pid,"asset.created",{"id":a.id,"sha256":h,"mock":mock},actor)
            return public(a)

    def review_asset(self,pid,aid,status,motion,note,actor="admin"):
        require(status in ("accepted","rejected"),"review_invalid","Choose accepted or rejected")
        require(motion in ("none","camera","subject_action","lip_sync"),"motion_invalid","Unknown motion contract")
        require(len(note.strip())>=2,"review_note","Explain the review")
        with self.db.tx() as s:
            self.project(s,pid,actor,True)
            a=s.get(Asset,aid)
            require(a is not None and a.project_id==pid,"not_found","Asset not found",404)
            require(a.kind=="video" or motion in ("none","camera"),"motion_invalid","Still image cannot satisfy subject action")
            a.review=status;a.motion=motion
            log(s,pid,"asset.reviewed",{"id":aid,"status":status,"motion":motion,"note":note},actor)
            return public(a)

    def enqueue(self,pid,req:TaskInput,key:str,actor="admin"):
        require(bool(re.fullmatch(r"[A-Za-z0-9_-]{8,128}",key)),"idempotency_required","Provide an 8–128 character Idempotency-Key")
        body=req.model_dump()
        for field in ("image_options", "cloud_image_options", "video_options", "speech_options"):
            if body.get(field) is None:
                body.pop(field, None)  # Preserve pre-upgrade idempotency fingerprints.
        request_hash=fingerprint(body)
        with self.db.tx() as s:
            p=self.project(s,pid,actor,True)
            previous=s.scalar(select(Task).where(Task.project_id==pid,Task.idempotency_key==key))
            if previous:
                require(previous.request_hash==request_hash,"idempotency_conflict","Key already used with another request",409)
                return public(previous)
            require(p.revision==req.expected_revision,"revision_conflict","Refresh before generating",409)
            content=self.revision(s,p).content
            shot=next((x for x in content["shots"] if x["id"]==req.shot_id),None)
            if req.kind in ("video","tts"):
                require(shot is not None,"shot_required","Select a shot")
                require(shot["motion"]!="lip_sync" or req.kind=="tts","unsupported_capability","Lip-sync is not implemented in this adapter")
            if req.kind=="script":
                require(bool(content["selected_concept"]),"concept_required","Choose a concept first")
            if req.kind in ("bible","storyboard"):
                require(bool(content["script"]),"script_required","Write or generate script first")
            local=req.kind in ("animatic","render") or req.provider=="mock"
            cfg={} if local else self.providers.get(req.provider)
            require(cfg is not None,"provider_unknown","Provider not configured")
            cfg=json.loads(json.dumps(cfg))  # Freeze without mutating administrator configuration.
            comfy=bool(cfg and cfg.get("type")=="comfyui")
            from .cloud_api import TYPES, references as native_references, prepare as prepare_native, minimum_reserve
            native=bool(cfg and cfg.get("type") in TYPES)
            if not native:
                require(not any((req.cloud_image_options,req.video_options,req.speech_options)),"unsupported_options","Native model options require the matching native provider")
            if not local and not comfy and not native:
                from .providers import preflight
                preflight(cfg,req.kind)
            require(req.image_options is None or comfy,"unsupported_options","image_options are supported only by the ComfyUI image adapter")
            reference_order=list(dict.fromkeys(req.reference_ids+(shot["reference_ids"] if shot and (not comfy or req.image_options is None or req.image_options.use_shot_references) else [])))
            if native:
                reference_order=native_references(req,shot,cfg)
            refs=set(reference_order)
            if comfy and req.image_options and req.image_options.mask_asset_id:
                refs.add(req.image_options.mask_asset_id)
            if req.kind in ("animatic","render"):
                for sh in content["shots"]:
                    refs.update(sh["reference_ids"])
                    refs.update(i for i in [sh["video_asset"],sh["audio_asset"]] if i)
                refs.update(a["asset_id"] for a in content["extra_audio"])
            assets={}
            for aid in refs:
                a=s.get(Asset,aid)
                require(a is not None and a.project_id==pid,"asset_not_found","Referenced asset missing",404)
                assets[aid]=public(a)
            snapshot={"revision":p.revision,"title":p.title,"brief":p.brief,"style":p.style,"spec":p.spec,"content":content,"request":body,"provider_config":cfg,"assets":assets}
            if native:
                snapshot["reference_order"]=reference_order
                snapshot["operation_id"]=fingerprint([pid,key])
                prepare_native(snapshot,self.store)
            if comfy:
                from .comfy_workflows import prepare
                snapshot["reference_order"]=reference_order
                prepare(snapshot,fingerprint([pid,key]))
                require(not req.budget_id and req.cap_micros==0,"self_hosted_budget","Self-hosted ComfyUI uses explicit resource consent, not a fabricated API bill")
            quote={"local":local,"self_hosted":comfy,"cap_micros":req.cap_micros,"references":reference_order,"input_hash":fingerprint(snapshot),"requires_paid_authorization":not local and not comfy}
            if comfy:
                quote["comfy"]={k:snapshot["comfy"][k] for k in ("workflow_id","workflow_hash","mode")}
                quote["compute_cost"]="unmetered; GPU rental/electricity not included"
                quote["requires_remote_consent"]=True
            if native:
                quote["api_plan"]=snapshot["api_plan"]
                quote["minimum_reserve_micros"]=minimum_reserve(cfg,snapshot["api_plan"])
                quote["currency"]=cfg.get("currency")
                quote["billing_note"]="Reservation only, not a verified supplier invoice"
            if req.dry_run:
                return {"dry_run":True,"external_calls":0,"quote":quote}
            if comfy:
                require(self.settings.comfyui_enabled,"comfyui_disabled","Administrator has not enabled self-hosted ComfyUI processing",403)
                require(req.image_options and req.image_options.allow_remote_processing,"remote_consent_required","Approve sending this prompt and the selected references/mask to the configured GPU server",403)
                require(self.has_gate(s,p,content,"G1"),"approval_required","G1 story approval required",409)
                count=len(s.scalars(select(Task.id).where(Task.project_id==pid,Task.provider==req.provider,Task.state.in_(["queued","dispatching","submitted","running","reconciling"]))).all())
                require(count<100,"queue_full","At most 100 outstanding ComfyUI tasks per project",409)
            if not local and not comfy:
                require(self.settings.external_enabled,"paid_disabled","External calls disabled by administrator",403)
                gate="G2" if req.kind=="video" else "G1"
                if req.kind not in ("concepts","script"):
                    require(self.has_gate(s,p,content,gate),"approval_required",f"{gate} approval required",409)
                if req.kind=="video" and not req.sample:
                    require(self.has_gate(s,p,content,"G3"),"approval_required","Approve real sample before batch production",409)
                    verified=False
                    for approval in s.scalars(select(Approval).where(Approval.project_id==pid,Approval.gate=="G3")).all():
                        if not self.approval_current(s,approval,content):
                            continue
                        sample_task=s.get(Task,approval.task_id)
                        sample_ids=set(sample_task.snapshot["request"].get("sample_shot_ids",[]))
                        for sh in sample_task.snapshot["content"]["shots"]:
                            if sh["id"] not in sample_ids or not sh.get("video_asset"):
                                continue
                            a=s.get(Asset,sh["video_asset"])
                            generated=s.get(Task,a.task_id) if a and a.task_id else None
                            if generated and generated.provider==req.provider and fingerprint(generated.snapshot.get("provider_config",{}))==fingerprint(cfg):
                                verified=True
                    require(verified,"provider_sample_required","This video model/config has not passed the approved sample",409)
                b=s.scalar(select(Budget).where(Budget.id==req.budget_id,Budget.project_id==pid).with_for_update())
                require(b is not None and not b.revoked and b.expires>time.time(),"budget_required","Valid budget authorization required",403)
                require(req.provider in b.policy["providers"] and req.kind in b.policy["kinds"],"budget_scope","Task is outside budget scope",403)
                require(not refs or b.policy["allow_reference_upload"],"upload_not_authorized","Reference upload not authorized",403)
                required_cap=minimum_reserve(cfg,snapshot["api_plan"]) if native else int(cfg.get("reserve_micros",{}).get(req.kind,0))
                require(required_cap>0 and req.cap_micros>=required_cap,"quote_required","Configure conservative per-request reserve and approve a sufficient cap")
                require(cfg.get("currency")==b.currency,"currency_conflict","Provider and budget currency differ")
                require(req.cap_micros<=b.per_task_micros and b.used_micros+req.cap_micros<=b.limit_micros,"budget_exceeded","Budget has insufficient unreserved balance",409)
                b.used_micros+=req.cap_micros
            t=Task(project_id=pid,kind=req.kind,provider="local" if req.kind in ("animatic","render") else req.provider,input_hash=fingerprint(snapshot),request_hash=request_hash,idempotency_key=key,snapshot=snapshot,revision=p.revision,budget_id=None if local or comfy else req.budget_id,cap_micros=0 if local or comfy else req.cap_micros,fee_state="unmetered" if comfy else ("free" if local else "reserved"))
            s.add(t);s.flush();log(s,pid,"task.queued",{"task_id":t.id,"kind":t.kind,"input_hash":t.input_hash,"cap_micros":t.cap_micros},actor)
            return public(t)

    def apply_task(self,pid,tid,expected,actor="admin"):
        with self.db.tx() as s:
            p=self.project(s,pid,actor,True);t=s.get(Task,tid)
            require(t is not None and t.project_id==pid and t.state=="succeeded","task_not_ready","Successful task required",409)
            require(t.revision==expected==p.revision,"stale_candidate","Candidate belongs to another revision; review and merge manually",409)
            require(t.kind in ("concepts","script","bible","storyboard"),"wrong_task","Only text-stage results can be applied")
            c=dict(self.revision(s,p).content)
            field={"storyboard":"shots"}.get(t.kind,t.kind)
            c[field]=t.result["data"]
            self.save_revision(s,p,c,expected)
            log(s,pid,"candidate.applied",{"task_id":tid,"revision":p.revision},actor)
        return self.snapshot(pid,actor)

    def cancel(self,pid,tid,actor="admin"):
        with self.db.tx() as s:
            self.project(s,pid,actor,True);t=s.get(Task,tid)
            require(t is not None and t.project_id==pid,"not_found","Task not found",404)
            if t.state in ("succeeded","failed","cancelled"):
                return public(t)
            t.cancel_requested=True
            if t.state=="queued":
                t.state="cancelled"
                if t.budget_id and t.fee_state=="reserved":
                    b=s.scalar(select(Budget).where(Budget.id==t.budget_id).with_for_update())
                    b.used_micros-=t.cap_micros;t.fee_state="released"
            log(s,pid,"task.cancel_requested",{"task_id":tid,"remote_may_still_run":bool(t.remote)},actor)
            return public(t)

    def settle(self,pid,tid,amount,evidence,actor="admin"):
        require(isinstance(amount,int) and 0<=amount<=10**12 and len(evidence.strip())>=5,"settlement_invalid","Supply non-negative micro-unit amount and billing evidence")
        with self.db.tx() as s:
            self.project(s,pid,actor,True);t=s.get(Task,tid)
            require(t is not None and t.project_id==pid and t.budget_id,"not_found","Paid task not found",404)
            require(t.state in ("succeeded","failed","cancelled","reconciling"),"not_terminal","Wait for task completion or reconcile")
            require(t.fee_state in ("reserved","uncertain"),"already_settled","Settlement is immutable",409)
            b=s.scalar(select(Budget).where(Budget.id==t.budget_id).with_for_update())
            b.used_micros+=amount-t.cap_micros;t.settled_micros=amount;t.fee_state="settled"
            log(s,pid,"cost.settled",{"task_id":tid,"amount_micros":amount,"evidence":evidence[:1000],"over_cap":amount>t.cap_micros},actor)
            return public(t)
