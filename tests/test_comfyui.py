"""Comfy protocol/authorization regressions. No GPU or external services are used."""
import copy
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from PIL import Image

from conftest import create, save, workspace
from mj.comfy_transport import Client, ComfyFailure, validate_endpoint
from mj.comfy_workflows import BUILTIN_NODES, bind, check_workflow, load_config
from mj.comfyui import ComfyUI, check_server
from mj.config import Settings
from mj.contracts import ApprovalInput, ImageOptions, TaskInput, fingerprint
from mj.db import Asset, Task
from mj.domain import Problem
from mj.providers import UnknownSubmission
from mj.worker import Worker

ROOT = Path(__file__).resolve().parents[1]


def raw_cfg():
    cfg = json.loads((ROOT / 'providers.comfyui.example.json').read_text())['comfy-gpu']
    cfg['default_size'] = [256, 256]
    return cfg


def config():
    return load_config(raw_cfg(), ROOT)


@pytest.fixture
def comfy(env):
    client, studio = env
    p = create(client)
    p = save(client, p, workspace(p))
    studio.approve(p['id'], ApprovalInput(gate='G1', expected_revision=p['revision'], note='人工确认故事'))
    # Fault injection only: no production endpoint is contacted by this fixture.
    studio.providers = {'gpu': config()}
    studio.settings.comfyui_enabled = True
    return client, studio, p


def image_req(p, workflow='sdxl-text', **kwargs):
    opts = dict(workflow=workflow, width=256, height=256, seed=12, allow_remote_processing=True)
    opts.update(kwargs.pop('options', {}))
    return TaskInput(kind='image', provider=kwargs.pop('provider', 'gpu'), expected_revision=p['revision'], image_options=ImageOptions(**opts), **kwargs)


def upload(studio, p, tmp_path, name='ref.png', size=(256,256), mock=False, accepted=True):
    path = tmp_path / name
    Image.new('RGB', size, 'white').save(path)
    a = studio.add_asset(p['id'], path, name, 'Original synthetic test fixture', mock=mock)
    if accepted:
        studio.review_asset(p['id'], a['id'], 'accepted', 'none', '人工确认参考')
    return a['id']


class FakeComfy:
    def __init__(self, cfg):
        self.cfg=cfg; self.posts=[]; self.uploads=[]; self.gets=[]; self.body=None
        self.lost=False; self.done=False; self.error=False; self.missing=False
        self.rid='a777-test-prompt'; self.bad_output=False
        self.objects={n['class_type']:{'input':{'required':{}}} for d in cfg['workflows'].values() for n in d['manifest']['graph'].values()}

    def entry(self):
        return [1, self.rid, self.body['prompt'], self.body['extra_data'], ['9']]

    def history(self):
        if not self.body or not self.done or self.missing:
            return {}
        folder=self.body['prompt']['9']['inputs']['filename_prefix'].rsplit('/',1)[0]
        return {self.rid:{'prompt':self.entry(), 'status':{'status_str':'error' if self.error else 'success','completed':True},
                 'outputs':{'9':{'images':[{'filename':'../../secret.png' if self.bad_output else 'image_00001_.png','subfolder':folder,'type':'output'}]}}}}

    def request(self, method, path, **kw):
        if method=='POST' and path=='/upload/image':
            self.uploads.append(kw)
            return json.dumps({'name':kw['files']['image'][0],'subfolder':kw['data']['subfolder'],'type':'input'}).encode()
        if method=='POST' and path=='/prompt':
            self.posts.append(kw);self.body=kw['json']
            if self.lost:raise httpx.ReadTimeout('lost acknowledgement')
            return json.dumps({'prompt_id':self.rid,'number':1,'node_errors':{}}).encode()
        self.gets.append(path)
        if path=='/object_info':return json.dumps(self.objects).encode()
        if path=='/system_stats':return b'{}'
        if path.startswith('/history'):return json.dumps(self.history()).encode()
        if path=='/queue':return json.dumps({'queue_running':[self.entry()] if self.body and not self.done and not self.missing else [],'queue_pending':[]}).encode()
        if path=='/view':
            buf=io.BytesIO();Image.new('RGB',(256,256),'blue').save(buf,'PNG');return buf.getvalue()
        raise AssertionError((method,path))


