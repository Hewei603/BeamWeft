import React, {useEffect,useState} from 'react';
import {workbenchApi} from '../api/workbench';
import {widthNames} from './beamWidths';
import {EvidenceRole,ReadingOriginal,readingUses} from './ReadingEvidence';
import './PredictionReadingCheck.css';

const fmt=v=>Number.isFinite(v)?Number(v.toPrecision(7)):'—';
const unitLabel=u=>u==='um'?'μm':u;
const verdict={inside:'区间内',outside:'区间外',unknown:'待核对'};
function appendPage(current,next) {
  const ids=new Set(current.map(item=>item.id));
  return [...current,...next.filter(item=>!ids.has(item.id))];
}
function recordedTime(stamp,archiveTime) {
  if(!stamp)return '未记录';
  const text=stamp.replace('T',' ');
  return archiveTime && !/(Z|[+-]\d{2}:\d{2})$/i.test(stamp)?text+' UTC':text;
}

function UseSelect({label,value,onChange}) {
  return <label>{label}<select aria-label={label} value={value} onChange={e=>onChange(e.target.value)}>
    {Object.entries(readingUses).map(([v,text])=><option key={v} value={v}>{text}</option>)}
  </select></label>;
}

export function PredictionCheckResult({caseId,item,onAsk}) {
  const unit=unitLabel(item.unit);
  const reading=item.reading, chronology=item.chronology, conditions=item.prediction_conditions;
  return <article className="prediction-check-result" aria-label="已存预测检验">
    <h4>实测检验 #{item.id}</h4>
    <p>预测比较 #{item.prediction_id} · 实测 #{item.reading_id} · {item.source?.path}{item.reading?.locator && ' · '+item.reading.locator}</p>
    <p>{item.axis==='tangential'?'T':'S'} 向 · {widthNames[item.definition]} · 实测 {fmt(item.measured)} {unit}
      {Number.isFinite(item.position_mm) && ' · '+(item.position_reference==='element_depth'?'入口深度':'测点坐标')+' '+fmt(item.position_mm)+' mm'}</p>
    <p>{item.registration_note}</p>
    <div className="check-outcomes">{item.outcomes.map(o=><section key={o.revision_id} aria-label={'版本 #'+o.revision_id+' 的检验'}>
      <h5>版本 #{o.revision_id} · {o.title}</h5>
      <p><strong className={'check-state '+o.state}>{verdict[o.state]}</strong>{o.reason && ' · '+o.reason}</p>
      <dl><dt>冻结预测 / {unit}</dt><dd>{fmt(o.predicted)}</dd>
        <dt>可读区间 / {unit}</dt><dd>{o.interval?'['+o.interval.map(fmt).join(', ')+']':'未确定'}</dd>
        <dt>实测 − 预测 / {unit}</dt><dd>{fmt(o.residual)}</dd></dl>
      <EvidenceRole evidence={o.evidence}/>
    </section>)}</div>
    {conditions && <details><summary>冻结预测的条件与误差界限</summary>
      <p>{conditions.region==='incident_train'?'接入前支路':'主光路区段'} · {conditions.registration_note}</p>
      <p>{conditions.absolute_width_bound==null?'宽度读数的绝对误差界限未记录。':'宽度读数 ±'+fmt(conditions.absolute_width_bound)+' '+unit+' · '+conditions.bound_source}</p>
      {conditions.position_bound && <p>位置 ±{fmt(conditions.position_bound.plus_minus_mm)} mm · {conditions.position_bound.source}</p>}
      {conditions.solver_sha256 && <small className="check-digest">预测方法 SHA-256 · <code>{conditions.solver_sha256}</code></small>}
    </details>}
    {reading && <details><summary>原始读数与记录时间</summary>
      <p>登记版本 #{reading.revision_id} · 台架坐标 {fmt(reading.z_mm)} mm · {reading.wavelength_nm==null?'波长待核对':fmt(reading.wavelength_nm)+' nm'} · {reading.conditions}</p>
      <p>{widthNames[reading.definition]} · {['tangential','sagittal'].filter(a=>Number.isFinite(reading[a]))
        .map(a=>(a==='tangential'?'T':'S')+' '+fmt(reading[a])+' '+unitLabel(reading.unit)).join(' / ')} · {reading.axes_aligned?'仪器轴向已配准':'仪器轴向待配准'}</p>
      <p>{({agent:'Agent 转录',user:'手工转录',manual:'手工转录'})[reading.transcribed_by] || '转录人未记录'} · {reading.locator}</p>
      {chronology && <dl className="check-chronology">{[['prediction_saved_at','冻结预测'],['original_uploaded_at','上传原件'],
        ['reading_registered_at','登记录数'],['acquired_at','实验采集']].map(([key,label])=><React.Fragment key={key}>
          <dt>{label}</dt><dd>{recordedTime(chronology[key],key!=='acquired_at')}</dd></React.Fragment>)}</dl>}
    </details>}
    <ReadingOriginal key={item.id} caseId={caseId} source={item.source}/>
    {onAsk && <button type="button" className="bench-text-btn" onClick={()=>onAsk('关于实测检验 #'+item.id+' 与预测比较 #'+item.prediction_id+'：\n',
      {artifact_ids:[item.id,item.prediction_id],project_revision_id:item.revision_id})}>引用这次检验问 Agent</button>}
  </article>;
}

