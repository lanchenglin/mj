import copy
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PIL import Image
import pytest
from mj.contracts import BudgetInput, TaskInput, Workspace, gate_hash
from mj.db import Asset, Budget, Project, Task, public
from mj.domain import Problem
from mj.worker import Worker
from conftest import create, save, workspace


def req(p,kind='concepts',**kw):
    return TaskInput(kind=kind,expected_revision=p['revision'],**kw)


def test_login_required(env):
    client,_=env;client.cookies.clear()
    assert client.get('/api/v1/projects').status_code==401


def test_csrf_required(env):
    client,_=env;client.headers.pop('X-CSRF-Token')
    assert client.post('/api/v1/projects',json={'title':'x','brief':'y'}).status_code==403


def test_cross_origin_rejected(env):
    client,_=env
    assert client.post('/api/v1/logout',headers={'Origin':'https://evil.example'}).status_code==403


def test_logout_revokes_session(env):
    client,_=env;assert client.post('/api/v1/logout').status_code==200
    assert client.get('/api/v1/session').status_code==401


def test_create_and_revision_conflict(env):
    client,_=env;p=create(client);c=workspace(p);new=save(client,p,c)
    assert new['revision']==2
    assert client.put(f"/api/v1/projects/{p['id']}/workspace",json={'expected_revision':1,'content':c}).status_code==409
    assert len(client.get(f"/api/v1/projects/{p['id']}/history").json())==2


def test_restore_creates_new_revision(env):
    client,_=env;p=create(client);new=save(client,p,workspace(p))
    r=client.post(f"/api/v1/projects/{p['id']}/restore/1",json={'expected_revision':2})
    assert r.status_code==200 and r.json()['revision']==3 and not r.json()['content']['shots']


def test_cross_owner_project_hidden(env):
    client,studio=env;p=create(client)
    with studio.db.tx() as s:s.get(Project,p['id']).owner='other'
    assert client.get(f"/api/v1/projects/{p['id']}").status_code==404


def test_schema_rejects_unknown_fields(env):
    client,_=env;p=create(client);c=workspace(p);c['pretend_approved']=True
    assert client.put(f"/api/v1/projects/{p['id']}/workspace",json={'expected_revision':1,'content':c}).status_code==422


def test_duplicate_shots_and_unknown_character():
    p={'content':Workspace().model_dump(),'spec':{'total_frames':60}}
    c=workspace(p);c['shots'][1]['id']='SH01'
    with pytest.raises(ValueError):Workspace.model_validate(c)
    c['shots'][1]['id']='SH02';c['shots'][0]['character_ids']=['nonexistent']
    with pytest.raises(ValueError):Workspace.model_validate(c)


def test_dry_run_has_no_task_and_no_network(env,monkeypatch):
    _,studio=env;p=create(env[0])
    monkeypatch.setattr('httpx.Client.request',lambda *a,**k:pytest.fail('network'))
    r=studio.enqueue(p['id'],req(p,dry_run=True),'dryrun-key')
    assert r['external_calls']==0
    with studio.db.sessions() as s:assert not s.query(Task).all()


def test_idempotent_request_and_conflict(env):
    client,studio=env;p=create(client)
    a=studio.enqueue(p['id'],req(p),'same-key-123')
    b=studio.enqueue(p['id'],req(p),'same-key-123')
    assert a['id']==b['id']
    with pytest.raises(Problem) as e:studio.enqueue(p['id'],req(p,prompt='changed'),'same-key-123')
    assert e.value.status==409


def test_mock_workflow_returns_three_candidates(env):
    client,studio=env;p=create(client)
    t=studio.enqueue(p['id'],req(p),'concept-key')
    assert Worker(studio).once()
    with studio.db.sessions() as s:
        job=s.get(Task,t['id']);assert job.state=='succeeded';assert len(job.result['data'])==3;assert job.result['mock']
    new=studio.apply_task(p['id'],t['id'],1)
    assert len(new['content']['concepts'])==3 and new['revision']==2


def test_old_candidate_does_not_overwrite(env):
    client,studio=env;p=create(client);t=studio.enqueue(p['id'],req(p),'old-task-key')
    save(client,p,workspace(p));Worker(studio).once()
    with pytest.raises(Problem) as e:studio.apply_task(p['id'],t['id'],2)
    assert e.value.code=='stale_candidate'