def fake_service(monkeypatch, studio):
    fake=FakeComfy(studio.providers['gpu'])
    monkeypatch.setattr(Client,'request',lambda self,*a,**kw:fake.request(*a,**kw))
    return fake


def ready(studio, tid):
    with studio.db.tx() as s:s.get(Task,tid).next_run=0


def test_all_three_templates_are_valid():
    cfg=config()
    assert len(cfg['workflows'])==3
    for w in cfg['workflows'].values():check_workflow(w['manifest'],BUILTIN_NODES)


@pytest.mark.parametrize('url', ['file:///etc/passwd','http://user:pass@10.10.10.20:8188','http://10.10.10.20:8188/path','http://10.10.10.20:8188/?token=a','http://evil.test'])
def test_unapproved_endpoints_rejected(url):
    cfg=raw_cfg();cfg['base_url']=url
    with pytest.raises(ValueError):validate_endpoint(cfg)


@pytest.mark.parametrize('ip',['169.254.169.254','0.0.0.0','224.0.0.1','::ffff:127.0.0.1'])
def test_unsafe_ips_are_never_approved(ip):
    cfg=raw_cfg();cfg['allowed_ips']=[ip]
    with pytest.raises(ValueError):validate_endpoint(cfg)


def test_private_requires_separate_opt_in():
    cfg=raw_cfg();cfg['allow_private']=False
    with pytest.raises(ValueError):validate_endpoint(cfg)
    cfg=raw_cfg();cfg['allow_insecure_http']=False
    with pytest.raises(ValueError):validate_endpoint(cfg)


def test_public_http_and_unauthenticated_https_forbidden():
    cfg=raw_cfg();cfg.update(base_url='http://8.8.8.8',hosts=['8.8.8.8'],allowed_ips=['8.8.8.8'])
    with pytest.raises(ValueError):validate_endpoint(cfg)
    cfg['base_url']='https://8.8.8.8'
    with pytest.raises(ValueError):validate_endpoint(cfg)
    cfg.update(auth='bearer',key_env='MJ_GPU_TOKEN')
    validate_endpoint(cfg)


def test_local_model_flag_required():
    cfg=raw_cfg();cfg['local_models_only']=False
    with pytest.raises(ValueError):load_config(cfg,ROOT)


def test_workflow_path_cannot_escape(tmp_path):
    cfg=raw_cfg();cfg.update(workflow_dir=str(tmp_path),workflows={'x':'../elsewhere.json'},default_workflow='x')
    with pytest.raises(ValueError):load_config(cfg,ROOT)


def test_canvas_export_not_api_graph():
    w=config()['workflows']['sdxl-text']['manifest'];w['graph']={'nodes':[],'links':[]}
    with pytest.raises(ValueError):check_workflow(w,BUILTIN_NODES)


def test_unknown_node_and_binding_are_blocked():
    w=config()['workflows']['sdxl-text']['manifest'];w['graph']['4']['class_type']='RunArbitraryCode'
    with pytest.raises(ValueError):check_workflow(w,BUILTIN_NODES)
    w=config()['workflows']['sdxl-text']['manifest'];w['bindings']['prompt']=[['3','latent_image']]
    with pytest.raises(ValueError):check_workflow(w,BUILTIN_NODES)


def test_demo_is_offline_by_default(tmp_path):
    with pytest.raises(ValueError):Settings(mode='demo',data_dir=tmp_path,admin_password='a'*20,comfyui_enabled=True).validate()


def test_api_users_cannot_submit_graphs():
    with pytest.raises(ValueError):ImageOptions(workflow='sdxl-text',graph={'unsafe':'node'})


def test_dry_run_freezes_only_locally(comfy,monkeypatch):
    _,studio,p=comfy
    before=fingerprint(studio.providers)
    monkeypatch.setattr(Client,'request',lambda *a,**kw:pytest.fail('dry-run contacted GPU'))
    r=studio.enqueue(p['id'],image_req(p,dry_run=True),'comfy-dry-run')
    assert r['external_calls']==0 and not r['quote']['requires_paid_authorization']
    assert r['quote']['self_hosted']
    assert fingerprint(studio.providers)==before
    with studio.db.sessions() as s:assert not s.query(Task).all()


