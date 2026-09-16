"""Live HTTP browser smoke for ComfyUI configuration UI. No GPU/model calls.

Starts a temporary SQLite test server with remote processing OFF. Browser
uses actual HTTP/cookies/CSP (not a TestClient bridge). Requires Chromium.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
out = Path(os.getenv('MJ_CHECK_DIR', '/tmp/mj-comfy-browser')).resolve()
out.mkdir(parents=True, exist_ok=True)
bridge_mode=os.getenv('MJ_BROWSER_BRIDGE')=='1'
with tempfile.TemporaryDirectory(prefix='mj-browser-') as temp:
    state = Path(temp)
    password = secrets.token_urlsafe(24)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'MJ_MODE':'test', 'MJ_EXTERNAL_ENABLED':'false',
           'MJ_COMFYUI_ENABLED':'false', 'MJ_ADMIN_PASSWORD':password,
           'MJ_DATABASE_URL':f'sqlite:///{state}/test.db', 'MJ_DATA_DIR':str(state),
           'MJ_PROVIDERS_FILE':str(ROOT/'providers.comfyui.example.json'),
           'MJ_PUBLIC_ORIGIN':origin, 'MJ_SECURE_COOKIE':'false'}
    with (out/'server.log').open('w') as log:
        process = subprocess.Popen([sys.executable,'-m','uvicorn','mj.app:create_app','--factory',
                                   '--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
        try:
            with httpx.Client(trust_env=False,timeout=1) as client:
                for _ in range(100):
                    try:
                        if client.get(origin+'/health').status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.1)
                else:raise RuntimeError('Temporary test server failed to start')
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1050})
                page.set_default_timeout(8000)
                errors=[];requests=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda request:requests.append(request.url))
                if bridge_mode:
                    # Separate in-process component test. Does not contact blocked URLs
                    # or change browser/network policy; does not validate HTTP/CSP.
                    from fastapi.testclient import TestClient
                    from mj.app import create_app
                    from mj.config import Settings
                    tc=TestClient(create_app(Settings(database_url=f'sqlite:///{state}/bridge.db',
                        data_dir=state/'bridge',mode='test',admin_password=password,
                        provider_file=str(ROOT/'providers.comfyui.example.json'))))
                    tc.__enter__()
                    def bridge(path,method,body,headers):
                        r=tc.request(method,path,content=body,headers=headers)
                        return {'status':r.status_code,'body':r.text,'headers':dict(r.headers)}
                    page.expose_function('mjTestBridge',bridge)
                    page.set_content('<html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><div id="toast" role="status" hidden></div><dialog id="modal"></dialog></body></html>')
                    page.add_style_tag(content=(ROOT/'mj/web/style.css').read_text())
                    page.add_script_tag(content='window.testResponses=[];window.fetch=async(path,opts={})=>{const r=await window.mjTestBridge(path,opts.method||"GET",opts.body||null,opts.headers||{});window.testResponses.push({path,method:opts.method||"GET",status:r.status,body:r.body});return new Response(r.body,{status:r.status,headers:r.headers})}')
                    page.add_script_tag(type='module',content=(ROOT/'mj/web/app.js').read_text())
                else:
                    response=page.goto(origin)
                    assert "script-src 'self'" in response.headers['content-security-policy']
                page.get_by_label('管理员密码',exact=True).fill(password)
                page.get_by_role('button',name='登录工作台 →').click()
                page.get_by_role('button',name='创建离线演示',exact=True).click()
                page.get_by_role('heading',name='先让故事成立').wait_for()
                page.locator('button[data-page="bible"]').click()
                page.locator('button[data-act="generate"][data-kind="image"]').first.click()
                page.locator('select[name="provider"]').select_option('comfy-gpu')
                page.get_by_label('工作流（服务端已固定版本）').wait_for()
                assert page.locator('[name="budget_id"]').is_disabled()
                page.locator('[name="comfy_workflow"]').select_option('sdxl-reference')
                assert page.locator('[name="comfy_ref"]').count()==1
                page.locator('[name="comfy_workflow"]').select_option('sdxl-inpaint')
                assert page.locator('[name="comfy_mask"]').is_visible()
                page.screenshot(path=str(out/'comfy-inpaint.png'))
                page.set_viewport_size({'width':390,'height':844})
                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                page.screenshot(path=str(out/'comfy-mobile.png'))
                page.set_viewport_size({'width':1440,'height':1050})
                page.locator('[name="comfy_workflow"]').select_option('sdxl-text')
                page.locator('[name="comfy_width"]').fill('256')
                page.locator('[name="comfy_height"]').fill('256')
                page.locator('[name="dry"]').check()
                if bridge_mode:
                    page.locator('#modal button[type="submit"]').click()
                    page.wait_for_function('window.testResponses.some(r=>r.path.endsWith("/tasks")&&r.method==="POST")')
                    response=page.evaluate('window.testResponses.findLast(r=>r.path.endsWith("/tasks")&&r.method==="POST")')
                    result=json.loads(response['body'])
                    assert response['status'] in (200,202),result
                else:
                    with page.expect_response(lambda r:'/tasks' in r.url and r.request.method=='POST') as task_response:
                        page.locator('#modal button[type="submit"]').click()
                    result=task_response.value.json()
                    assert task_response.value.status in (200,202),result
                assert result['dry_run'] and result['quote']['self_hosted']
                page.locator('button[data-page="settings"]').click()
                page.get_by_role('button',name='检查节点/模型').wait_for()
                assert all(url.startswith(origin) or url.startswith('data:') for url in requests)
                report={'transport':'in-process component bridge (HTTP/CSP NOT verified)' if bridge_mode else 'live HTTP','page_errors':errors,'external_requests':0,
                        'gpu_generation_calls':0,'comfyui_enabled':False,'mobile_overflow':False,
                        'tested':['login through TestClient' if bridge_mode else 'login with actual cookie/CSRF','configured workflows','reference selection fields',
                                  'inpaint mask fields','hidden cloud budget','text dry-run','provider status','mobile layout']}
                assert not errors,errors
                (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(report,ensure_ascii=False))
                browser.close()
                if bridge_mode:tc.__exit__(None,None,None)
        finally:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