def test_frozen_generation_inputs(env):
    client,studio=env;p=create(client);t=studio.enqueue(p['id'],req(p),'frozen-task')
    save(client,p,workspace(p))
    with studio.db.sessions() as s:assert s.get(Task,t['id']).snapshot['content']['script']==[]


def test_cancel_queued_prevents_execution(env):
    client,studio=env;p=create(client);t=studio.enqueue(p['id'],req(p),'cancel-task')
    assert studio.cancel(p['id'],t['id'])['state']=='cancelled'
    assert not Worker(studio).once()


def test_worker_lease_recovery_and_fencing(env):
    client,studio=env;p=create(client);t=studio.enqueue(p['id'],req(p),'lease-task-1');w=Worker(studio);old=w.claim()
    with studio.db.tx() as s:s.get(Task,t['id']).lease_until=0
    new=w.claim()
    assert new['fence']>old['fence']
    assert not w.finish(old,result={'data':[],'mock':True})
    assert w.finish(new,result={'data':[],'mock':True})


def test_cancelled_late_result_kept_without_activation(env):
    client,studio=env;p=create(client);t=studio.enqueue(p['id'],req(p),'late-cancel-task');w=Worker(studio);job=w.claim()
    studio.cancel(p['id'],t['id']);w.finish(job,result={'data':[],'mock':True})
    with studio.db.sessions() as s:
        task=s.get(Task,t['id']);assert task.state=='cancelled' and task.result
        assert s.get(Project,p['id']).revision==1


def paid_config(studio):
    # Domain-only fault tests. No external worker is executed and no network is used.
    studio.providers={'stub':{'type':'openai_compatible','base_url':'https://example.com/v1','hosts':['example.com'],'key_env':'TEST_STUB_KEY','models':{'text':'stub'},'currency':'USD','reserve_micros':{'concepts':100}}}


def budget(studio,p,limit=100):
    return studio.authorize(p['id'],BudgetInput(limit_micros=limit,per_task_micros=100,providers=['stub'],kinds=['concepts']))


def test_external_disabled_even_with_budget(env):
    client,studio=env;p=create(client);paid_config(studio);b=budget(studio,p)
    with pytest.raises(Problem) as e:studio.enqueue(p['id'],req(p,provider='stub',budget_id=b['id'],cap_micros=100),'external-closed')
    assert e.value.code=='paid_disabled'


def test_budget_required_and_currency(env):
    client,studio=env;p=create(client);paid_config(studio);studio.settings.external_enabled=True
    with pytest.raises(Problem) as e:studio.enqueue(p['id'],req(p,provider='stub',cap_micros=100),'budget-missing')
    assert e.value.code=='budget_required'
    b=budget(studio,p)
    with pytest.raises(Problem):studio.authorize(p['id'],BudgetInput(currency='CNY',limit_micros=100,per_task_micros=100,providers=['stub'],kinds=['concepts']))


def test_atomic_concurrent_budget_reservation(env):
    client,studio=env;p=create(client);paid_config(studio);studio.settings.external_enabled=True;b=budget(studio,p)
    def work(i):
        try:return studio.enqueue(p['id'],req(p,provider='stub',budget_id=b['id'],cap_micros=100),f'concurrent-{i}')['id']
        except Problem:return None
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(work,range(4)))
    assert sum(x is not None for x in results)==1
    with studio.db.sessions() as s:assert s.get(Budget,b['id']).used_micros==100


def test_cancel_releases_only_unsubmitted_reservation(env):
    client,studio=env;p=create(client);paid_config(studio);studio.settings.external_enabled=True;b=budget(studio,p)
    t=studio.enqueue(p['id'],req(p,provider='stub',budget_id=b['id'],cap_micros=100),'reserved-task')
    studio.cancel(p['id'],t['id'])
    with studio.db.sessions() as s:assert s.get(Budget,b['id']).used_micros==0


def test_unknown_submission_is_not_requeued(env):
    client,studio=env;p=create(client);paid_config(studio);studio.settings.external_enabled=True;b=budget(studio,p)
    t=studio.enqueue(p['id'],req(p,provider='stub',budget_id=b['id'],cap_micros=100),'unknown-task')
    w=Worker(studio);job=w.claim();w.finish(job,error='response lost',unknown=True)
    assert not w.claim()
    with studio.db.sessions() as s:
        assert s.get(Task,t['id']).state=='reconciling'
        assert s.get(Budget,b['id']).used_micros==100


