"""Durable worker with leases/fencing, query-only recovery and explicit uncertain cost."""
import argparse
from contextlib import contextmanager
import logging
import threading
import time
from sqlalchemy import select, update, or_
from .config import Settings
from .db import database, Project, Job, Asset, emit, now, uid
from .contracts import Plan
from .storage import Store, media_cancel
from . import providers, media

log = logging.getLogger("mj.worker")


def recover(sessions):
    with sessions.begin() as db:
        expired = db.scalars(select(Job).where(Job.state.in_(["dispatching", "running"]),
                                             Job.lease_until < now()).with_for_update(skip_locked=True)).all()
        for j in expired:
            if j.remote_id:
                j.state = "submitted"
            elif j.spec["provider_config"]["type"] in {"local", "mock"}:
                j.state = "queued"
            else:
                j.state, j.error = "reconciling", "Worker中断且提交状态未知；禁止自动重发。"
                j.charge_state = "uncertain"
            j.lease_until = 0
            j.fence += 1
            emit(db, j.project_id, "task.recovered", {"job_id": j.id, "state": j.state})


def claim(sessions, settings):
    with sessions.begin() as db:
        j = db.scalar(select(Job).where(Job.state.in_(["queued", "submitted"]), Job.lease_until < now())
                      .order_by(Job.created, Job.id).with_for_update(skip_locked=True).limit(1))
        if not j: return None
        if j.state == "queued" and j.spec["provider_config"]["type"] not in {"mock", "local"}:
            p = db.get(Project, j.project_id)
            policy = p.budget_policy
            if (not settings.paid_enabled or not policy.get("outbound") or policy.get("expires", 0) <= now()
                    or j.provider not in policy.get("providers", []) or j.kind not in policy.get("kinds", [])):
                j.state, j.error = "cancelled", "生成前授权已失效，未向供应商提交。"
                if j.charge_state == "reserved":
                    db.execute(update(Project).where(Project.id == p.id).values(budget_used=Project.budget_used-j.reserved))
                    j.charge_state = "released"
                emit(db, j.project_id, "task.authorization_expired", {"job_id": j.id})
                return None
        old_fence = j.fence
        next_state = "running" if j.remote_id else "dispatching"
        ok = db.execute(update(Job).where(Job.id == j.id, Job.fence == old_fence,
                                         Job.state == j.state, Job.lease_until < now())
                        .values(state=next_state, fence=old_fence+1, lease_until=now()+settings.lease_seconds)).rowcount
        if ok != 1: return None
        db.refresh(j)
        return {"id": j.id, "project_id": j.project_id, "kind": j.kind, "spec": j.spec,
                "remote_id": j.remote_id, "fence": j.fence, "cancel_requested": j.cancel_requested}


@contextmanager
def heartbeat(sessions, job, settings):
    done = threading.Event()
    def loop():
        while not done.wait(max(1, settings.lease_seconds // 3)):
            with sessions.begin() as db:
                count = db.execute(update(Job).where(Job.id == job["id"], Job.fence == job["fence"],
                    Job.state.in_(["dispatching", "running"]))
                    .values(lease_until=now()+settings.lease_seconds)).rowcount
                if count != 1: return
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try: yield
    finally:
        done.set(); thread.join(timeout=2)


def process_one(sessions, settings):
    recover(sessions)
    job = claim(sessions, settings)
    if not job: return False
    result, error, unknown = None, "", False
    store = Store(settings)
    def cancelled():
        with sessions() as db:
            current = db.get(Job, job["id"])
            return not current or current.fence != job["fence"] or current.cancel_requested
    context = media_cancel.set(cancelled)
    try:
        with heartbeat(sessions, job, settings):
            if job["kind"] == "render":
                result = media.render(Plan.model_validate(job["spec"]["plan"]), job["spec"]["assets"],
                                      store, f"{job['id']}-{job['fence']}", job["spec"]["request"]["mode"],
                                      job["spec"].get("cost_snapshot"))
            elif job["remote_id"]:
                result = providers.query(job["spec"], job["remote_id"], cancel=job["cancel_requested"])
            else:
                result = providers.submit(job["spec"], job["kind"], store)
            if result and "bytes" in result:
                payload = result.pop("bytes")
                result["media"] = store.ingest(payload)
                expected = {"image": "image", "video": "video", "tts": "audio"}.get(job["kind"])
                if expected and result["media"]["kind"] != expected:
                    raise ValueError("供应商返回了错误的媒体类型。")
    except providers.SubmissionUnknown as exc:
        error, unknown = str(exc), True
    except Exception as exc:
        log.warning("Task %s failed (%s)", job["id"], type(exc).__name__)
        error = str(exc) if isinstance(exc, (ValueError, providers.ProviderError)) else f"{type(exc).__name__}：任务失败，请检查配置或服务端日志。"
        # For a known remote prediction, a failed download/query can be resumed without creating a new one.
        unknown = bool(job["remote_id"])
    finally:
        media_cancel.reset(context)
    with sessions.begin() as db:
        j = db.scalar(select(Job).where(Job.id == job["id"]).with_for_update())
        if j.fence != job["fence"] or j.state not in {"dispatching", "running"}:
            return True  # A stale worker must not activate, settle, or overwrite anything.
        j.lease_until = 0
        if error:
            j.error = error[:1000]
            j.state = "reconciling" if unknown else "cancelled" if j.cancel_requested else "failed"
        elif result.get("remote_id"):
            j.remote_id, j.state = result["remote_id"], "submitted"
            j.lease_until = now() + 3
        elif result.get("pending"):
            j.state, j.lease_until = "submitted", now() + 3
        elif result.get("terminal"):
            j.state = result["terminal"]
        else:
            if "media" in result:
                info = result.pop("media")
                asset = Asset(id=uid(), project_id=j.project_id, **info, mock=result.get("mock", False),
                              rights="模型生成；发布前请核对供应商条款。" if not result.get("mock") else "离线测试占位素材，不是正式创作。",
                              shot_id=j.spec["request"].get("shot_id"), fingerprint=j.spec.get("fingerprint"))
                db.add(asset); db.flush()
                result["asset_id"] = asset.id
            j.result = result
            j.state = "cancelled" if j.cancel_requested else "succeeded"
        if j.reserved and j.state not in {"queued", "submitted", "running", "dispatching"} and j.charge_state == "reserved":
            j.charge_state = "uncertain"  # Failure/cancel does not mean free; user reconciles actual invoice.
        emit(db, j.project_id, "task.updated", {"job_id": j.id, "state": j.state, "charge_state": j.charge_state})
    return True


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings(); engine, sessions = database(settings)
    logging.basicConfig(level=logging.INFO)
    while True:
        worked = process_one(sessions, settings)
        if args.once: break
        if not worked: time.sleep(1)

if __name__ == "__main__": main()