export default function PredictionReadingCheck({caseId,prediction,onAsk,disabled=false}) {
  const [open,setOpen]=useState(false),[attempt,retry]=useState(0);
  const [readings,setReadings]=useState([]),[history,setHistory]=useState([]);
  const [readingCursor,setReadingCursor]=useState(null),[historyCursor,setHistoryCursor]=useState(null);
  const [chosen,setChosen]=useState(''),[selected,setSelected]=useState(null);
  const [note,setNote]=useState(''),[role,setRole]=useState('unspecified');
  const [separate,setSeparate]=useState(false),[overrides,setOverrides]=useState({});
  const [error,setError]=useState(''),[loaded,setLoaded]=useState(null),[busy,setBusy]=useState(false);
  const loading=loaded!==attempt;

  useEffect(()=>{
    if(!open)return;
    let live=true;
    Promise.allSettled([workbenchApi.beamObservations(caseId),workbenchApi.predictionChecks(caseId,prediction.id)])
      .then(([sources,checks])=>{
        if(!live)return;
        if(sources.status==='fulfilled'){
          const page=sources.value;
          setReadings(old=>page.next_before_id?appendPage(page.readings,old.filter(r=>r.id<page.next_before_id)):page.readings);
          setReadingCursor(page.next_before_id);
        }
        if(checks.status==='fulfilled'){
          const page=checks.value;
          setHistory(old=>page.next_before_id?appendPage(page.items,old.filter(r=>r.id<page.next_before_id)):page.items);
          setHistoryCursor(page.next_before_id);
        }
        setError([sources,checks].filter(r=>r.status==='rejected').map(r=>r.reason.message).join(' '));setLoaded(attempt);
      });
    return ()=>{live=false;};
  },[caseId,prediction.id,open,attempt]);

  const reading=readings.find(r=>r.id===Number(chosen));
  const saved=history.find(r=>r.id===selected) || history[0];
  function choose(value){
    setChosen(value);setNote('');setRole('unspecified');setSeparate(false);setOverrides({});
  }
  async function more(kind){
    setBusy(true);setError('');
    try{
      if(kind==='readings'){
        const page=await workbenchApi.beamObservations(caseId,readingCursor);
        setReadings(old=>appendPage(old,page.readings));setReadingCursor(page.next_before_id);
      }else{
        const page=await workbenchApi.predictionChecks(caseId,prediction.id,historyCursor);
        setHistory(old=>appendPage(old,page.items));setHistoryCursor(page.next_before_id);
      }
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  async function save(event){
    event.preventDefault();
    if(!reading || !note.trim())return;
    setError('');setBusy(true);
    try{
      const check=await workbenchApi.checkPrediction(caseId,{prediction_id:prediction.id,reading_id:reading.id,
        registration_note:note.trim(),data_role:role,...(separate?{model_roles:overrides}:{})});
      setHistory(old=>[check,...old.filter(r=>r.id!==check.id)]);setSelected(check.id);
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }

  return <details className="prediction-reading-check" onToggle={e=>{if(e.target===e.currentTarget){setOpen(e.currentTarget.open);if(e.currentTarget.open)retry(n=>n+1);}}}>
    <summary>用实测检验这份预测</summary>{open && <>
      {loading && <p role="status">读取预测检验及已登记读数…</p>}
      {!loading && !readings.length && !error && <div><p>可在当前画布的「顺序光路」下选择观察面，登记原件中的光斑宽度。</p>
        <a href={'#register-reading-'+caseId} onClick={()=>{
          const entry=document.getElementById('register-reading-'+caseId);
          if(entry?.tagName==='DETAILS'){entry.open=true;entry.querySelector('summary')?.focus();}
        }}>前往画布登记实验读数</a> · <button type="button" onClick={()=>retry(n=>n+1)}>刷新已登记读数</button></div>}
      {(readings.length>0 || readingCursor) && <form onSubmit={save}><fieldset disabled={busy || disabled || loading}>
        <label>核对预测的已登记读数<select required value={chosen} onChange={e=>choose(e.target.value)}>
          <option value="">选择工程内的实测读数</option>
          {readings.map(r=><option value={r.id} key={r.id}>#{r.id} · {r.source_path} · {r.locator} · 工程 #{r.revision_id} · z = {fmt(r.z_mm)} mm</option>)}</select></label>
        {readingCursor && <button type="button" onClick={()=>more('readings')}>更早登记的读数</button>}
        {reading && <p>{widthNames[reading.definition]} · {['tangential','sagittal'].filter(a=>Number.isFinite(reading[a]))
          .map(a=>(a==='tangential'?'T':'S')+' '+fmt(reading[a])+' '+unitLabel(reading.unit)).join(' / ')} · {reading.conditions}</p>}
        <label>测点与采集条件依据<textarea required maxLength={800} rows={2} value={note} onChange={e=>setNote(e.target.value)}
          placeholder="记录探测面如何定位、对应哪束光，以及测量时的工况。"/></label>
        <UseSelect label="本次读数的用途" value={role} onChange={setRole}/>
        <label className="per-model-use"><input type="checkbox" checked={separate} onChange={e=>{setSeparate(e.target.checked);setOverrides({});}}/>
          这条读数与两版模型的关系不同</label>
        {separate && prediction.models.map(m=><UseSelect key={m.revision_id} label={'版本 #'+m.revision_id+' 的数据用途'}
          value={overrides[m.revision_id] ?? role} onChange={v=>setOverrides(old=>({...old,[m.revision_id]:v}))}/>)}
        <button className="btn btn-secondary" disabled={!reading || !note.trim()}>保存实测检验</button>
      </fieldset></form>}
      {error && <p role="alert">{error} <button type="button" onClick={()=>retry(n=>n+1)}>重新读取</button></p>}
      {history.length>1 && <label>已存预测检验记录<select value={saved.id} onChange={e=>setSelected(Number(e.target.value))}>
        {history.map(r=><option key={r.id} value={r.id}>检验 #{r.id} · 实测 #{r.reading_id} · {r.source?.path} · {r.registration_note}</option>)}</select></label>}
      {historyCursor && <button type="button" disabled={loading || busy} onClick={()=>more('checks')}>更早的实测检验</button>}
      {saved && <PredictionCheckResult key={saved.id} caseId={caseId} item={saved} onAsk={onAsk}/>}
    </>}
  </details>;
}