def test_explicit_remote_consent_and_master_switch(comfy):
    _,studio,p=comfy
    with pytest.raises(Problem) as e:studio.enqueue(p['id'],image_req(p,options={'allow_remote_processing':False}),'comfy-no-consent')
    assert e.value.code=='remote_consent_required'
    studio.settings.comfyui_enabled=False
    with pytest.raises(Problem) as e:studio.enqueue(p['id'],image_req(p),'comfy-disabled')
    assert e.value.code=='comfyui_disabled'


def test_story_approval_required(env):
    client,studio=env;p=create(client);studio.providers={'gpu':config()};studio.settings.comfyui_enabled=True
    with pytest.raises(Problem) as e:studio.enqueue(p['id'],image_req(p),'comfy-no-gate')
    assert e.value.code=='approval_required'


def test_no_fake_api_invoice(comfy):
    _,studio,p=comfy
    with pytest.raises(Problem):studio.enqueue(p['id'],image_req(p,cap_micros=10),'comfy-fake-bill')
    t=studio.enqueue(p['id'],image_req(p),'comfy-real-queue')
    assert t['fee_state']=='unmetered' and t['budget_id'] is None


def test_options_cannot_be_silently_ignored(comfy):
    _,studio,p=comfy
    with pytest.raises(Problem):studio.enqueue(p['id'],image_req(p,provider='mock'),'comfy-mock-options')


@pytest.mark.parametrize('dims',[(258,256),(2048,2048)])
def test_unsupported_sizes_blocked(comfy,dims):
    _,studio,p=comfy
    with pytest.raises(ValueError):studio.enqueue(p['id'],image_req(p,options={'width':dims[0],'height':dims[1]}),'comfy-bad-size')


def test_reference_missing_and_unreviewed_rejected(comfy,tmp_path):
    _,studio,p=comfy
    with pytest.raises(ValueError):studio.enqueue(p['id'],image_req(p,'sdxl-reference'),'comfy-no-reference')
    aid=upload(studio,p,tmp_path,accepted=False)
    with pytest.raises(ValueError):studio.enqueue(p['id'],image_req(p,'sdxl-reference',reference_ids=[aid]),'comfy-unreviewed')


def test_mock_reference_not_a_production_reference(comfy,tmp_path):
    _,studio,p=comfy;aid=upload(studio,p,tmp_path,mock=True)
    with pytest.raises(ValueError):studio.enqueue(p['id'],image_req(p,'sdxl-reference',reference_ids=[aid]),'comfy-mock-reference')


def test_cross_project_image_blocked(comfy,tmp_path):
    client,studio,p=comfy;other=create(client);aid=upload(studio,other,tmp_path)
    with pytest.raises(Problem) as e:studio.enqueue(p['id'],image_req(p,'sdxl-reference',reference_ids=[aid]),'comfy-cross-project')
    assert e.value.code=='asset_not_found'


def test_mask_dimensions_must_match(comfy,tmp_path):
    _,studio,p=comfy;a=upload(studio,p,tmp_path);b=upload(studio,p,tmp_path,'mask.png',size=(512,256))
    with pytest.raises(ValueError):studio.enqueue(p['id'],image_req(p,'sdxl-inpaint',reference_ids=[a],options={'mask_asset_id':b}),'comfy-mask-size')


def test_reference_order_and_frozen_seed(comfy,tmp_path):
    _,studio,p=comfy;a=upload(studio,p,tmp_path)
    t=studio.enqueue(p['id'],image_req(p,'sdxl-reference',reference_ids=[a],options={'seed':None}),'comfy-seed-stable')
    again=studio.enqueue(p['id'],image_req(p,'sdxl-reference',reference_ids=[a],options={'seed':None}),'comfy-seed-stable')
    assert t['id']==again['id'] and t['input_hash']==again['input_hash']
    assert t['snapshot']['reference_order']==[a]
    graph=bind(t['snapshot'],{'ref_0':'uploaded-reference.png'})
    assert graph['10']['inputs']['image']=='uploaded-reference.png'
    assert graph['3']['inputs']['seed']==t['snapshot']['comfy']['values']['seed']
    assert len(studio.providers['gpu']['workflows'])==3


def test_idempotency_conflict_for_image_change(comfy):
    _,studio,p=comfy;studio.enqueue(p['id'],image_req(p),'comfy-same-request')
    with pytest.raises(Problem) as e:studio.enqueue(p['id'],image_req(p,options={'seed':13}),'comfy-same-request')
    assert e.value.status==409


