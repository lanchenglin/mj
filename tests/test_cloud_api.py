"""Native protocol/worker tests. All network calls are intercepted; no paid generation."""
import copy
import io
import json
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image
from conftest import create, save, workspace
from mj import api_transport as net
from mj.cloud_api import Native, prepare, validate_config, minimum_reserve
from mj.contracts import ApprovalInput, BudgetInput, TaskInput, VideoOptions, CloudImageOptions, fingerprint
from mj.db import Task, Budget, Asset
from mj.domain import Problem
from mj.media import Store, command, render, ffprobe
from mj.providers import UnknownSubmission
from mj.worker import Worker


def config(kind):
    ptype={'image':'qwen_image','video':'minimax_h3','tts':'minimax_tts'}[kind]
    model={'image':'qwen-image-2.0-pro','video':'MiniMax-H3','tts':'speech-2.8-hd'}[kind]
    return {'type':ptype,'base_url':'https://api.example.test','hosts':['api.example.test'],
            'download_hosts':['cdn.example.test'],'key_env':'MJ_NATIVE_TEST_KEY','models':{kind:model},
            'default_voice':'Chinese_Mandarin_TestNarrator','currency':'CNY' if kind=='image' else 'USD',
            'reserve_micros':{kind:100},'max_inflight':1,'remote_timeout_seconds':1800}


@pytest.fixture
def native(env,monkeypatch):
    client,studio=env
    p=create(client,450);p=save(client,p,workspace(p,3))
    studio.approve(p['id'],ApprovalInput(gate='G1',expected_revision=p['revision'],note='测试故事审核'))
    studio.approve(p['id'],ApprovalInput(gate='G2',expected_revision=p['revision'],note='测试分镜审核'))
    studio.providers={kind:config(kind) for kind in ('image','video','tts')}
    # Fault injection, not a production-mode bypass shipped in the app.
    studio.settings.external_enabled=True
    monkeypatch.setenv('MJ_NATIVE_TEST_KEY','synthetic-credential-not-real')
    budgets={kind:studio.authorize(p['id'],BudgetInput(currency=cfg['currency'],limit_micros=10000,per_task_micros=1000,providers=[kind],kinds=[kind],allow_reference_upload=True)) for kind,cfg in studio.providers.items()}
    return client,studio,p,budgets


def upload(studio,p,tmp_path,name='ref.png',size=(512,512),accepted=True,mock=False):
    path=tmp_path/name;Image.new('RGB',size,'navy').save(path)
    a=studio.add_asset(p['id'],path,name,'Synthetic engineering fixture, not a character',mock=mock)
    if accepted:studio.review_asset(p['id'],a['id'],'accepted','none','测试参考图审核')
    return a['id']


def req(p,budgets,kind='image',**kw):
    b={'kind':kind,'provider':kind,'budget_id':budgets[kind]['id'],'cap_micros':100,'expected_revision':p['revision']}
    if kind in ('video','tts'):b['shot_id']='SH01'
    if kind=='video':b.update(sample=True,sample_shot_ids=['SH01','SH02','SH03'],video_options={'mode':'text'})
    b.update(kw);return TaskInput(**b)


def job(studio,p,budgets,kind='image',key='native-one',**kw):
    return studio.enqueue(p['id'],req(p,budgets,kind,**kw),key)


def read_task(studio,t):
    with studio.db.sessions() as s:return copy.deepcopy(s.get(Task,t['id']).__dict__)


def due(studio,t):
    with studio.db.tx() as s:s.get(Task,t['id']).next_run=0


@pytest.mark.parametrize('kind',['image','video','tts'])
def test_configs(kind):validate_config(config(kind))


@pytest.mark.parametrize('change',[{'base_url':'http://api.example.test'},{'base_url':'https://api.example.test/v1'},
    {'key_env':'paste-secret-here'},{'download_hosts':['*.example.test']},{'max_inflight':0},
    {'reserve_micros':{'video':-1}},{'pricing':{'per_image_micros':True}},{'models':{'video':'pretend-model'}}])
