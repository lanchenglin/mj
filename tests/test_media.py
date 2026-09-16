import copy
import json
import math
import struct
import wave
import zipfile
from pathlib import Path
from PIL import Image
import pytest
from mj.contracts import TaskInput, subtitle_chunks
from mj.db import Asset, Task
from mj.media import Store, command, export_bundle, inspect_media, render
from mj.worker import Worker
from conftest import create, save, workspace


def media_workspace(env,tmp_path,frames=60):
    client,studio=env;p=create(client,frames);c=workspace(p,2)
    image=tmp_path/'画面 含空格.png';Image.new('RGB',(300,200),'#21344d').save(image)
    a=studio.add_asset(p['id'],image,image.name,'own synthetic test fixture')
    studio.review_asset(p['id'],a['id'],'accepted','camera','图像与场景已检查')
    wav=tmp_path/'声音.wav'
    with wave.open(str(wav),'wb') as w:
        w.setparams((1,2,48000,0,'NONE','not compressed'))
        w.writeframes(b''.join(struct.pack('<h',int(2000*math.sin(2*math.pi*220*i/48000))) for i in range(24000)))
    au=studio.add_asset(p['id'],wav,wav.name,'own synthetic test tone')
    studio.review_asset(p['id'],au['id'],'accepted','none','测试声音已检查')
    for s in c['shots']:
        s.update(video_asset=a['id'],audio_asset=au['id'],narration='这盏灯，亮了。')
    p=save(client,p,c)
    return p,a,au


def test_chinese_subtitle_segmentation():
    chunks=subtitle_chunks('你好，世界！今天我们讲一个温暖的小故事。',8)
    assert ''.join(chunks)=='你好，世界！今天我们讲一个温暖的小故事。'
    assert all(len(x)<=8 for x in chunks)
    assert ' ' not in ''.join(chunks)


def test_media_storage_rejects_path_escape(tmp_path):
    store=Store(tmp_path)
    with pytest.raises(ValueError):store.path('../bad','.mp4')
    with pytest.raises(ValueError):store.path('a'*64,'../../bad')


def test_corrupt_media_rejected(tmp_path):
    p=tmp_path/'broken.mp4';p.write_bytes(b'not-a-video')
    with pytest.raises(ValueError):inspect_media(p)


def test_real_ffmpeg_render_chinese_audio_and_export(env,tmp_path):
    p,a,au=media_workspace(env,tmp_path);client,studio=env
    task=studio.enqueue(p['id'],TaskInput(kind='render',expected_revision=p['revision']),'real-render-key')
    assert Worker(studio).once()
    with studio.db.sessions() as s:
        t=s.get(Task,task['id'])
        assert t.state=='succeeded',t.error
        assert t.result['qa']['frames']==60
        assert t.result['qa']['samples']==96000
        directory=studio.store.root/'jobs'/t.result['directory']
        assert '这盏灯' in (directory/'captions.srt').read_text()
        archive=export_bundle(directory,t.result,{'unsettled':True},t.snapshot['assets'],studio.store)
        with zipfile.ZipFile(archive) as z:
            assert 'manifest.json' in z.namelist()
            manifest=json.loads(z.read('manifest.json'))
            assert manifest['files']['final_clean.mp4']
            assert len(manifest['assets'])==2
            assert not any(n.startswith('/') or '..' in n or '.env' in n for n in z.namelist())
    assert client.get(f"/api/v1/projects/{p['id']}/tasks/{task['id']}/files/final_clean.mp4").status_code==200


def test_no_audio_video_can_render(env,tmp_path):
    client,studio=env;p=create(client,30);c=workspace(p,1)
    path=tmp_path/'无音轨.mp4'
    command(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=blue:s=320x180:r=24:d=2','-an','-c:v','libx264','-threads','2',str(path)])
    a=studio.add_asset(p['id'],path,path.name,'own generated test clip')
    assert not a['info']['audio']
    studio.review_asset(p['id'],a['id'],'accepted','camera','测试色块素材')
    c['shots'][0]['video_asset']=a['id'];p=save(client,p,c)
    t=studio.enqueue(p['id'],TaskInput(kind='render',expected_revision=p['revision']),'silent-video')
    Worker(studio).once()
    with studio.db.sessions() as s:
        t=s.get(Task,t['id']);assert t.state=='succeeded',t.error;assert t.result['qa']['frames']==30


def test_subject_action_cannot_be_still_image(env,tmp_path):
    p,a,au=media_workspace(env,tmp_path);client,studio=env;c=copy.deepcopy(p['content']);c['shots'][0]['motion']='subject_action';p=save(client,p,c)
    t=studio.enqueue(p['id'],TaskInput(kind='render',expected_revision=p['revision']),'action-failure')
    Worker(studio).once()
    with studio.db.sessions() as s:
        t=s.get(Task,t['id']);assert t.state=='failed';assert 'action contract' in t.error


def test_incomplete_timeline_blocks_render(env,tmp_path):
    p,a,au=media_workspace(env,tmp_path);client,studio=env;c=copy.deepcopy(p['content']);c['shots'][0]['frames']-=1;p=save(client,p,c)
    t=studio.enqueue(p['id'],TaskInput(kind='render',expected_revision=p['revision']),'gap-failure')
    Worker(studio).once()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).state=='failed'


def test_long_narration_not_silently_trimmed(env,tmp_path):
    p,a,au=media_workspace(env,tmp_path,frames=12);client,studio=env
    t=studio.enqueue(p['id'],TaskInput(kind='render',expected_revision=p['revision']),'speech-too-long')
    Worker(studio).once()
    with studio.db.sessions() as s:
        t=s.get(Task,t['id']);assert t.state=='failed';assert 'narration is longer' in t.error


def test_animatic_is_always_marked_mock(env):
    client,studio=env;p=create(client,30);p=save(client,p,workspace(p,1))
    t=studio.enqueue(p['id'],TaskInput(kind='animatic',expected_revision=p['revision']),'animatic-only')
    Worker(studio).once()
    with studio.db.sessions() as s:
        t=s.get(Task,t['id']);assert t.state=='succeeded',t.error;assert t.result['mock']


def test_missing_media_is_not_exported(env,tmp_path):
    p,a,au=media_workspace(env,tmp_path);client,studio=env
    studio.store.asset_path(a).unlink()
    t=studio.enqueue(p['id'],TaskInput(kind='render',expected_revision=p['revision']),'missing-asset')
    Worker(studio).once()
    with studio.db.sessions() as s:assert s.get(Task,t['id']).state=='failed'


def test_multi_shot_timestamps_do_not_accumulate_missing_frame(env,tmp_path):
    from mj.media import ffprobe
    client,studio=env;p=create(client,180);c=workspace(p,6)
    for shot in c['shots']:
        shot.update(motion='none',narration='中文时间轴验证。')
    p=save(client,p,c)
    t=studio.enqueue(p['id'],TaskInput(kind='animatic',expected_revision=p['revision']),'multi-clip-clock')
    Worker(studio).once()
    with studio.db.sessions() as db:
        job=db.get(Task,t['id']);assert job.state=='succeeded',job.error
        directory=studio.store.root/'jobs'/job.result['directory']
        for name in ['visual.mp4','final_clean.mp4','final_subtitled.mp4']:
            video=next(s for s in ffprobe(directory/name,count=True)['streams'] if s['codec_type']=='video')
            assert int(video['nb_read_frames'])==180
            assert abs(float(video['duration'])-6)<0.5/30
