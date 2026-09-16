from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from .config import Settings
from .contracts import ApprovalInput, BudgetInput, NewProject, RevisionInput, TaskInput, Workspace, fingerprint, gate_hash
from .db import Approval, Asset, AuditEvent, Budget, Project, Revision, SessionToken, Task, log, public
from .domain import Problem, Studio, require
from .media import export_bundle


def sha(value:str):
    return hashlib.sha256(value.encode()).hexdigest()


def task_public(t):
    d=public(t)
    d.pop("snapshot");d.pop("remote")
    d["remote_request_id"]=(t.remote or {}).get("request_id")
    if d.get("result"):
        d["result"]={k:v for k,v in d["result"].items() if k!="directory"}
    return d


class Login(BaseModel):
    password:str=Field(min_length=1,max_length=512)


class Choice(BaseModel):
    expected_revision:int
    shot_id:str
    track:str


class Review(BaseModel):
    status:str
    motion:str="none"
    note:str=Field(min_length=2,max_length=2000)


class Expected(BaseModel):
    expected_revision:int


class Settlement(BaseModel):
    amount_micros:int=Field(ge=0,le=10**12)
    evidence:str=Field(min_length=5,max_length=1000)


def create_app(settings:Settings|None=None) -> FastAPI:
    studio=Studio(settings or Settings.env())
    salt=secrets.token_bytes(16)
    password_hash=hashlib.scrypt(studio.settings.admin_password.encode(),salt=salt,n=16384,r=8,p=1)
    attempts={}

    @asynccontextmanager
    async def lifespan(app):
        if studio.settings.mode in ("test","demo"):
            studio.db.init()
        yield
        studio.db.engine.dispose()

    app=FastAPI(title="MJ Original Drama Studio",version="0.4.0",lifespan=lifespan)
    app.state.studio=studio

    @app.exception_handler(Problem)
    async def problem(_,exc):
        return JSONResponse(status_code=exc.status,content={"error":{"code":exc.code,"message":exc.message}})

    @app.exception_handler(ValueError)
    async def invalid(_,exc):
        return JSONResponse(status_code=422,content={"error":{"code":"invalid_input","message":str(exc)[:1000]}})

    @app.middleware("http")
    async def security(request:Request,call_next):
        if request.method not in ("GET","HEAD","OPTIONS"):
            origin=request.headers.get("origin")
            expected=studio.settings.public_origin or f"{request.url.scheme}://{request.url.netloc}"
            if origin and origin!=expected:
                return JSONResponse(status_code=403,content={"error":{"code":"origin_rejected","message":"Cross-origin mutation rejected"}})
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["X-Frame-Options"]="DENY"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["Content-Security-Policy"]="default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"]="no-store"
        return response

    def actor(request:Request):
        token=request.cookies.get("mj_session","")
        with studio.db.sessions() as s:
            session=s.get(SessionToken,sha(token))
            if not session or session.expires<time.time():
                raise Problem("login_required","Please sign in",401)
            if request.method not in ("GET","HEAD","OPTIONS"):
                csrf=request.headers.get("x-csrf-token","")
                require(hmac.compare_digest(session.csrf_hash,sha(csrf)),"csrf_invalid","Invalid CSRF token",403)
            return session.actor

    def project_task(s,pid,tid,user):
        studio.project(s,pid,user)
        t=s.get(Task,tid)
        require(t is not None and t.project_id==pid,"not_found","Task not found",404)
        return t

    @app.get("/health")
    def health():
        return {"status":"ok","version":"0.4.0","mode":studio.settings.mode}

    @app.post("/api/v1/login")
    def login(req:Login,request:Request):
        ip=request.client.host if request.client else "unknown"
        now=time.time();times=[t for t in attempts.get(ip,[]) if now-t<60]
        if len(times)>=8:
            raise Problem("rate_limited","Too many attempts; retry after a minute",429)
        attempts[ip]=times+[now]
        candidate=hashlib.scrypt(req.password.encode(),salt=salt,n=16384,r=8,p=1)
        require(hmac.compare_digest(candidate,password_hash),"invalid_login","Invalid credentials",401)
        token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        with studio.db.tx() as s:
            s.add(SessionToken(token_hash=sha(token),csrf_hash=sha(csrf),expires=now+studio.settings.session_seconds))
        response=JSONResponse({"actor":"admin","csrf":csrf})
        response.set_cookie("mj_session",token,httponly=True,samesite="strict",secure=studio.settings.secure_cookie,max_age=studio.settings.session_seconds)
        response.set_cookie("mj_csrf",csrf,httponly=False,samesite="strict",secure=studio.settings.secure_cookie,max_age=studio.settings.session_seconds)
        return response

    @app.get("/api/v1/session")
    def session(request:Request,user=Depends(actor)):
        return {"actor":user,"csrf":request.cookies.get("mj_csrf","")}

    @app.post("/api/v1/logout")
    def logout(request:Request,user=Depends(actor)):
        with studio.db.tx() as s:
            t=s.get(SessionToken,sha(request.cookies.get("mj_session","")))
            if t:s.delete(t)
        response=JSONResponse({"ok":True});response.delete_cookie("mj_session");response.delete_cookie("mj_csrf")
        return response

    @app.get("/api/v1/settings")
    def settings_view(user=Depends(actor)):
        import os
        configs=[]
        from .cloud_api import TYPES, public_config
        for name,cfg in studio.providers.items():
            if cfg["type"] in TYPES:
                configs.append(public_config(name,cfg))
                continue
            if cfg["type"]=="comfyui":
                configs.append({"id":name,"type":"comfyui","key_present":cfg.get("auth","none")=="none" or bool(os.getenv(cfg.get("key_env",""))),"auth_required":cfg.get("auth","none")!="none","quality_verified":False,"default_workflow":cfg["default_workflow"],"default_size":cfg.get("default_size",[768,1024]),"workflows":[{"id":wid,"title":w["manifest"].get("title",wid),"mode":w["manifest"]["mode"],"references":w["manifest"]["reference_count"],"sha256":w["sha256"]} for wid,w in cfg["workflows"].items()]})
                continue
            kinds=["video"] if cfg["type"]=="fal_queue" else (["concepts","script","bible","storyboard"] if cfg.get("models",{}).get("text") else [])+[k for k in ("image","tts") if cfg.get("models",{}).get(k)]
            configs.append({"id":name,"type":cfg["type"],"kinds":kinds,"models":cfg.get("models",{}),"endpoint":cfg.get("endpoint"),"currency":cfg.get("currency"),"reserve_micros":cfg.get("reserve_micros",{}),"key_present":bool(os.getenv(cfg["key_env"])) ,"quality_verified":False})
        return {"mode":studio.settings.mode,"external_enabled":studio.settings.external_enabled,"comfyui_enabled":studio.settings.comfyui_enabled,"providers":configs,"mock_notice":"MOCK 只验证工程链路，不代表真实模型画质与配音。"}

    @app.post("/api/v1/providers/{name}/check")
    def check_provider(name:str,user=Depends(actor)):
        cfg=studio.providers.get(name)
        from .cloud_api import TYPES, validate_config, public_config
        if cfg and cfg.get("type") in TYPES:
            validate_config(cfg)
            return {"ok":public_config(name,cfg)["key_present"],"scope":"local_configuration_only","network_calls":0,"generation_calls":0,"quality_verified":False,"message":"Config checked locally; no paid capability probe or account verification performed"}
        require(cfg is not None and cfg.get("type")=="comfyui","provider_unknown","Select an administrator-configured ComfyUI provider")
        require(studio.settings.comfyui_enabled,"comfyui_disabled","Enable MJ_COMFYUI_ENABLED before contacting the GPU",403)
        from .comfyui import check_server
        result=check_server(cfg)
        with studio.db.tx() as s:
            log(s,None,"comfy.checked",{"provider":name,"generation_calls":0,"ok":result["ok"]},user)
        return result

    @app.get("/api/v1/projects")
    def projects(user=Depends(actor)):
        with studio.db.sessions() as s:
            return [public(p) for p in s.scalars(select(Project).where(Project.owner==user).order_by(Project.created_at.desc())).all()]

    @app.post("/api/v1/projects",status_code=201)
    def create(req:NewProject,user=Depends(actor)):
        return studio.snapshot(studio.create_project(req,user),user)

    @app.get("/api/v1/projects/{pid}")
    def get_project(pid:str,user=Depends(actor)):
        return studio.snapshot(pid,user)

    @app.put("/api/v1/projects/{pid}/workspace")
    def save(pid:str,req:RevisionInput,user=Depends(actor)):
        return studio.update(pid,req,user)

    @app.post("/api/v1/projects/{pid}/impact")
    def impact(pid:str,req:RevisionInput,user=Depends(actor)):
        old=studio.snapshot(pid,user)["content"];new=req.content.model_dump()
        oldshots={s["id"]:s for s in old["shots"]};newshots={s["id"]:s for s in new["shots"]}
        changed=[key for key in oldshots.keys()|newshots.keys() if oldshots.get(key)!=newshots.get(key)]
        return {"changed_shots":sorted(changed),"bible_changed":old["bible"]!=new["bible"],"script_changed":old["script"]!=new["script"],"subtitle_only":old|{"subtitles":new["subtitles"]}==new,"gates_invalidated":[g for g in ("G1","G2","G3","G4") if gate_hash(g,old)!=gate_hash(g,new)],"external_calls":0}

    @app.get("/api/v1/projects/{pid}/history")
    def history(pid:str,user=Depends(actor)):
        with studio.db.sessions() as s:
            studio.project(s,pid,user)
            return [public(r) for r in s.scalars(select(Revision).where(Revision.project_id==pid).order_by(Revision.number.desc())).all()]

    @app.post("/api/v1/projects/{pid}/restore/{number}")
    def restore(pid:str,number:int,req:Expected,user=Depends(actor)):
        with studio.db.tx() as s:
            p=studio.project(s,pid,user,True)
            rev=s.scalar(select(Revision).where(Revision.project_id==pid,Revision.number==number))
            require(rev is not None,"not_found","Revision not found",404)
            studio.save_revision(s,p,rev.content,req.expected_revision)
        return studio.snapshot(pid,user)

    @app.post("/api/v1/projects/{pid}/approvals")
    def approve(pid:str,req:ApprovalInput,user=Depends(actor)):
        return studio.approve(pid,req,user)

    @app.get("/api/v1/projects/{pid}/budgets")
    def budgets(pid:str,user=Depends(actor)):
        with studio.db.sessions() as s:
            studio.project(s,pid,user)
            return [public(b) for b in s.scalars(select(Budget).where(Budget.project_id==pid)).all()]

    @app.post("/api/v1/projects/{pid}/budgets")
    def budget(pid:str,req:BudgetInput,user=Depends(actor)):
        return studio.authorize(pid,req,user)

    @app.post("/api/v1/projects/{pid}/budgets/{bid}/revoke")
    def revoke(pid:str,bid:str,user=Depends(actor)):
        with studio.db.tx() as s:
            studio.project(s,pid,user,True);b=s.get(Budget,bid)
            require(b is not None and b.project_id==pid,"not_found","Budget not found",404)
            b.revoked=True;log(s,pid,"budget.revoked",{"id":bid},user)
            return public(b)

    @app.get("/api/v1/projects/{pid}/assets")
    def assets(pid:str,user=Depends(actor)):
        with studio.db.sessions() as s:
            studio.project(s,pid,user)
            return [public(a) for a in s.scalars(select(Asset).where(Asset.project_id==pid).order_by(Asset.created_at.desc())).all()]

    @app.post("/api/v1/projects/{pid}/assets",status_code=201)
    async def upload(pid:str,file:UploadFile=File(...),rights:str=Form(...),user=Depends(actor)):
        with studio.db.sessions() as s:studio.project(s,pid,user)
        ext=Path(file.filename or "upload").suffix.lower()
        from .media import MIMES
        require(ext in MIMES,"file_type","Unsupported media extension")
        with tempfile.TemporaryDirectory(dir=studio.settings.data_dir) as tmp:
            path=Path(tmp)/("source"+ext);size=0
            with path.open("wb") as f:
                while chunk:=await file.read(1024*1024):
                    size+=len(chunk)
                    require(size<=studio.settings.max_upload_bytes,"file_too_large","Upload exceeds configured size limit",413)
                    f.write(chunk)
            return await asyncio.to_thread(studio.add_asset,pid,path,file.filename or "upload",rights,user)

    @app.get("/api/v1/projects/{pid}/assets/{aid}/file")
    def asset_file(pid:str,aid:str,user=Depends(actor)):
        with studio.db.sessions() as s:
            studio.project(s,pid,user);a=s.get(Asset,aid)
            require(a is not None and a.project_id==pid,"not_found","Asset not found",404)
            return FileResponse(studio.store.asset_path(public(a)),media_type=a.info["mime"])

    @app.post("/api/v1/projects/{pid}/assets/{aid}/review")
    def review(pid:str,aid:str,req:Review,user=Depends(actor)):
        return studio.review_asset(pid,aid,req.status,req.motion,req.note,user)

    @app.post("/api/v1/projects/{pid}/assets/{aid}/select")
    def choose(pid:str,aid:str,req:Choice,user=Depends(actor)):
        require(req.track in ("visual","audio","reference"),"invalid_track","Unknown track")
        with studio.db.tx() as s:
            p=studio.project(s,pid,user,True);a=s.get(Asset,aid)
            require(a is not None and a.project_id==pid and a.review=="accepted","asset_unreviewed","Review asset before selection")
            c=json.loads(json.dumps(studio.revision(s,p).content))
            shot=next((x for x in c["shots"] if x["id"]==req.shot_id),None)
            require(shot is not None,"not_found","Shot not found",404)
            if req.track=="visual":
                require(a.kind in ("image","video"),"wrong_media","Expected visual media")
                if shot["motion"] in ("subject_action","lip_sync"):
                    require(not a.mock and a.kind=="video" and a.motion==shot["motion"],"motion_unmet","Candidate does not satisfy motion contract")
                shot["video_asset"]=aid
            elif req.track=="audio":
                require(a.kind=="audio","wrong_media","Expected audio")
                shot["audio_asset"]=aid
            else:
                require(a.kind=="image","wrong_media","Expected image reference")
                shot["reference_ids"]=list(dict.fromkeys(shot["reference_ids"]+[aid]))
            studio.save_revision(s,p,c,req.expected_revision)
        return studio.snapshot(pid,user)

    @app.get("/api/v1/projects/{pid}/tasks")
    def tasks(pid:str,user=Depends(actor)):
        with studio.db.sessions() as s:
            studio.project(s,pid,user)
            return [task_public(t) for t in s.scalars(select(Task).where(Task.project_id==pid).order_by(Task.created_at.desc())).all()]

    @app.post("/api/v1/projects/{pid}/tasks",status_code=202)
    def enqueue(pid:str,req:TaskInput,idempotency_key:str=Header(default=""),user=Depends(actor)):
        result=studio.enqueue(pid,req,idempotency_key,user)
        result.pop("snapshot",None);result.pop("remote",None)
        return result

    @app.post("/api/v1/projects/{pid}/tasks/{tid}/apply")
    def apply(pid:str,tid:str,req:Expected,user=Depends(actor)):
        return studio.apply_task(pid,tid,req.expected_revision,user)

    @app.post("/api/v1/projects/{pid}/tasks/{tid}/cancel")
    def cancel(pid:str,tid:str,user=Depends(actor)):
        t=studio.cancel(pid,tid,user);t.pop("snapshot",None);t.pop("remote",None);return t

    @app.post("/api/v1/projects/{pid}/tasks/{tid}/reconcile")
    def reconcile(pid:str,tid:str,user=Depends(actor)):
        with studio.db.tx() as s:
            t=project_task(s,pid,tid,user)
            require(t.state=="reconciling","reconcile_required","Only an uncertain task can be reconciled",409)
            if not t.remote and t.snapshot.get("provider_config",{}).get("type")=="comfyui":
                t.remote={"lookup":True,"client_id":t.snapshot["comfy"]["client_id"]}
            require(t.remote,"remote_id_required","No recoverable remote ID. Check provider billing; this operation will not resubmit",409)
            t.remote={**t.remote,"resume_at":time.time()}
            t.state="submitted";t.next_run=0;log(s,pid,"task.reconcile",{"task_id":tid},user)
            return task_public(t)

    @app.post("/api/v1/projects/{pid}/tasks/{tid}/settle")
    def settle(pid:str,tid:str,req:Settlement,user=Depends(actor)):
        t=studio.settle(pid,tid,req.amount_micros,req.evidence,user);t.pop("snapshot",None);t.pop("remote",None);return t

    @app.get("/api/v1/projects/{pid}/tasks/{tid}/files/{name}")
    def output(pid:str,tid:str,name:str,user=Depends(actor)):
        with studio.db.sessions() as s:
            t=project_task(s,pid,tid,user)
            require(t.result and name in t.result.get("files",[]),"not_found","Output not found",404)
            directory=studio.store.root/"jobs"/t.result["directory"]
            require(directory.resolve().is_relative_to((studio.store.root/"jobs").resolve()),"invalid_path","Invalid output")
            mime="video/mp4" if name.endswith("mp4") else ("audio/wav" if name.endswith("wav") else "application/octet-stream")
            return FileResponse(directory/name,media_type=mime)

    @app.post("/api/v1/projects/{pid}/tasks/{tid}/export")
    def export(pid:str,tid:str,user=Depends(actor)):
        with studio.db.sessions() as s:
            t=project_task(s,pid,tid,user);p=studio.project(s,pid,user)
            require(t.state=="succeeded" and t.kind in ("render","animatic"),"render_required","Successful render required")
            mock=t.result.get("mock",True)
            if not mock:
                approval=s.scalar(select(Approval).where(Approval.project_id==pid,Approval.gate=="G4",Approval.task_id==tid))
                require(approval is not None and studio.approval_current(s,approval,studio.revision(s,p).content),"final_approval_required","G4 final review required",409)
            all_tasks=s.scalars(select(Task).where(Task.project_id==pid)).all()
            cost={"tasks":[{"id":x.id,"provider":x.provider,"currency":s.get(Budget,x.budget_id).currency if x.budget_id else None,"usage":(x.result or {}).get("usage",{}),"fee_state":x.fee_state,"reserved_micros":x.cap_micros,"settled_micros":x.settled_micros} for x in all_tasks],"local_compute_cost":"not measured","note":"Unknown costs are not zero"}
            path=export_bundle(studio.store.root/"jobs"/t.result["directory"],t.result,cost,t.snapshot["assets"],studio.store)
        return FileResponse(path,media_type="application/zip",filename="mj-preview-project.zip" if mock else "mj-release-project.zip")

    @app.get("/api/v1/projects/{pid}/events")
    async def events(pid:str,request:Request,user=Depends(actor)):
        with studio.db.sessions() as s:studio.project(s,pid,user)
        try:cursor=max(0,int(request.headers.get("last-event-id","0")))
        except ValueError:cursor=0
        async def stream():
            nonlocal cursor
            for _ in range(60):
                if await request.is_disconnected():break
                with studio.db.sessions() as s:
                    rows=s.scalars(select(AuditEvent).where(AuditEvent.project_id==pid,AuditEvent.id>cursor).order_by(AuditEvent.id).limit(100)).all()
                    for e in rows:
                        cursor=e.id
                        yield f"id: {e.id}\ndata: {json.dumps(public(e),ensure_ascii=False)}\n\n"
                if not rows:yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(),media_type="text/event-stream",headers={"X-Accel-Buffering":"no"})

    app.mount("/",StaticFiles(directory=Path(__file__).parent/"web",html=True),name="web")
    return app