def test_bad_config_blocked(change):
    cfg=config('video');cfg.update(change)
    with pytest.raises((ValueError,net.ApiFailure)):validate_config(cfg)


def test_dry_run_has_zero_requests(native,monkeypatch):
    _,s,p,b=native
    monkeypatch.setattr(net,'request',lambda *a,**k:pytest.fail('dry-run contacted network'))
    t=job(s,p,b,dry_run=True)
    assert t['external_calls']==0 and t['quote']['api_plan']['model']=='qwen-image-2.0-pro'
    assert t['quote']['minimum_reserve_micros']==100
    with s.db.sessions() as session:assert session.get(Budget,b['image']['id']).used_micros==0


def test_currencies_are_independent(native):
    _,s,p,b=native
    job(s,p,b);job(s,p,b,'video',key='usd-task')
    with s.db.sessions() as session:
        assert session.get(Budget,b['image']['id']).currency=='CNY'
        assert session.get(Budget,b['video']['id']).currency=='USD'
        assert session.get(Budget,b['image']['id']).used_micros==100
        assert session.get(Budget,b['video']['id']).used_micros==100


def test_unknown_provider_not_admitted(native):
    _,s,p,b=native
    with pytest.raises(Problem):job(s,p,b,provider='nonexistent')


def test_idempotency_and_frozen_seed(native):
    _,s,p,b=native;t=job(s,p,b);again=job(s,p,b)
    assert t['id']==again['id']
    assert t['snapshot']['api_plan']['parameters']['seed']==again['snapshot']['api_plan']['parameters']['seed']
    with pytest.raises(Problem):job(s,p,b,prompt='different request')


@pytest.mark.parametrize('accepted,mock',[(False,False),(True,True)])
def test_unapproved_references_rejected(native,tmp_path,accepted,mock):
    _,s,p,b=native;aid=upload(s,p,tmp_path,accepted=accepted,mock=mock)
    with pytest.raises(ValueError):job(s,p,b,reference_ids=[aid])


def test_cross_project_reference_rejected(native,tmp_path):
    client,s,p,b=native;p2=create(client);aid=upload(s,p2,tmp_path)
    with pytest.raises(Problem):job(s,p,b,reference_ids=[aid])


def test_qwen_order_and_payload(native,tmp_path):
    _,s,p,b=native
    ids=[upload(s,p,tmp_path,name=f'{i}.png') for i in range(3)]
    t=job(s,p,b,reference_ids=list(reversed(ids)),cloud_image_options={'use_shot_references':False,'seed':23})
    a=Native(t['snapshot'],s.store);body=a.payload()
    assert t['snapshot']['reference_order']==list(reversed(ids))
    assert body['parameters']['n']==1 and body['parameters']['seed']==23
    assert len(body['input']['messages'][0]['content'])==4
    assert body['input']['messages'][0]['content'][0]['image'].startswith('data:image/png;base64,')


def test_qwen_four_references_rejected(native,tmp_path):
    _,s,p,b=native;ids=[upload(s,p,tmp_path,name=f'{i}.png') for i in range(4)]
    with pytest.raises(ValueError):job(s,p,b,reference_ids=ids)


@pytest.mark.parametrize('options',[{'mode':'text','first_frame_id':'x'},{'mode':'first_frame','references':[{'asset_id':'a','role':'reference_image'}]},
    {'mode':'first_frame','last_frame_id':'x'},{'mode':'reference','references':[{'asset_id':'x','role':'reference_image'},{'asset_id':'x','role':'reference_audio'}]}])
def test_h3_mutually_exclusive_inputs(options):
    with pytest.raises(ValueError):VideoOptions(**options)


