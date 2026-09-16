"""Render a 120-second synthetic animatic. No cloud requests or paid credentials."""
import json
import os
from pathlib import Path
from mj.config import Settings
from mj.contracts import NewProject, RevisionInput, Spec, TaskInput, Workspace
from mj.domain import Studio
from mj.db import Task
from mj.worker import Worker

root=Path(os.getenv('MJ_CHECK_DIR','checks')).resolve()/'render120'
root.mkdir(parents=True,exist_ok=True)
s=Studio(Settings(database_url=f'sqlite:///{root}/test.db',data_dir=root,mode='test',admin_password='synthetic-test-password-only'))
s.db.init()
pid=s.create_project(NewProject(title='120秒工程预演',brief='程序生成的20镜头占位卡，不是正式漫剧。',spec=Spec(width=360,height=640,fps=30,total_frames=3600)))
p=s.snapshot(pid)
w=Workspace.model_validate({'shots':[{'id':f'SH{i+1:02}','frames':180,'action':f'工程测试镜头 {i+1}：核对画幅、帧数、中文字幕占位。','motion':'subject_action','narration':'这是工程预演，不是正式生成的漫剧。'} for i in range(20)]})
p=s.update(pid,RevisionInput(expected_revision=1,content=w))
t=s.enqueue(pid,TaskInput(kind='animatic',expected_revision=p['revision']),'render-120-frames')
Worker(s).once()
with s.db.sessions() as db:
    job=db.get(Task,t['id'])
    assert job.state=='succeeded',job.error
    result=job.result
    assert result['qa']['frames']==3600 and result['qa']['samples']==5760000
    assert result['mock'] is True
    report={'task_id':job.id,'qa':result['qa'],'output_dir':str(root/'jobs'/result['directory']),'paid_calls':0}
    (root.parent/'render120-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
