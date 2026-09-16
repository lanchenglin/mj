import json
from pathlib import Path
import pytest
from mj import providers
from mj.config import Settings
from mj.contracts import Workspace


def cfg():
    return {'type':'openai_compatible','base_url':'https://example.com/v1','hosts':['example.com'],'key_env':'MJ_TEST_KEY','models':{'text':'test','image':'test-image','tts':'test-tts'}}


def snap(kind='concepts'):
    return {'request':{'kind':kind,'shot_id':'','prompt':''},'provider_config':cfg(),'assets':{},'style':'二维','title':'样例','brief':'修灯','spec':{'fps':30,'total_frames':3600},'content':Workspace().model_dump()}


def test_safe_url_blocks_private_protocols():
    for url in ['http://example.com','file:///etc/passwd','https://user:pass@example.com','https://evil.test','https://example.com:444']:
        with pytest.raises(ValueError):providers.safe_url(url,['example.com'],resolve=False)


def test_dns_private_ip_rejected(monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError):providers.safe_url('https://example.com',['example.com'])


def test_production_requires_pg(tmp_path):
    with pytest.raises(ValueError):Settings(mode='production',data_dir=tmp_path,admin_password='a'*20).validate()


def test_demo_cannot_enable_paid(tmp_path):
    with pytest.raises(ValueError):Settings(external_enabled=True,data_dir=tmp_path,admin_password='a'*20).validate()


def test_password_has_no_unsafe_default(tmp_path):
    with pytest.raises(ValueError):Settings(data_dir=tmp_path).validate()


def test_text_adapter_validates_three_concepts(monkeypatch,tmp_path):
    monkeypatch.setenv('MJ_TEST_KEY','test-not-secret')
    data=[{'id':str(i),'title':'灯','logline':'修灯','conflict':'停电','ending':'灯亮'} for i in range(3)]
    calls=[]
    def fake(method,url,hosts,**kw):
        calls.append((method,url,kw))
        return json.dumps({'choices':[{'message':{'content':json.dumps({'data':data})}}],'usage':{'tokens':1}}).encode()
    monkeypatch.setattr(providers,'request',fake)
    r=providers.External(snap(),None).submit(tmp_path)
    assert len(r['data'])==3 and r['mock'] is False
    assert calls[0][1].endswith('/chat/completions')
    assert len(calls)==1


def test_failed_response_is_not_automatically_retried(monkeypatch,tmp_path):
    import httpx
    monkeypatch.setenv('MJ_TEST_KEY','test-not-secret');calls=[]
    def fake(*a,**kw):calls.append(1);raise httpx.ReadTimeout('lost response')
    monkeypatch.setattr(providers,'request',fake)
    with pytest.raises(providers.UnknownSubmission):providers.External(snap(),None).submit(tmp_path)
    assert len(calls)==1


def test_fake_approval_fields_rejected_by_text_schema():
    with pytest.raises(ValueError):providers.validate_text('bible',{'characters':[],'scenes':[],'props':[],'approved':True})


def test_models_not_silently_substituted():
    c=cfg();c['models']={}
    with pytest.raises(ValueError):providers.preflight(c,'tts')


def test_fal_requires_explicit_capability_config():
    with pytest.raises(ValueError):providers.preflight({'type':'fal_queue','endpoint':'fal-ai/test','key_env':'MJ_TEST_KEY'},'video')