def test_expired_grant_stops_queued_spend(env):
    client,studio=env;p=create(client);paid_config(studio);studio.settings.external_enabled=True;b=budget(studio,p)
    t=studio.enqueue(p['id'],req(p,provider='stub',budget_id=b['id'],cap_micros=100),'expired-grant')
    with studio.db.tx() as s:s.get(Budget,b['id']).expires=0
    assert not Worker(studio).claim()
    with studio.db.sessions() as s:
        assert s.get(Task,t['id']).state=='cancelled'
        assert s.get(Budget,b['id']).used_micros==0


def test_settlement_records_overage_honestly(env):
    client,studio=env;p=create(client);paid_config(studio);studio.settings.external_enabled=True;b=budget(studio,p)
    t=studio.enqueue(p['id'],req(p,provider='stub',budget_id=b['id'],cap_micros=100),'settle-task')
    w=Worker(studio);job=w.claim();w.finish(job,error='unknown',unknown=True)
    studio.settle(p['id'],t['id'],120,'invoice-123')
    with studio.db.sessions() as s:assert s.get(Budget,b['id']).used_micros==120
    with pytest.raises(Problem):studio.settle(p['id'],t['id'],0,'invoice-123')


def test_g1_requires_real_content_not_empty_flag(env):
    client,studio=env;p=create(client)
    r=client.post(f"/api/v1/projects/{p['id']}/approvals",json={'gate':'G1','expected_revision':1,'note':'已确认'})
    assert r.status_code==422


def test_subtitle_change_keeps_story_approval(env):
    client,studio=env;p=create(client);p=save(client,p,workspace(p))
    r=client.post(f"/api/v1/projects/{p['id']}/approvals",json={'gate':'G1','expected_revision':2,'note':'已看完故事'})
    assert r.status_code==200
    c=copy.deepcopy(p['content']);c['subtitles']['font_size']=60;p=save(client,p,c)
    assert p['approvals'][0]['current']
    c=copy.deepcopy(p['content']);c['script'][0]['action']='新的冲突';p=save(client,p,c)
    assert not p['approvals'][0]['current']


def test_upload_invalid_media_and_hashes(env,tmp_path):
    client,studio=env;p=create(client)
    assert client.post(f"/api/v1/projects/{p['id']}/assets",data={'rights':'owned'},files={'file':('fake.mp4',b'<html>error</html>','video/mp4')}).status_code==422
    a=tmp_path/'same.png';Image.new('RGB',(64,64),'red').save(a)
    first=studio.add_asset(p['id'],a,'same.png','own test')
    Image.new('RGB',(64,64),'blue').save(a)
    second=studio.add_asset(p['id'],a,'same.png','own test')
    assert first['blob_hash']!=second['blob_hash']


def test_cross_project_asset_rejected(env,tmp_path):
    client,studio=env;p=create(client);q=create(client);file=tmp_path/'image.png';Image.new('RGB',(20,20)).save(file)
    a=studio.add_asset(p['id'],file,'image.png','own image');c=workspace(q);c['shots'][0]['reference_ids']=[a['id']]
    assert client.put(f"/api/v1/projects/{q['id']}/workspace",json={'expected_revision':1,'content':c}).status_code==404


def test_missing_idempotency_key(env):
    client,_=env;p=create(client)
    r=client.post(f"/api/v1/projects/{p['id']}/tasks",json={'kind':'concepts','expected_revision':1})
    assert r.status_code==422


def test_mock_cannot_be_approved_as_real_sample(env):
    client,studio=env;p=create(client,450);p=save(client,p,workspace(p,3))
    client.post(f"/api/v1/projects/{p['id']}/approvals",json={'gate':'G1','expected_revision':2,'note':'故事通过'})
    with studio.db.tx() as s:
        t=Task(project_id=p['id'],kind='render',provider='local',state='succeeded',input_hash='x',request_hash='x',idempotency_key='mock-render',snapshot={'content':p['content']},revision=2,result={'mock':True})
        s.add(t);s.flush();tid=t.id
    r=client.post(f"/api/v1/projects/{p['id']}/approvals",json={'gate':'G3','expected_revision':2,'task_id':tid,'note':'尝试错误批准'})
    assert r.status_code==422 and r.json()['error']['code']=='mock_not_deliverable'