def test_submit_poll_download_review_boundary(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio)
    t=studio.enqueue(p['id'],image_req(p),'comfy-happy-path');worker=Worker(studio)
    assert worker.once();assert len(fake.posts)==1
    with studio.db.sessions() as s:assert s.get(Task,t['id']).remote['request_id']==fake.rid
    fake.done=True;ready(studio,t['id']);assert worker.once()
    with studio.db.sessions() as s:
        task=s.get(Task,t['id']);asset=s.get(Asset,task.result['asset_id'])
        assert task.state=='succeeded' and task.fee_state=='unmetered'
        assert not asset.mock and asset.review=='pending' and asset.motion=='none'
        assert task.result['provenance']['workflow_hash']==task.snapshot['comfy']['workflow_hash']
    assert studio.snapshot(p['id'])['revision']==p['revision']
    assert len(fake.posts)==1


def test_reference_and_mask_uploaded_before_prompt(comfy,tmp_path,monkeypatch):
    _,studio,p=comfy;a=upload(studio,p,tmp_path);b=upload(studio,p,tmp_path,'mask.png');fake=fake_service(monkeypatch,studio)
    t=studio.enqueue(p['id'],image_req(p,'sdxl-inpaint',reference_ids=[a],options={'mask_asset_id':b}),'comfy-inpaint')
    Worker(studio).once()
    assert len(fake.uploads)==2 and len(fake.posts)==1
    graph=fake.posts[0]['json']['prompt']
    assert graph['10']['inputs']['image']!=graph['13']['inputs']['image']
    with Image.open(io.BytesIO(fake.uploads[1]['files']['image'][1])) as im:assert im.mode=='L'


def test_unknown_ack_never_resubmits_and_lookup_recovers(comfy,monkeypatch):
    client,studio,p=comfy;fake=fake_service(monkeypatch,studio);fake.lost=True
    t=studio.enqueue(p['id'],image_req(p),'comfy-lost-ack');worker=Worker(studio);worker.once()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).state=='reconciling'
    assert not worker.once() and len(fake.posts)==1
    response=client.post(f"/api/v1/projects/{p['id']}/tasks/{t['id']}/reconcile")
    assert response.status_code==200;worker.once()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).remote['request_id']==fake.rid
    fake.done=True;ready(studio,t['id']);worker.once()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).state=='succeeded'
    assert len(fake.posts)==1


def test_missing_history_is_not_safe_to_resubmit(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio)
    t=studio.enqueue(p['id'],image_req(p),'comfy-lost-history');worker=Worker(studio);worker.once()
    fake.missing=True;ready(studio,t['id']);worker.once()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).state=='reconciling'
    assert not worker.once();assert len(fake.posts)==1


def test_node_preflight_prevents_generation(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio);fake.objects={}
    t=studio.enqueue(p['id'],image_req(p),'comfy-node-missing');Worker(studio).once()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).state=='failed'
    assert not fake.posts and not fake.uploads


def test_model_check_prevents_generation(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio)
    fake.objects['CheckpointLoaderSimple']={'input':{'required':{'ckpt_name':[['different.safetensors']]}}}
    t=studio.enqueue(p['id'],image_req(p),'comfy-model-missing');Worker(studio).once()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).state=='failed'
    assert not fake.posts


def test_output_path_injection_rejected(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio);fake.bad_output=True
    t=studio.enqueue(p['id'],image_req(p),'comfy-evil-output');w=Worker(studio);w.once();fake.done=True;ready(studio,t['id']);w.once()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).state=='failed'
    assert '/view' not in fake.gets


def test_remote_failure_does_not_activate_candidate(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio);fake.error=True
    t=studio.enqueue(p['id'],image_req(p),'comfy-remote-failure');w=Worker(studio);w.once();fake.done=True;ready(studio,t['id']);w.once()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).state=='failed' and not s.query(Asset).all()


def test_cancel_running_preserves_late_output_without_global_interrupt(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio)
    t=studio.enqueue(p['id'],image_req(p),'comfy-cancel-late');w=Worker(studio);w.once()
    studio.cancel(p['id'],t['id']);fake.done=True;ready(studio,t['id']);w.once()
    with studio.db.sessions() as s:
        job=s.get(Task,t['id']);assert job.state=='cancelled' and job.result['late_cancelled']
        assert s.get(Asset,job.result['asset_id']).review=='pending'
    assert len(fake.posts)==1  # Client also has no allowed /interrupt route.


