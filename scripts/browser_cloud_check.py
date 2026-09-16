"""Offline Chromium component test, no server/model requests; not an HTTP/CSP test."""
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from mj.app import create_app
from mj.config import Settings

ROOT=Path(__file__).resolve().parents[1]
out=Path(os.environ.get('MJ_CHECK_DIR','/tmp/mj-cloud-components'));out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    password=secrets.token_urlsafe(24)
    settings=Settings(database_url=f'sqlite:///{temp}/test.db',data_dir=Path(temp),mode='test',
                      admin_password=password,provider_file=str(ROOT/'providers.cloud.example.json'))
    with TestClient(create_app(settings)) as tc,sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':1440,'height':1000});page.set_default_timeout(8000)
        errors=[];requests=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:requests.append(r.url))
        def bridge(path,method,body,headers):
            r=tc.request(method,path,content=body,headers=headers)
            return {'status':r.status_code,'body':r.text,'headers':dict(r.headers)}
        page.expose_function('mjTestBridge',bridge)
        page.set_content('<html lang="zh-CN"><body><div id="app"></div><div id="toast" hidden></div><dialog id="modal"></dialog></body></html>')
        page.add_style_tag(content=(ROOT/'mj/web/style.css').read_text())
        page.add_script_tag(content='window.testResponses=[];window.fetch=async(path,opts={})=>{const r=await window.mjTestBridge(path,opts.method||"GET",opts.body||null,opts.headers||{});window.testResponses.push({path,status:r.status,body:r.body});return new Response(r.body,{status:r.status,headers:r.headers})}')
        # Inline the two known local modules only for this isolated component test.
        source=(ROOT/'mj/web/cloud-ui.js').read_text().replace('export const ','const ').replace('export function ','function ')
        source+='\n'+(ROOT/'mj/web/app.js').read_text().split('\n',1)[1]
        page.add_script_tag(type='module',content=source)
        page.get_by_label('管理员密码',exact=True).fill(password);page.get_by_role('button',name='登录工作台 →').click()
        page.get_by_role('button',name='创建离线演示',exact=True).click();page.get_by_role('heading',name='先让故事成立').wait_for()
        page.locator('button[data-page="bible"]').click();page.locator('button[data-act="generate"][data-kind="image"]').first.click()
        page.locator('select[name="provider"]').select_option('qwen-images')
        assert page.locator('[name="qwen_ref"]').count()==3
        assert page.locator('[name="qwen_width"]').input_value()=='720'
        assert page.locator('[name="qwen_height"]').input_value()=='1280'
        page.locator('[name="dry"]').check();page.locator('#modal button[type="submit"]').click()
        page.wait_for_function('window.testResponses.some(x=>x.path.endsWith("/tasks")&&x.status===202)')
        result=json.loads(page.evaluate('window.testResponses.findLast(x=>x.path.endsWith("/tasks")).body'))
        assert result['external_calls']==0 and result['quote']['api_plan']['protocol']=='qwen_image'
        page.locator('button[data-page="shots"]').click()
        page.locator('button[data-act="generate"][data-kind="video"]').first.click()
        page.locator('[name="provider"]').select_option('h3-video')
        assert page.locator('[name="h3_first"]').is_visible()
        page.locator('[name="h3_mode"]').select_option('first_last');assert page.locator('[name="h3_last"]').is_visible()
        page.locator('[name="h3_mode"]').select_option('reference');assert page.locator('[name="h3_reference_ids"]').is_visible()
        page.screenshot(path=str(out/'h3-references.png'))
        page.set_viewport_size({'width':390,'height':844});assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        page.screenshot(path=str(out/'mobile.png'));page.set_viewport_size({'width':1440,'height':1000})
        page.keyboard.press('Escape')
        page.locator('button[data-act="generate"][data-kind="tts"]').first.click()
        page.locator('[name="provider"]').select_option('minimax-narration')
        assert page.locator('[name="tts_voice"]').is_visible() and page.locator('[name="voice"]').is_disabled()
        assert page.locator('[name="tts_speed"]').input_value()=='1'
        page.screenshot(path=str(out/'narration.png'));page.keyboard.press('Escape')
        page.locator('button[data-page="settings"]').click();assert page.get_by_role('button',name='检查本地配置').count()==3
        assert not errors,errors
        assert not requests,requests
        report={'transport':'isolated in-process component bridge; live HTTP/TLS/CSP NOT verified','page_errors':errors,
                'external_requests':0,'paid_calls':0,'mobile_overflow':False,
                'tested':['Qwen form and project-aspect defaults','image dry-run','H3 exclusive input forms','independent voice form','per-provider type selection','settings checks','390px layout']}
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False));browser.close()