@pytest.mark.parametrize('mode,roles',[('first_frame',['first_frame']),('last_frame',['last_frame']),('first_last',['first_frame','last_frame']),('reference',['reference_image'])])
def test_h3_frame_modes(native,tmp_path,mode,roles):
    _,s,p,b=native;aid=upload(s,p,tmp_path);aid2=upload(s,p,tmp_path,name='other.png')
    options={'mode':mode}
    if 'first_frame' in roles:options['first_frame_id']=aid
    if 'last_frame' in roles:options['last_frame_id']=aid2
    if mode=='reference':options['references']=[{'asset_id':aid,'role':'reference_image'}]
    t=job(s,p,b,'video',video_options=options);body=Native(t['snapshot'],s.store).payload()
    assert [x['role'] for x in body['content'][1:]]==roles
    assert body['content'][1]['image_url']['url'].startswith('data:image/png;')
    assert body['duration']==5 and body['model']=='MiniMax-H3'
    assert 'audio' not in body and 'seed' not in body


@pytest.mark.parametrize('opts',[{'mode':'first_frame'},{'mode':'first_last','first_frame_id':'bad'}, {'mode':'reference'}, {'mode':'text','duration_seconds':4}])
def test_h3_invalid_inputs_pre_spend(native,opts):
    _,s,p,b=native
    with pytest.raises((ValueError,Problem)):job(s,p,b,'video',video_options=opts)
    with s.db.sessions() as session:assert session.get(Budget,b['video']['id']).used_micros==0


def test_max_2k_rejected(native):
    _,s,p,b=native;s.providers['video']['models']['video']='MiniMax-H3-Max'
    with pytest.raises(ValueError):job(s,p,b,'video',video_options={'mode':'text','resolution':'2K'})


def test_h3_sample_scope(native):
    _,s,p,b=native
    with pytest.raises(ValueError):job(s,p,b,'video',sample_shot_ids=['SH01','SH03'])


def test_qwen_ack_is_persisted_before_download(native,monkeypatch):
    _,s,p,b=native;t=job(s,p,b);calls=[]
    def fake(method,url,hosts,**kw):
        calls.append((method,url,kw))
        if method=='POST':return json.dumps({'request_id':'qwen-ack','output':{'choices':[{'message':{'content':[{'image':'https://cdn.example.test/out.png'}]}}]},'usage':{'image_count':1}}).encode()
        Image.new('RGB',(512,512),'navy').save(kw['destination']);return b''
    monkeypatch.setattr(net,'request',fake);w=Worker(s);w.once()
    state=read_task(s,t);assert state['state']=='submitted' and state['remote']['request_id']=='qwen-ack'
    assert len(calls)==1
    due(s,t);w.once();state=read_task(s,t)
    assert state['state']=='succeeded' and state['fee_state']=='uncertain'
    assert state['result']['usage']=={'image_count':1}
    assert len([c for c in calls if c[0]=='POST'])==1
    assert 'headers' not in calls[-1][2]  # No API key goes to CDN.
    with s.db.sessions() as session:assert session.get(Asset,state['result']['asset_id']).review=='pending'


def test_h3_original_id_polling(native,monkeypatch):
    _,s,p,b=native;t=job(s,p,b,'video');calls=[]
    def fake(method,url,hosts,**kw):
        calls.append((method,url))
        if method=='POST':return b'{"task_id":"h3-original"}'
        return b'{"task":{"id":"h3-original","model":"MiniMax-H3","status":"running"}}'
    monkeypatch.setattr(net,'request',fake);w=Worker(s);w.once();due(s,t);w.once()
    assert read_task(s,t)['state']=='submitted'
    assert calls==[('POST','https://api.example.test/v2/video_generation'),('GET','https://api.example.test/v2/query/video_generation/h3-original')]


def test_h3_mismatched_task_stops(native,monkeypatch):
    _,s,p,b=native;t=job(s,p,b,'video')
    monkeypatch.setattr(net,'request',lambda method,*a,**k:b'{"task_id":"original"}' if method=='POST' else b'{"task":{"id":"other","model":"MiniMax-H3","status":"succeeded"}}')
    w=Worker(s);w.once();due(s,t);w.once()
    assert read_task(s,t)['state']=='reconciling'


