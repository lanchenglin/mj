"""Database-backed worker with leases and fenced completion.

SQLite is serialized offline demo mode; PostgreSQL uses SKIP LOCKED. A stale
worker may leave an orphan CAS blob but cannot change the active candidate.
"""
from __future__ import annotations

import threading
import time
import httpx
from sqlalchemy import select, update
from .db import Asset, Budget, Task, log, public
from . import media, providers


class Worker:
    def __init__(self,studio):
        self.studio=studio;self.db=studio.db;self.stop_event=threading.Event()

    def recover(self):
        with self.db.tx() as s:
            tasks=s.scalars(select(Task).where(Task.state.in_(["dispatching","running"]),Task.lease_until<time.time()).with_for_update(skip_locked=True)).all()
            for t in tasks:
                local=t.provider in ("mock","local")
                t.state="cancelled" if local and t.cancel_requested else ("queued" if local else ("submitted" if t.remote else "reconciling"))
                if not local and t.fee_state=="reserved":
                    t.fee_state="uncertain"
                t.fence+=1;t.lease_until=0
                log(s,t.project_id,"task.recovered",{"task_id":t.id,"state":t.state})

    def claim(self):
        self.recover()
        with self.db.tx() as s:
            t=s.scalar(select(Task).where(Task.state.in_(["queued","submitted"]),Task.next_run<=time.time()).order_by(Task.created_at).with_for_update(skip_locked=True).limit(1))
            if not t:
                return None
            if t.state == "queued" and t.budget_id:
                b=s.scalar(select(Budget).where(Budget.id==t.budget_id).with_for_update())
                if not self.studio.settings.external_enabled or b.revoked or b.expires <= time.time():
                    t.state="cancelled";t.cancel_requested=True
                    if t.fee_state=="reserved":
                        b.used_micros-=t.cap_micros;t.fee_state="released"
                    log(s,t.project_id,"task.authorization_expired",{"task_id":t.id})
                    return None
            t.fence+=1;t.lease_until=time.time()+self.studio.settings.lease_seconds
            t.state="running" if t.provider in ("mock","local") or t.remote else "dispatching"
            s.flush();log(s,t.project_id,"task.claimed",{"task_id":t.id,"fence":t.fence,"state":t.state})
            return public(t)

    def heartbeat(self,tid,fence,done):
        while not done.wait(max(1,self.studio.settings.lease_seconds//3)):
            with self.db.tx() as s:
                s.execute(update(Task).where(Task.id==tid,Task.fence==fence,Task.state.in_(["running","dispatching"])).values(lease_until=time.time()+self.studio.settings.lease_seconds))

    def finish(self,job,result=None,error=None,unknown=False):
        asset_data=None
        if result and "path" in result:
            h,ext,info=self.studio.store.ingest(result["path"])
            asset_data=(h,ext,info)
        with self.db.tx() as s:
            t=s.scalar(select(Task).where(Task.id==job["id"]).with_for_update())
            if t.fence!=job["fence"] or t.state not in ("running","dispatching"):
                return False
            t.lease_until=0
            if error:
                t.error=str(error)[:1200]
                t.state="reconciling" if unknown else ("cancelled" if t.cancel_requested else "failed")
            elif result.get("remote"):
                t.remote=result["remote"];t.state="submitted";t.next_run=time.time()+3
            elif result.get("pending"):
                t.state="submitted";t.next_run=time.time()+5
            else:
                if asset_data:
                    h,ext,info=asset_data
                    a=Asset(project_id=t.project_id,kind=info["kind"],name=f"{t.kind}-{t.id[:8]}{ext}",blob_hash=h,extension=ext,info=info,mock=result["mock"],task_id=t.id,motion=result.get("motion","none"),rights="Generated under project authorization" if not result["mock"] else "MJ synthetic engineering fixture")
                    s.add(a);s.flush()
                    t.result={"asset_id":a.id,"mock":a.mock,"late_cancelled":t.cancel_requested}
                else:
                    t.result=result
                t.state="cancelled" if t.cancel_requested else "succeeded"
            if t.budget_id and t.state in ("succeeded","cancelled","failed","reconciling") and t.fee_state=="reserved":
                t.fee_state="uncertain"  # Generated output is not an invoice.
            log(s,t.project_id,"task.updated",{"task_id":t.id,"state":t.state,"fee_state":t.fee_state})
            return True

    def once(self):
        job=self.claim()
        if not job:
            return False
        done=threading.Event()
        heart=threading.Thread(target=self.heartbeat,args=(job["id"],job["fence"],done),daemon=True);heart.start()
        directory=self.studio.store.root/"jobs"/f"{job['id']}-{job['fence']}"
        try:
            if job["provider"]=="mock":
                result=providers.mock(job["snapshot"],directory)
            elif job["provider"]=="local":
                result=media.render(job["snapshot"],self.studio.store,directory,self.studio.settings.render_timeout)
            else:
                if not self.studio.settings.external_enabled:
                    raise ValueError("External services disabled; no request sent")
                adapter=providers.External(job["snapshot"],self.studio.store)
                result=adapter.poll(job["remote"],directory) if job["remote"] else adapter.submit(directory)
            self.finish(job,result=result)
        except providers.UnknownSubmission as exc:
            self.finish(job,error=exc,unknown=True)
        except Exception as exc:
            # GET/poll can be retried with the same ID; submission errors without
            # an ID are held for human reconciliation, never sent again.
            external=job["provider"] not in ("mock","local")
            transient = isinstance(exc,httpx.TransportError) or getattr(exc,"retryable",False)
            if external and job["remote"] and transient and time.time()-job["created_at"]<3600:
                self.finish(job,result={"pending":True})
            else:
                self.finish(job,error=exc,unknown=external and (not job["remote"] or transient))
        finally:
            done.set();heart.join(timeout=2)
        return True

    def run(self):
        while not self.stop_event.is_set():
            if not self.once():
                self.stop_event.wait(1)