def test_concurrent_workers_respect_gpu_slots(comfy):
    _,studio,p=comfy
    for i in range(4):studio.enqueue(p['id'],image_req(p),f'comfy-concurrent-{i}')
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed=list(pool.map(lambda _:Worker(studio).claim(),range(4)))
    assert sum(x is not None for x in claimed)==1


def test_expired_lease_without_id_requires_lookup(comfy):
    _,studio,p=comfy;t=studio.enqueue(p['id'],image_req(p),'comfy-expired-lease');w=Worker(studio);job=w.claim()
    with studio.db.tx() as s:s.get(Task,t['id']).lease_until=0
    w.recover()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).state=='reconciling'
    assert not w.finish(job,result={'pending':True})


def test_server_check_is_get_only_and_not_quality_validation(comfy,monkeypatch):
    client,studio,p=comfy;fake=fake_service(monkeypatch,studio)
    result=client.post('/api/v1/providers/gpu/check')
    assert result.status_code==200 and result.json()['ok'] and not result.json()['quality_verified']
    assert not fake.posts and not fake.uploads


def test_config_metadata_contains_no_origin_graph_or_token(comfy):
    client,studio,p=comfy
    response=client.get('/api/v1/settings');cfg=response.json()['providers'][0]
    assert len(cfg['workflows'])==3 and cfg['type']=='comfyui'
    assert 'base_url' not in cfg and 'graph' not in str(cfg) and 'key_env' not in cfg