def test_lost_ack_never_resubmits(native,monkeypatch):
    _,s,p,b=native;t=job(s,p,b,'video');calls=[]
    def fake(*a,**kw):calls.append(1);raise UnknownSubmission('lost ack')
    monkeypatch.setattr(net,'request',fake);w=Worker(s);w.once();w.once()
    assert calls==[1] and read_task(s,t)['state']=='reconciling'


def test_unknown_task_occupies_inflight(native,monkeypatch):
    _,s,p,b=native;one=job(s,p,b,'video',key='native-one-unknown');two=job(s,p,b,'video',key='native-two-queued')
    monkeypatch.setattr(net,'request',lambda *a,**k:(_ for _ in ()).throw(UnknownSubmission('lost ack')))
    w=Worker(s);w.once();assert not w.once()
    assert read_task(s,two)['state']=='queued'


def test_usage_does_not_leak_arbitrary_response_data():
    assert Native.usage({'image_count':1,'token':'secret','url':'https://secret','total_tokens':float('nan')})=={'image_count':1}


def test_pricing_uses_generated_seconds_not_timeline_only():
    c=config('video');c['pricing']={'per_output_second_micros':100,'per_input_second_micros':25}
    assert minimum_reserve(c,{'duration':8,'input_video_seconds':2})==850


@pytest.mark.parametrize('ip',['127.0.0.1','10.0.0.1','169.254.169.254','168.63.129.16','::ffff:8.8.8.8'])
def test_transport_private_dns_rejected(monkeypatch,ip):
    monkeypatch.setattr(net.socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',(ip,443))])
    with pytest.raises(net.ApiFailure):net.request('GET','https://api.example.test/x',['api.example.test'])


def test_transport_connects_pinned_ip_and_no_secret_to_cdn(monkeypatch,tmp_path):
    seen=[]
    monkeypatch.setattr(net.socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('8.8.8.8',443))])
    actual=httpx.Client
    def handle(r):seen.append(r);return httpx.Response(200,content=b'OK')
    monkeypatch.setattr(net.httpx,'Client',lambda **kw:actual(transport=httpx.MockTransport(handle),timeout=kw['timeout']))
    assert net.request('GET','https://cdn.example.test/image?sig=synthetic',['cdn.example.test'])==b'OK'
    assert seen[0].url.host=='8.8.8.8' and seen[0].headers['host']=='cdn.example.test'
    assert seen[0].extensions['sni_hostname']=='cdn.example.test' and 'authorization' not in seen[0].headers


@pytest.mark.parametrize('status',[302,429,500])
def test_transport_no_post_retry(monkeypatch,status):
    seen=[];actual=httpx.Client
    monkeypatch.setattr(net.socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('8.8.8.8',443))])
    def handle(r):seen.append(r);return httpx.Response(status,headers={'location':'https://evil.test/'},content=b'secret error must not leak')
    monkeypatch.setattr(net.httpx,'Client',lambda **kw:actual(transport=httpx.MockTransport(handle)))
    with pytest.raises((net.ApiFailure,UnknownSubmission)) as e:net.request('POST','https://api.example.test/x',['api.example.test'],body={})
    assert len(seen)==1 and 'secret' not in str(e.value)


def test_cloud_check_is_config_only(native,monkeypatch):
    client,_,_,_=native
    monkeypatch.setattr(net,'request',lambda *a,**k:pytest.fail('must be local'))
    r=client.post('/api/v1/providers/video/check');assert r.status_code==200
    assert r.json()['network_calls']==0 and r.json()['quality_verified'] is False


def test_narration_uses_saved_text_and_freezes_voice(native):
    client,s,p,b=native;c=p['content'];c['shots'][0]['narration']='他终于把旧灯修亮了。';p=save(client,p,c)
    s.approve(p['id'],ApprovalInput(gate='G1',expected_revision=p['revision'],note='测试确认'))
    t=job(s,p,b,'tts',prompt='do not read this',speech_options={'speed':1.1})
    body=Native(t['snapshot'],s.store).payload()
    assert body['text']=='他终于把旧灯修亮了。' and 'do not' not in json.dumps(body)
    assert body['output_format']=='hex' and body['stream'] is False
    assert body['voice_setting']['speed']==1.1 and body['audio_setting']['format']=='mp3'


