// Native API forms. Credentials and arbitrary server URLs never enter these forms.
export const nativeTypes = ['qwen_image','minimax_h3','minimax_tts'];
export function nativeFields(form, S, ui) {
 const panel=form.querySelector('#native-options'); if(!panel)return;
 const provider=(S.settings.providers||[]).find(p=>p.id===form.elements.provider?.value);
 const {select,field,area}=ui;
 const oldMode=form.elements.h3_mode?.value || 'first_frame';
 const oldDuration=form.elements.h3_duration?.value||'';const oldResolution=form.elements.h3_resolution?.value||'768P';
 const images=S.assets.filter(a=>a.kind==='image'&&a.review==='accepted'&&!a.mock);
 const choices=[['','请选择已审核图片'],...images.map(a=>[a.id,a.name+' · '+a.id.slice(0,8)])];
 const ratio=S.project?.spec?.width/S.project?.spec?.height || 9/16;
 const [imageWidth,imageHeight]=Math.abs(ratio-1)<.01?[1024,1024]:(ratio>1?[1280,Math.round(1280/ratio/16)*16]:[Math.round(1280*ratio/16)*16,1280]);
 const voice=form.elements.voice;
 if(voice){voice.disabled=provider?.type==='minimax_tts';voice.closest('label').hidden=voice.disabled;}
 if(!provider||!nativeTypes.includes(provider.type)){
  panel.innerHTML='';const b=form.elements.budget_id;
  if(b){const last=b.value;b.replaceChildren(...[['','选择授权'],...S.budgets.filter(x=>!x.revoked).map(x=>[x.id,x.currency+' · '+x.id.slice(0,8)])].map(([v,t])=>{const o=document.createElement('option');o.value=v;o.textContent=t;return o;}));b.value=last;}
  return;
 }
 const kind=provider.kinds[0];
 const budget=form.elements.budget_id;
 if(budget){const last=budget.value;budget.innerHTML=[['','请选择对应币种与供应商的授权'],...S.budgets.filter(b=>!b.revoked&&b.currency===provider.currency&&b.policy.providers.includes(provider.id)&&b.policy.kinds.includes(kind)).map(b=>[b.id,b.currency+' · '+b.id.slice(0,8)])].map(([v,t])=>{const o=document.createElement('option');o.value=v;o.textContent=t;return o.outerHTML;}).join('');budget.value=last;}
 const note='<p class="small">仅预检不会联网。真实提交仍需预算与阶段审批；生成后只保存候选，不自动选用。</p>';
 if(provider.type==='qwen_image'){
  panel.innerHTML=`<div class="banner">Qwen 云端生图 / 编辑：不选参考即文生图；最多三张参考，按下面顺序传入。不支持蒙版参数。</div><div class="field-row">${field('qwen_width','宽度',imageWidth,'number','min="512" max="2048" step="16"')}${field('qwen_height','高度',imageHeight,'number','min="512" max="2048" step="16"')}</div>${[1,2,3].map(i=>select('qwen_ref','参考图 '+i,choices,'')).join('')}${field('qwen_seed','种子（留空由任务固定）','','number','min="0" max="2147483647"')}${area('qwen_negative','负面提示词（最多500字符）','','maxlength="500"')}${note}`;
 }else if(provider.type==='minimax_h3'){
  panel.innerHTML=`<div class="banner warn">H3 原生声音默认不进入成片。独立旁白另行生成；只把明确选定的参考素材发送给视频接口。</div>${select('h3_mode','生成模式',[['first_frame','分镜首帧 → 视频'],['first_last','首尾帧 → 视频'],['last_frame','尾帧 → 视频'],['reference','多参考素材 → 视频'],['text','纯文生视频']],oldMode)}<div id="h3-refs"></div><div class="field-row">${select('h3_resolution','生成分辨率',provider.models.video==='MiniMax-H3-Max'?[['480P','480P'],['768P','768P']]:[['768P','768P'],['2K','2K']],'768P')}${field('h3_duration','生成秒数（留空按镜头含入点向上取整）','','number','min="4" max="15" step="1"')}</div>${note}`;
  const target=form.querySelector('#h3-refs');
  if(oldMode==='first_frame'||oldMode==='first_last')target.innerHTML+=select('h3_first','首帧',choices,'');
  if(oldMode==='last_frame'||oldMode==='first_last')target.innerHTML+=select('h3_last','尾帧',choices,'');
  if(oldMode==='reference')target.innerHTML+=[1,2,3].map(i=>select('h3_ref','参考素材 '+i,[['','不选择'],...S.assets.filter(a=>a.review==='accepted'&&!a.mock).map(a=>[a.id,'['+a.kind+'] '+a.name])],'')).join('')+area('h3_reference_ids','高级：更多参考素材 ID（填写后替代上面选择，逗号分隔且保持顺序）','','placeholder="可留空；填写时明确完整参考顺序"')+'<p class="small">图片≤9张，视频≤3段/合计15秒，音频≤3段/合计15秒。须为已审核且有授权的本项目素材。</p>';
  form.elements.h3_duration.value=oldDuration;
  if([...form.elements.h3_resolution.options].some(x=>x.value===oldResolution))form.elements.h3_resolution.value=oldResolution;
 }else{
  panel.innerHTML=`<div class="banner">独立中文旁白：只朗读分镜里已经保存的文字，不读取“补充方向”作为台词。声音 ID 需与 MiniMax 账户核对。</div>${provider.voices?.length?select('tts_voice','声音 ID',provider.voices.map(x=>[x,x]),provider.default_voice):field('tts_voice','声音 ID',provider.default_voice||'','text','required')}<div class="field-row three">${field('tts_speed','语速',1,'number','min="0.5" max="2" step="0.05"')}${field('tts_volume','音量',1,'number','min="0.1" max="4" step="0.1"')}${field('tts_pitch','音调',0,'number','min="-12" max="12" step="1"')}</div>${area('tts_pronunciation','发音词典：每行 词语/读法（可留空）','')}${note}`;
 }
}
export function nativeOptions(form, S) {
 const f=new FormData(form),v=Object.fromEntries(f);
 const provider=(S.settings.providers||[]).find(p=>p.id===v.provider);
 if(provider?.type==='qwen_image')return {reference_ids:f.getAll('qwen_ref').filter(Boolean),cloud_image_options:{width:+v.qwen_width,height:+v.qwen_height,seed:v.qwen_seed===''?null:Number(v.qwen_seed),negative_prompt:v.qwen_negative||'',prompt_extend:false,use_shot_references:false}};
 if(provider?.type==='minimax_h3'){
  const ids=v.h3_reference_ids?.trim()?v.h3_reference_ids.split(/[,，\s]+/).filter(Boolean):f.getAll('h3_ref').filter(Boolean);
  const references=ids.map(id=>{const a=S.assets.find(x=>x.id===id);if(!a)throw new Error('参考素材不存在：'+id);return {asset_id:id,role:'reference_'+a.kind};});
  return {video_options:{mode:v.h3_mode,first_frame_id:v.h3_first||null,last_frame_id:v.h3_last||null,references,duration_seconds:v.h3_duration?Number(v.h3_duration):null,resolution:v.h3_resolution}};
 }
 if(provider?.type==='minimax_tts')return {speech_options:{voice_id:v.tts_voice,speed:+v.tts_speed,volume:+v.tts_volume,pitch:+v.tts_pitch,pronunciation:(v.tts_pronunciation||'').split('\n').map(x=>x.trim()).filter(Boolean)}};
 return {};
}