def test_transport_pins_dns_and_preserves_sni(monkeypatch):
    cfg=raw_cfg();cfg.update(base_url='https://gpu.example',hosts=['gpu.example'],auth='bearer',key_env='MJ_TEST_COMFY')
    monkeypatch.setenv('MJ_TEST_COMFY','not-a-real-secret');seen=[]
    monkeypatch.setattr('socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('10.10.10.20',443))])
    def handler(request):
        seen.append(request);return httpx.Response(200,json={})
    monkeypatch.setattr(httpx,'HTTPTransport',lambda **kw:httpx.MockTransport(handler))
    assert Client(cfg).request('GET','/queue')==b'{}'
    assert seen[0].url.host=='10.10.10.20'
    assert seen[0].headers['Host']=='gpu.example:443'
    assert seen[0].extensions['sni_hostname']=='gpu.example'


def test_dns_change_and_redirect_are_rejected(monkeypatch):
    cfg=raw_cfg();cfg.update(base_url='https://gpu.example',hosts=['gpu.example'],auth='bearer',key_env='MJ_TEST_COMFY')
    monkeypatch.setenv('MJ_TEST_COMFY','not-a-real-secret')
    monkeypatch.setattr('socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('169.254.169.254',443))])
    with pytest.raises(ComfyFailure):Client(cfg).request('GET','/queue')
    monkeypatch.setattr('socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('10.10.10.20',443))])
    seen=[]
    def redirect(req):seen.append(req);return httpx.Response(302,headers={'Location':'http://169.254.169.254/'})
    monkeypatch.setattr(httpx,'HTTPTransport',lambda **kw:httpx.MockTransport(redirect))
    with pytest.raises(ComfyFailure):Client(cfg).request('GET','/queue')
    assert len(seen)==1


def test_global_interrupt_and_clear_queue_never_allowed():
    for method,path in [('POST','/interrupt'),('POST','/queue'),('POST','/history'),('GET','/view?filename=secret')]:
        with pytest.raises(ComfyFailure):Client(raw_cfg()).request(method,path)


@pytest.mark.parametrize('ip',['168.63.129.16','fd00:ec2::254'])
def test_metadata_special_addresses_are_forbidden(ip):
    cfg=raw_cfg();cfg['allowed_ips']=[ip]
    with pytest.raises(ValueError):validate_endpoint(cfg)


def test_remote_timeout_holds_slot_but_completed_result_can_be_recovered(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio)
    task=studio.enqueue(p['id'],image_req(p),'comfy-remote-timeout');w=Worker(studio);w.once()
    with studio.db.tx() as s:
        t=s.get(Task,task['id']);t.remote={**t.remote,'submitted_at':time.time()-2000};t.next_run=0
    w.once()
    with studio.db.sessions() as s:assert s.get(Task,task['id']).state=='reconciling'
    fake.done=True
    with studio.db.tx() as s:s.get(Task,task['id']).state='submitted';s.get(Task,task['id']).next_run=0
    w.once()
    with studio.db.sessions() as s:
        t=s.get(Task,task['id']);assert t.state=='succeeded' and t.error==''
        assert s.get(Asset,t.result['asset_id']).info['generation']['engine']=='comfyui'
    assert len(fake.posts)==1


def test_transient_poll_exhaustion_preserves_unknown_state(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio)
    task=studio.enqueue(p['id'],image_req(p),'comfy-transient-timeout');w=Worker(studio);w.once()
    original=Client.request
    def failing(self,method,path,**kw):
        if path.startswith('/history'):raise ComfyFailure('Comfy temporarily unavailable',retryable=True)
        return original(self,method,path,**kw)
    monkeypatch.setattr(Client,'request',failing)
    with studio.db.tx() as s:s.get(Task,task['id']).created_at=time.time()-4000;s.get(Task,task['id']).next_run=0
    w.once()
    with studio.db.sessions() as s:assert s.get(Task,task['id']).state=='reconciling'
    assert len(fake.posts)==1


def test_revoked_access_does_not_resubmit(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio)
    task=studio.enqueue(p['id'],image_req(p),'comfy-access-revoked');w=Worker(studio);w.once()
    studio.settings.comfyui_enabled=False;ready(studio,task['id']);w.once()
    with studio.db.sessions() as s:assert s.get(Task,task['id']).state=='reconciling'
    assert len(fake.posts)==1


def test_bad_image_response_is_not_candidate(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio)
    original=Client.request
    def broken(self,method,path,**kw):
        return b'<html>not image</html>' if path=='/view' else original(self,method,path,**kw)
    monkeypatch.setattr(Client,'request',broken)
    task=studio.enqueue(p['id'],image_req(p),'comfy-invalid-media');w=Worker(studio);w.once();fake.done=True;ready(studio,task['id']);w.once()
    with studio.db.sessions() as s:assert s.get(Task,task['id']).state=='failed' and not s.query(Asset).all()


def test_zero_lookup_matches_does_not_submit(comfy,monkeypatch):
    _,studio,p=comfy;fake=fake_service(monkeypatch,studio)
    task=studio.enqueue(p['id'],image_req(p),'comfy-empty-lookup')
    adapter=ComfyUI(task['snapshot'],studio.store)
    with pytest.raises(UnknownSubmission):adapter.lookup()
    assert not fake.posts


def test_live_http_protocol_round_trip(comfy,tmp_path):
    """Actual TCP HTTP server, synthetic image; no Comfy installation or GPU."""
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from urllib.parse import urlsplit,parse_qs
    import threading
    from email.parser import BytesParser
    from email.policy import default
    _,studio,p=comfy
    cfg=raw_cfg();fake=FakeComfy(config())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,body):
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            u=urlsplit(self.path);self.reply(fake.request('GET',u.path,params=parse_qs(u.query)))
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            if self.path=='/prompt':
                data=fake.request('POST',self.path,json=json.loads(body))
            else:
                parsed=BytesParser(policy=default).parsebytes(('Content-Type: '+self.headers['Content-Type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
                fields={};files={}
                for part in parsed.iter_parts():
                    name=part.get_param('name',header='content-disposition')
                    if part.get_filename():files[name]=(part.get_filename(),part.get_payload(decode=True),'image/png')
                    else:fields[name]=part.get_payload(decode=True).decode()
                data=fake.request('POST',self.path,data=fields,files=files)
            self.reply(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        cfg.update(base_url=f'http://127.0.0.1:{server.server_port}',hosts=['127.0.0.1'],allowed_ips=['127.0.0.1'],allow_loopback=True)
        studio.providers={'gpu':load_config(cfg,ROOT)}
        aid=upload(studio,p,tmp_path)
        task=studio.enqueue(p['id'],image_req(p,'sdxl-reference',reference_ids=[aid]),'comfy-live-http')
        w=Worker(studio);w.once();assert len(fake.posts)==1 and len(fake.uploads)==1
        fake.done=True;ready(studio,task['id']);w.once()
        with studio.db.sessions() as s:
            t=s.get(Task,task['id']);assert t.state=='succeeded',t.error
            a=s.get(Asset,t.result['asset_id']);assert a.review=='pending' and a.info['width']==256
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