def test_disabled_flag_blocks_before_network(native,monkeypatch):
    _,s,p,b=native;s.settings.external_enabled=False
    monkeypatch.setattr(net,'request',lambda *a,**k:pytest.fail('no paid calls'))
    with pytest.raises(Problem):job(s,p,b)


def audio_fixture(tmp_path):
    p=tmp_path/'native-tone.mp3'
    command(['ffmpeg','-y','-v','error','-f','lavfi','-i','sine=frequency=330:sample_rate=32000:duration=0.5','-c:a','libmp3lame',str(p)])
    return p


def video_fixture(tmp_path,seconds=5):
    p=tmp_path/'native-video.mp4'
    command(['ffmpeg','-y','-v','error','-f','lavfi','-i',f'testsrc2=s=256x256:r=24:d={seconds}',
             '-f','lavfi','-i',f'sine=frequency=990:sample_rate=48000:duration={seconds}',
             '-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(p)])
    return p


def narrated(native):
    client,s,p,b=native;c=copy.deepcopy(p['content'])
    for shot in c['shots']:shot['narration']='旧灯亮了。'
    p=save(client,p,c)
    for gate in ('G1','G2'):s.approve(p['id'],ApprovalInput(gate=gate,expected_revision=p['revision'],note='测试批准'))
    return client,s,p,b


def test_tts_staged_once_then_normalized(native,tmp_path,monkeypatch):
    _,s,p,b=narrated(native);audio=audio_fixture(tmp_path);seen=[]
    def fake(method,url,hosts,**kw):
        seen.append((method,url))
        return json.dumps({'data':{'status':2,'audio':audio.read_bytes().hex()},'base_resp':{'status_code':0},'trace_id':'tts-test','extra_info':{'usage_characters':5,'audio_length':500}}).encode()
    monkeypatch.setattr(net,'request',fake)
    t=job(s,p,b,'tts');w=Worker(s);w.once();j=read_task(s,t)
    assert j['remote']['sha256'] and j['state']=='submitted'
    due(s,t);w.once();j=read_task(s,t)
    assert j['state']=='succeeded',j['error']
    with s.db.sessions() as session:
        a=session.get(Asset,j['result']['asset_id']);assert a.info['sample_rate']==48000 and a.review=='pending'
        assert a.info['generation']['narration_hash']==fingerprint('旧灯亮了。')
    assert len(seen)==1 and seen[0][0]=='POST' and j['fee_state']=='uncertain'


def test_tts_stage_missing_never_regenerates(native,tmp_path,monkeypatch):
    _,s,p,b=narrated(native);audio=audio_fixture(tmp_path);seen=[]
    def fake(*a,**k):
        seen.append(a);return json.dumps({'data':{'status':2,'audio':audio.read_bytes().hex()}}).encode()
    monkeypatch.setattr(net,'request',fake);t=job(s,p,b,'tts');w=Worker(s);w.once()
    j=read_task(s,t);(s.store.root/j['remote']['staged']).unlink();due(s,t);w.once()
    assert read_task(s,t)['state']=='reconciling' and len(seen)==1


def test_unknown_cdn_can_resume_download_without_regeneration(native,tmp_path,monkeypatch):
    client,s,p,b=native;seen=[];image=tmp_path/'image.png';Image.new('RGB',(512,512)).save(image)
    def fake(method,url,hosts,**kw):
        seen.append(method)
        if method=='POST':return json.dumps({'request_id':'image-1','output':{'choices':[{'message':{'content':[{'image':'https://new-cdn.example.test/image.png'}]}}]}}).encode()
        assert 'headers' not in kw;kw['destination'].write_bytes(image.read_bytes());return b''
    monkeypatch.setattr(net,'request',fake);t=job(s,p,b);w=Worker(s);w.once();due(s,t);w.once()
    assert read_task(s,t)['state']=='reconciling'
    s.providers['image']['download_hosts'].append('new-cdn.example.test')
    response=client.post(f"/api/v1/projects/{p['id']}/tasks/{t['id']}/reconcile")
    assert response.status_code==200,response.text
    due(s,t);w.once();j=read_task(s,t)
    assert j['state']=='succeeded',j['error']
    assert seen==['POST','GET']


@pytest.mark.parametrize('payload',[b'not-json',b'[]',b'{}',b'{"task_id": 123}',b'{"task_id":"../other"}'])
def test_malformed_h3_ack_is_unknown(native,monkeypatch,payload):
    _,s,p,b=native;seen=[]
    def fake(*a,**k):seen.append(a);return payload
    monkeypatch.setattr(net,'request',fake);t=job(s,p,b,'video');w=Worker(s);w.once();w.once()
    assert read_task(s,t)['state']=='reconciling' and len(seen)==1


def test_legacy_request_hash_unchanged(env):
    client,s=env;p=create(client);r=TaskInput(kind='concepts',expected_revision=p['revision'])
    old=r.model_dump()
    for f in ('image_options','cloud_image_options','video_options','speech_options'):old.pop(f,None)
    t=s.enqueue(p['id'],r,'legacy-request-key');assert t['request_hash']==fingerprint(old)


def test_h3_short_result_rejected(native,tmp_path,monkeypatch):
    _,s,p,b=native;v=video_fixture(tmp_path,2)
    def fake(method,url,hosts,**kw):
        if method=='POST':return b'{"task_id":"h3-test"}'
        if kw.get('destination'):kw['destination'].write_bytes(v.read_bytes());return b''
        return json.dumps({'task':{'id':'h3-test','model':'MiniMax-H3','status':'succeeded','content':{'url':'https://cdn.example.test/test.mp4'}}}).encode()
    monkeypatch.setattr(net,'request',fake);t=job(s,p,b,'video');w=Worker(s);w.once();due(s,t);w.once()
    j=read_task(s,t);assert j['state']=='failed' and 'shorter' in j['error']


def test_h3_late_cancelled_output_preserved(native,tmp_path,monkeypatch):
    _,s,p,b=native;v=video_fixture(tmp_path);seen=[]
    def fake(method,url,hosts,**kw):
        seen.append(method)
        if method=='POST':return b'{"task_id":"h3-test"}'
        if kw.get('destination'):kw['destination'].write_bytes(v.read_bytes());return b''
        return json.dumps({'task':{'id':'h3-test','model':'MiniMax-H3','status':'succeeded','content':{'url':'https://cdn.example.test/test.mp4'},'usage':{'output_seconds':5}}}).encode()
    monkeypatch.setattr(net,'request',fake);t=job(s,p,b,'video');w=Worker(s);w.once();s.cancel(p['id'],t['id']);due(s,t);w.once()
    j=read_task(s,t);assert j['state']=='cancelled' and j['result']['late_cancelled'] and j['result']['asset_id']
    assert s.snapshot(p['id'])['content']['shots'][0]['video_asset'] is None
    assert seen.count('POST')==1 and j['fee_state']=='uncertain'


def test_15s_native_protocol_to_render(native,tmp_path,monkeypatch):
    """Full media/worker path with generated test signals, NOT real AI quality."""
    client,s,p,b=narrated(native);v=video_fixture(tmp_path);audio=audio_fixture(tmp_path)
    image=tmp_path/'result.png';Image.new('RGB',(512,512),'navy').save(image)
    seen=[]
    def fake(method,url,hosts,**kw):
        seen.append((method,url))
        if method=='POST':
            if url.endswith('/v2/video_generation'):return json.dumps({'task_id':'h3-'+str(len(seen))}).encode()
            if url.endswith('/v1/t2a_v2'):return json.dumps({'data':{'status':2,'audio':audio.read_bytes().hex()},'trace_id':'tts-test','extra_info':{'usage_characters':5}}).encode()
            return json.dumps({'request_id':'qwen-test','output':{'choices':[{'message':{'content':[{'image':'https://cdn.example.test/image.png'}]}}]}}).encode()
        if kw.get('destination'):
            kw['destination'].write_bytes((image if url.endswith('png') else v).read_bytes());return b''
        return json.dumps({'task':{'id':url.rsplit('/',1)[1],'model':'MiniMax-H3','status':'succeeded','content':{'url':'https://cdn.example.test/video.mp4'},'usage':{'output_seconds':5}}}).encode()
    monkeypatch.setattr(net,'request',fake);w=Worker(s)
    def complete(t):
        assert w.once();due(s,t);assert w.once();j=read_task(s,t);assert j['state']=='succeeded',j['error'];return j['result']['asset_id']
    img=complete(job(s,p,b));s.review_asset(p['id'],img,'accepted','none','合成协议测试图片非质量认证')
    c=copy.deepcopy(p['content'])
    for i,shot in enumerate(c['shots']):
        vid=complete(job(s,p,b,'video',key=f'video-sample-{i}',shot_id=shot['id'],video_options={'mode':'first_frame','first_frame_id':img}))
        au=complete(job(s,p,b,'tts',key=f'tts-sample-{i}',shot_id=shot['id']))
        s.review_asset(p['id'],vid,'accepted','camera','合成运动图案非角色质量认证');s.review_asset(p['id'],au,'accepted','none','测试音而非中文音色认证')
        shot.update(video_asset=vid,audio_asset=au)
    p=save(client,p,c);t=s.enqueue(p['id'],TaskInput(kind='render',sample=True,sample_shot_ids=['SH01','SH02','SH03'],expected_revision=p['revision']),'native-sample-render')
    assert w.once();j=read_task(s,t);assert j['state']=='succeeded',j['error']
    qa=j['result']['qa'];assert qa['frames']==450 and qa['samples']==720000 and qa['duration_seconds']==15
    assert qa['real_model_quality_verified'] is False
    # Native 990 Hz source track must be excluded by default; sfx bus stays silent.
    import wave, array
    directory=s.store.root/'jobs'/j['result']['directory']
    with wave.open(str(directory/'sfx.wav'),'rb') as wav:
        raw=wav.readframes(wav.getnframes());assert not any(array.array('h',raw))
    assert len([x for x in seen if x[0]=='POST'])==7


def test_native_audio_and_tts_conflict(env,tmp_path):
    from test_media import media_workspace
    p,a,au=media_workspace(env,tmp_path);client,s=env;c=copy.deepcopy(p['content']);c['shots'][0]['original_audio']='keep';p=save(client,p,c)
    t=s.enqueue(p['id'],TaskInput(kind='render',expected_revision=p['revision']),'duplicate-audio-guard');Worker(s).once()
    j=read_task(s,t);assert j['state']=='failed' and 'never both' in j['error']


def test_old_narration_is_not_used_after_edit(env,tmp_path):
    from test_media import media_workspace
    p,a,au=media_workspace(env,tmp_path);client,s=env
    with s.db.tx() as session:
        asset=session.get(Asset,au['id']);asset.info={**asset.info,'generation':{'narration_hash':fingerprint('另一个文本')}}
    t=s.enqueue(p['id'],TaskInput(kind='render',expected_revision=p['revision']),'stale-narration-guard');Worker(s).once()
    j=read_task(s,t);assert j['state']=='failed' and 'older text' in j['error']


def test_qwen_prompt_never_silently_truncated(native):
    _,s,p,b=native
    with pytest.raises(ValueError,match='UTF-8-byte'):
        job(s,p,b,prompt='中文长提示词'*300)


def test_native_independent_provider_kinds_visible(native):
    client,s,_,_=native
    d=client.get('/api/v1/settings').json()
    assert {x['type']:x['kinds'] for x in d['providers']}=={'qwen_image':['image'],'minimax_h3':['video'],'minimax_tts':['tts']}
    assert 'synthetic-credential' not in json.dumps(d)
