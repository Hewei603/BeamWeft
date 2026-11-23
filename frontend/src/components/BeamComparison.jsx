import React, { useEffect, useState } from 'react';
import { workbenchApi } from '../api/workbench';
import DetectorPositionBound from './DetectorPositionBound';
import {EvidenceRole,ReadingOriginal,readingUses} from './ReadingEvidence';

import { readingAxis, widthNames } from './beamWidths';
const roles = {unspecified:'独立性待确认，差值仅供参考', held_out:'同一工况，作为留出数据独立检验',
  model_input:'这条读数参与了目标模型的建模', different_configuration:'另一种工况，只比较预测'};
const fmt = value => Number.isFinite(value) ? Number(value.toPrecision(7)) : '—';
const offsets = intervals => intervals.map(([lo,hi])=>lo===hi ? fmt(lo) : '['+fmt(lo)+', '+fmt(hi)+']').join(' 或 ');
const samePrediction = (a,b) => a.revision_id===b.revision_id && a.beam_uid===b.beam_uid;
const predictionLabel = model => '版本 #'+model.revision_id+(model.beam_name ? ' · '+model.beam_name : '');

function intervalVerdict(band, original, target, paired) {
  if (!samePrediction(original,target) && typeof band.reference_compatible === 'boolean' && typeof band.target_compatible === 'boolean') {
    if (band.reference_compatible && band.target_compatible) return paired
      ? '单看这个方向，两版都有相符的位置。'
      : '计入定位误差后，两版预测范围均与测量区间相交。';
    if (!band.reference_compatible && !band.target_compatible) return '计入定位误差后，两版预测范围仍与测量区间分开。';
    return '计入定位误差后，只有' + predictionLabel(band.reference_compatible ? original : target) + '的预测范围与测量区间相交。';
  }
  if (samePrediction(original,target) || typeof band.reference_inside !== 'boolean' || typeof band.target_inside !== 'boolean') return null;
  if (band.reference_inside && band.target_inside) return '这条读数无法区分两版。';
  if (!band.reference_inside && !band.target_inside) return '两版点预测都在区间外，请核对共同的实验条件与模型假设。';
  const compatible = band.reference_inside ? original : target;
  return '在这一测量界限内，只有' + predictionLabel(compatible) + '的点预测落入区间。';
}

