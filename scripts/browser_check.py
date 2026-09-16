"""Browser smoke test; requires a running offline demo server and Playwright.

Uses synthetic text only. Output screenshots are test evidence, not production
assets. The login password is read from MJ_TEST_PASSWORD, never source control.
"""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

out=Path(os.getenv('MJ_CHECK_DIR','checks'));out.mkdir(parents=True,exist_ok=True)
errors=[]
bridge_mode=os.getenv('MJ_BROWSER_BRIDGE')=='1'
if bridge_mode:
    # Offline test transport: no browser network access. This does not change
    # application code or disable browser/administrator security policies.
    from fastapi.testclient import TestClient
    from mj.app import create_app
    from mj.config import Settings
    from mj.worker import Worker
    state=out/'backend';state.mkdir(exist_ok=True)
    app=create_app(Settings(database_url=f'sqlite:///{state.resolve()}/test.db',data_dir=state,mode='test',admin_password=os.environ['MJ_TEST_PASSWORD']))
    testclient=TestClient(app);testclient.__enter__()
    def bridge(path,method,body,headers):
        r=testclient.request(method,path,content=body,headers=headers)
        return {'status':r.status_code,'body':r.text,'headers':dict(r.headers)}
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1440,'height':1050},device_scale_factor=1)
    page.set_default_timeout(8000)
    page.on('pageerror',lambda e:errors.append(str(e)))
    if bridge_mode:
        page.expose_function('mjTestBridge',bridge)
        page.set_content('<!doctype html><html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><div id="toast" role="status" hidden></div><dialog id="modal"></dialog></body></html>')
        page.add_style_tag(content=(Path(__file__).parents[1]/'mj/web/style.css').read_text())
        page.add_script_tag(content='window.fetch=async (path,opts={})=>{const r=await window.mjTestBridge(path,opts.method||"GET",opts.body||null,opts.headers||{});return new Response(r.body,{status:r.status,headers:r.headers})}')
        page.add_script_tag(type='module',content=(Path(__file__).parents[1]/'mj/web/app.js').read_text())
    else:
        page.goto(os.getenv('MJ_TEST_URL','http://127.0.0.1:8080'))
    page.get_by_label('管理员密码',exact=True).fill(os.environ['MJ_TEST_PASSWORD'])
    page.get_by_role('button',name='登录工作台 →').click()
    page.get_by_role('heading',name='你的原创漫剧工作空间').wait_for()
    page.get_by_role('button',name='创建离线演示').click()
    page.get_by_role('heading',name='先让故事成立').wait_for()
    page.locator('textarea[name="action_0"]').fill('灯突然熄灭，阿澄停下动作并抬头。')
    page.get_by_role('button',name='保存新版本',exact=True).click()
    page.locator('#toast').filter(has_text='已保存为版本').wait_for()
    page.evaluate('window.scrollTo(0,0)')
    page.screenshot(path=str(out/'story.png'),full_page=False)
    page.locator('button[data-page="bible"]').click()
    page.get_by_role('heading',name='给故事一个稳定的世界').wait_for()
    page.locator('input[name="characters_name_0"]').fill('阿澄')
    page.get_by_role('button',name='保存世界设定').click()
    page.locator('button[data-page="shots"]').click()
    page.get_by_role('heading',name='逐镜头，搭建你的故事').wait_for()
    page.screenshot(path=str(out/'shots.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.evaluate('window.scrollTo(0,0)')
    page.screenshot(path=str(out/'mobile.png'),full_page=False)
    overflow=page.evaluate('document.documentElement.scrollWidth > innerWidth')
    assert not overflow,'Mobile document overflows viewport'
    page.set_viewport_size({'width':1440,'height':1050})
    page.locator('button[data-page="render"]').click()
    page.get_by_role('button',name='完整预演',exact=True).click()
    page.locator('#modal button[type=submit]').click()
    page.get_by_role('heading',name='让每一次生成有迹可循').wait_for()
    page.screenshot(path=str(out/'tasks.png'),full_page=True)
    result={'transport':'in-process TestClient bridge' if bridge_mode else 'live HTTP','page_errors':errors,'mobile_overflow':overflow,'tested':['login','create synthetic project','edit and save script','save bible','shot cards','mobile layout','enqueue full 120-second animatic']}
    assert not errors,errors
    (out/'browser-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    browser.close()
if bridge_mode:
    testclient.__exit__(None,None,None)
