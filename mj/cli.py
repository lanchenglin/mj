from __future__ import annotations
import argparse
import os
import secrets
from pathlib import Path


def load_env(path:Path):
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k,v=line.split("=",1);os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))


def main():
    ap=argparse.ArgumentParser(description="MJ original micro-drama workbench")
    ap.add_argument("command",choices=["init","serve","worker","doctor","comfy-check"])
    ap.add_argument("--host",default="127.0.0.1")
    ap.add_argument("--port",type=int,default=8080)
    ap.add_argument("--with-worker",action="store_true",help="Offline demo only; production uses a separate worker")
    ap.add_argument("--once",action="store_true")
    ap.add_argument("--provider",help="Administrator-configured ComfyUI provider ID")
    args=ap.parse_args()
    if args.command=="init":
        env=Path(".env")
        if env.exists():
            raise SystemExit(".env exists; leaving it untouched")
        text="MJ_MODE=demo\nMJ_DATA_DIR=data\nMJ_DATABASE_URL=sqlite:///./data/mj-demo.db\nMJ_EXTERNAL_ENABLED=false\nMJ_ADMIN_PASSWORD="+secrets.token_urlsafe(24)+"\n"
        fd=os.open(env,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w") as f:f.write(text)
        print("Created .env with a random administrator password. Read MJ_ADMIN_PASSWORD locally. Never commit this file.")
        return
    load_env(Path(".env"))
    from .config import Settings
    settings=Settings.env()
    if args.command=="comfy-check":
        import json
        from .comfyui import check_server
        cfg=settings.providers().get(args.provider)
        if not cfg or cfg.get("type")!="comfyui":raise SystemExit("Choose --provider with type=comfyui")
        if not settings.comfyui_enabled:raise SystemExit("Enable MJ_COMFYUI_ENABLED before contacting the GPU")
        result=check_server(cfg)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        raise SystemExit(0 if result["ok"] else 1)
    if args.command=="doctor":
        import shutil
        for name in ("ffmpeg","ffprobe","fc-match"):
            print(name,shutil.which(name) or "MISSING")
        print("mode:",settings.mode,"external enabled:",settings.external_enabled)
        return
    if args.command=="worker":
        from .domain import Studio
        from .worker import Worker
        studio=Studio(settings)
        if settings.mode!="production":studio.db.init()
        worker=Worker(studio)
        if args.once:worker.once()
        else:worker.run()
        return
    from .app import create_app
    import uvicorn
    app=create_app(settings)
    worker=None
    if args.with_worker:
        if settings.mode!="demo":raise SystemExit("--with-worker is for offline demo only")
        import threading
        from .worker import Worker
        app.state.studio.db.init();worker=Worker(app.state.studio)
        threading.Thread(target=worker.run,daemon=True).start()
    try:
        uvicorn.run(app,host=args.host,port=args.port,proxy_headers=False)
    finally:
        if worker:worker.stop_event.set()


if __name__=="__main__":main()