export function BeamComparisonResult({item,caseId,onAsk}) {
  const {reading, reference_prediction:original, target_prediction:target} = item;
  const predictions = samePrediction(original,target) ? [target] : [original,target];
  const oldTargetEvidence={data_role:item.data_role,fit_input_ids:item.fit_input_ids,unverified_fit_ids:item.unverified_fit_ids};
  const axes = ['tangential','sagittal'].filter(axis=>reading[axis]!=null);
  const unit = reading.unit === 'um' ? 'μm' : 'mm';
  return <article className="beam-comparison-result">
    <h4>实测 #{item.reading_id} · {item.source.path}</h4>
    <p>{reading.locator} · {reading.conditions} · {reading.wavelength_nm ?? '未知'} nm</p>
    {target.comparison_scope && <p>原始读数 · {axes.map(axis=>(axis==='tangential'?'T ':'S ')+fmt(reading[axis])+' '+unit).join(' / ')}</p>}
    <p>{item.position_reference==='fixed_z'?'从共同零点量起':'从同一元件入口量起'} {fmt(item.used_position_mm)} mm
      {' · '}{item.position_mm == null ? '沿用登记坐标' : '采用补充坐标'}。{item.registration_note}</p>
    <table><caption>{widthNames[reading.definition]} / {unit}</caption><thead><tr><th>模型及测点</th><th>方向</th>
      <th>仪器读数</th><th>模型预测</th><th>实测 − 预测</th></tr></thead><tbody>
      {predictions.flatMap(p=>axes.map(axis=><tr key={p.revision_id + ':' + p.beam_uid + ':' + axis}>
        <th>{p === target ? '待检验' : '原登记'} · {p.model_kind==='draft'?'快照':'工程'} #{p.revision_id} {p.beam_name}
          {p.z_mm != null && <small>{p.element_name} · z = {fmt(p.z_mm)} mm</small>}</th>
        <td>{readingAxis(axis,reading.axes_aligned)}</td><td>{p.comparison_scope ? '—' : fmt(reading[axis])}</td>
        <td>{fmt(p.widths?.[axis]?.predicted)}
          {p.widths?.[axis]?.position_width_range && <small>定位范围内 [{p.widths[axis].position_width_range.map(fmt).join(', ')}]</small>}
          {!!p.widths?.[axis]?.compatible_offsets_mm?.length && <small>可解释该读数的轴向偏移 {offsets(p.widths[axis].compatible_offsets_mm)} mm</small>}
          {p.widths?.[axis]?.position_issue && <small>{p.widths[axis].position_issue}</small>}</td>
        <td>{fmt(p.widths?.[axis]?.residual)}{item.position_bound && <small>标称位置</small>}</td></tr>))}
    </tbody></table>
    {Object.entries(item.measurement_intervals||{}).map(([axis,band])=><p key={axis}>
      {axis==='tangential'?'T':'S'} 测量区间 [{fmt(band.lower)}, {fmt(band.upper)}] {unit}。
      {!samePrediction(original,target) && typeof band.reference_inside==='boolean' && ('原版点预测在区间'+(band.reference_inside?'内。':'外。'))}
      {typeof band.target_inside==='boolean' && ('待检验点预测在区间'+(band.target_inside?'内。':'外。'))}
      {(samePrediction(original,target) || typeof band.reference_compatible!=='boolean') && typeof band.target_compatible==='boolean'
        && (band.target_compatible?'待检验模型的宽度范围与测量区间相交。':'待检验模型的宽度范围与测量区间分开。')}
      <strong> {intervalVerdict(band, original, target, axes.length>1)}</strong>
    </p>)}
    {item.width_bounds && <p>测量界限依据 · {item.width_bounds.source}。此区间只计入已声明的测量误差。</p>}
    {item.position_bound && <p>模型宽度范围计入轴向位置 ±{fmt(item.position_bound.plus_minus_mm)} mm · {item.position_bound.source}</p>}
    {predictions.map(p=><React.Fragment key={p.revision_id+':'+p.beam_uid}>
      {p.common_offsets_mm && <p><strong>版本 #{p.revision_id} · 双轴共用一个测量面</strong>。
        {p.common_offsets_mm.length ? '两轴同时可相符的轴向偏移为 '+offsets(p.common_offsets_mm)+' mm。' : '在所给界限内，没有一个位置能同时解释 T 和 S 两条读数。'}</p>}
      {p.message && <p>版本 #{p.revision_id} · {p.message}</p>}
      {p.comparison_scope && <p>{p.comparison_scope}</p>}
      {Object.entries(p.axis_notes||{}).map(([axis,note])=><p key={axis}>版本 #{p.revision_id} · {axis==='sagittal'?'S':'T'} · {note}</p>)}
      {p.model_warnings?.map(note=><p key={note}>{note}</p>)}
    </React.Fragment>)}
    <div className="check-outcomes">{predictions.map(p=><section key={p.revision_id+':'+p.beam_uid}>
      <h5>{predictionLabel(p)} · 读数与模型的关系</h5>
      <EvidenceRole evidence={p.evidence ?? (p===target?oldTargetEvidence:null)}/>
      {!p.evidence && p!==target && <small>此历史对照只登记了待检验模型的数据用途。</small>}
    </section>)}</div>
    <small>对照记录 #{item.id}</small>
    <ReadingOriginal key={item.id} caseId={caseId} source={item.source}/>
    {onAsk && <button type="button" className="bench-text-btn" onClick={()=>onAsk('关于实测对照 #'+item.id+'：\n',
      {artifact_ids:[item.id],project_revision_id:item.revision_id})}>引用实测对照问 Agent</button>}
  </article>;
}

export default function BeamComparison({caseId, revision, paths=[], disabled, refreshToken,onAsk}) {
  const [open,setOpen]=useState(false), [readings,setReadings]=useState([]), [records,setRecords]=useState([]);
  const [readingCursor,setReadingCursor]=useState(null), [recordCursor,setRecordCursor]=useState(null);
  const [chosen,setChosen]=useState(''), [reference,setReference]=useState(''), [coordinate,setCoordinate]=useState('');
  const [targetBeam,setTargetBeam]=useState('');
  const [originalRole,setOriginalRole]=useState('unspecified');
  const [note,setNote]=useState(''), [role,setRole]=useState('unspecified'), [recordId,setRecordId]=useState(null);
  const [bounds,setBounds]=useState({}), [boundSource,setBoundSource]=useState('');
  const [positionLimit,setPositionLimit]=useState({mm:'',source:''});
  const [busy,setBusy]=useState(false), [loaded,setLoaded]=useState(null), [error,setError]=useState(''), [attempt,setAttempt]=useState(0);
  const key=JSON.stringify([caseId,revision,refreshToken,attempt]);
  const loading=loaded!==key;
  useEffect(()=>{
    if (!open) return;
    let active=true;
    Promise.all([workbenchApi.beamObservations(caseId),workbenchApi.beamComparisons(caseId,revision)])
      .then(([sources,history])=>{if(active){setError('');setReadings(sources.readings);setReadingCursor(sources.next_before_id);
        setRecords(history.items);setRecordCursor(history.next_before_id);}})
      .catch(e=>{if(active)setError(e.message);}).finally(()=>{if(active)setLoaded(key);});
    return ()=>{active=false;};
  },[caseId,revision,key,open]);
  const reading=readings.find(r=>r.id===Number(chosen)) || (!chosen && readings[0]);
  const distinct=reading && (reading.revision_id!==revision || (targetBeam && reading.beam_uid!==targetBeam));
  const record=records.find(r=>r.id===recordId)||records[0];
  const more=async(type)=>{
    setBusy(true);setError('');
    try{
      if(type==='readings'){
        const page=await workbenchApi.beamObservations(caseId,readingCursor);
        setReadings(old=>[...old,...page.readings]);setReadingCursor(page.next_before_id);
      }else{
        const page=await workbenchApi.beamComparisons(caseId,revision,recordCursor);
        setRecords(old=>[...old,...page.items]);setRecordCursor(page.next_before_id);
      }
    }catch(e){setError(e.message);}finally{setBusy(false);}
  };
  const submit=async e=>{
    e.preventDefault();if(!reading||!reference||!note.trim())return;
    setBusy(true);setError('');
    try{
      const limits=Object.fromEntries(Object.entries(bounds).filter(([,v])=>v!=='').map(([axis,v])=>[axis,Number(v)]));
      const result=await workbenchApi.compareBeamReading(caseId,revision,{reading_id:reading.id,position_reference:reference,
        ...(targetBeam ? {target_beam_uid:targetBeam} : {}),
        position_mm:coordinate===''?null:Number(coordinate),registration_note:note.trim(),data_role:role,
        ...(distinct && originalRole!=='unspecified' ? {reference_data_role:originalRole} : {}),
        ...(positionLimit.mm!=='' ? {position_bound:{plus_minus_mm:Number(positionLimit.mm),source:positionLimit.source.trim()}} : {}),
        ...(Object.keys(limits).length?{width_bounds:{...limits,source:boundSource.trim()}}:{})});
      setRecords(old=>[result,...old.filter(r=>r.id!==result.id)]);setRecordId(result.id);
    }catch(e){setError(e.message);}finally{setBusy(false);}
  };
  return <details className="beam-comparison" onToggle={e=>{if(e.target===e.currentTarget){setOpen(e.currentTarget.open);
    if(e.currentTarget.open)setAttempt(n=>n+1);}}}>
    <summary>用实验读数检验这版光路</summary>{open && <>
      {loading && <p role="status">读取已登记的光斑…</p>}
      {!loading && !error && !readings.length && <p>在光路的观察面登记一次实测宽度后，可以用同一读数检验多个版本。</p>}
      {!!readings.length && <form onSubmit={submit}><fieldset disabled={busy||loading||disabled}>
        <label>已登记的光斑<select value={reading?.id??''} onChange={e=>{setChosen(e.target.value);setTargetBeam('');setCoordinate('');setNote('');setRole('unspecified');setOriginalRole('unspecified');setReference('');setBounds({});setBoundSource('');setPositionLimit({mm:'',source:''});}}>
          {readings.map(r=><option key={r.id} value={r.id}>#{r.id} · {r.source_path} · {r.locator} · {widthNames[r.definition]} · 原登记 z = {r.z_mm} mm</option>)}</select></label>
        {readingCursor && <button type="button" onClick={()=>more('readings')}>更早的读数</button>}
        {(paths.length>0 || reading?.beam_uid!=='primary') && <label>此读数在待检验模型中对应哪束光<select value={targetBeam} onChange={e=>{setTargetBeam(e.target.value);setNote('');}}>
          <option value="">沿用原光束标识</option><option value="primary">主光束 / 腔模</option>
          {paths.map(p=><option key={p.uid} value={p.uid}>{p.name}</option>)}</select></label>}
        <div className="section-fields"><label>探测器如何定位<select required value={reference} onChange={e=>{setReference(e.target.value);setCoordinate('');setNote('');}}>
          <option value="">选择定位依据</option><option value="fixed_z">固定在两版共用的轴向坐标</option>
          <option value="element_depth">跟随原元件，按入口深度定位</option></select></label>
        <label>{reference==='element_depth'?'补充入口深度 / mm':'补充轴向位置 / mm'}<input type="number" step="any" disabled={!reference}
          placeholder="留空沿用原登记位置" value={coordinate} onChange={e=>setCoordinate(e.target.value)}/></label>
        <label>读数与待检验模型的关系<select value={role} onChange={e=>setRole(e.target.value)}>
          {Object.entries(roles).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label></div>
        <label>定位与工况依据<input required maxLength={800} placeholder="例如台架记录的零点、安装方式和仪器轴向" value={note} onChange={e=>setNote(e.target.value)}/></label>
        {distinct && <details><summary>原登记模型的数据用途</summary><label>版本 #{reading.revision_id} 与这条读数的关系
          <select value={originalRole} onChange={e=>setOriginalRole(e.target.value)}>
            {Object.entries(readingUses).map(([v,label])=><option key={v} value={v}>{label}</option>)}</select></label></details>}
        <details><summary>测量有已知的误差界限</summary><div className="section-fields">
          {['tangential','sagittal'].filter(a=>reading?.[a]!=null).map(a=><label key={a}>{a==='tangential'?'T':'S'} 宽度绝对界限 / {reading.unit==='um'?'μm':'mm'}
            <input type="number" min="0" step="any" value={bounds[a]??''} onChange={e=>setBounds({...bounds,[a]:e.target.value})}/></label>)}
          <label>误差界限依据<input maxLength={500} value={boundSource} required={Object.values(bounds).some(v=>v!=='')}
            onChange={e=>setBoundSource(e.target.value)}/></label></div>
          <DetectorPositionBound value={positionLimit} onChange={setPositionLimit}/></details>
        <button className="btn btn-secondary" disabled={!reference||!note.trim()}>计算仪器应读到的宽度</button>
      </fieldset></form>}
      {error && <p role="alert">{error} <button type="button" onClick={()=>setAttempt(n=>n+1)}>重新读取</button></p>}
      {record && <>{records.length>1 && <label>已存对照<select value={record.id} onChange={e=>setRecordId(Number(e.target.value))}>
        {records.map(r=><option key={r.id} value={r.id}>#{r.id} · {r.source.path} · {r.registration_note}</option>)}</select></label>}
        <BeamComparisonResult item={record} caseId={caseId} onAsk={onAsk}/></>}
      {recordCursor && <button type="button" disabled={busy||loading} onClick={()=>more('records')}>更早的对照结果</button>}
    </>}
  </details>;
}
