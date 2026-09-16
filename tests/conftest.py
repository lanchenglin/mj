from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from mj.app import create_app
from mj.config import Settings

PASSWORD="local-test-password-not-for-production"

@pytest.fixture
def env(tmp_path):
    settings=Settings(database_url=f"sqlite:///{tmp_path}/test.db",data_dir=tmp_path,mode="test",admin_password=PASSWORD)
    app=create_app(settings)
    with TestClient(app) as client:
        r=client.post('/api/v1/login',json={'password':PASSWORD})
        assert r.status_code==200
        client.headers['X-CSRF-Token']=r.json()['csrf']
        yield client,app.state.studio


def create(client,frames=60):
    r=client.post('/api/v1/projects',json={'title':'测试原创故事','brief':'雨夜修理铺里，两个人合作修亮旧灯。','spec':{'width':256,'height':256,'fps':30,'total_frames':frames}})
    assert r.status_code==201,r.text
    return r.json()


def workspace(project,n=2):
    c=project['content']
    c['concepts']=[{'id':'CPT1','title':'借来的光','logline':'修灯','conflict':'停电','ending':'亮灯'}]
    c['selected_concept']='CPT1'
    c['script']=[{'id':'B01','action':'人物拿起旧灯','narration':'','speaker_id':'narrator','emotion':'自然'}]
    c['bible']['characters']=[{'id':'C01','name':'阿澄','description':'蓝色外套','reference_ids':[]}]
    c['bible']['scenes']=[{'id':'SC01','name':'修理铺','description':'门在右侧','reference_ids':[]}]
    c['shots']=[{'id':f'SH{i+1:02}','beat_id':'B01','scene_id':'SC01','character_ids':['C01'],'frames':project['spec']['total_frames']//n,'action':'看向旧灯','motion':'camera'} for i in range(n)]
    return c


def save(client,p,c):
    r=client.put(f"/api/v1/projects/{p['id']}/workspace",json={'expected_revision':p['revision'],'content':c})
    assert r.status_code==200,r.text
    return r.json()
