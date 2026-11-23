import React, { useEffect, useMemo, useState } from 'react';
import { workbenchApi } from '../api/workbench';
import { widthNames } from './beamWidths';
import ScientificPlot from './ScientificPlot';
import DetectorPositionBound from './DetectorPositionBound';
import ModelInputComparison from './ModelInputComparison';
import PredictionReadingCheck from './PredictionReadingCheck';

const format = value => Number.isFinite(value) ? Number(value.toPrecision(7)) : '—';
const modelName = model => (model.variant_name || (model.kind === 'draft' || model.kind === 'optical_snapshot' ? '快照' : '工程')) + ' #' + (model.id ?? model.revision_id);
const fields = {other:'', beam:'primary', region:'host', frame:'fixed_z', element:'', side:'', axis:'tangential',
  definition:'diameter_1e2', unit:'um', positions:'', note:'', bound:'', source:''};

function WidthCell({sample}) {
  return <td>{sample.width != null ? <>{format(sample.width)}
    {sample.reading_interval && <small>可读区间 [{sample.reading_interval.map(format).join(', ')}]</small>}
    {!sample.reading_interval && sample.position_width_range && <small>定位范围内 [{sample.position_width_range.map(format).join(', ')}]</small>}</> : '—'}
    {sample.element && <small>{sample.element} · z = {format(sample.z_mm)} mm{sample.medium_n != null && ' · n = '+format(sample.medium_n)}</small>}
    {(sample.issue || sample.position_issue) && <small>{sample.issue || sample.position_issue}</small>}</td>;
}

function ComparisonPlot({item, unit}) {
  const view = useMemo(()=>{
    const labels={forecast:'',nominal_only:' · 仅有标称值',incomparable:' · 此处不可作差'};
    const kind=(p,key)=>{
      if(p.difference==null)return 'incomparable';
      const sample=p[key];
      if(sample.position_issue || (item.position_bound && !sample.position_width_range)
        || (item.absolute_width_bound!=null && !sample.reading_interval))return 'nominal_only';
      return 'forecast';
    };
    const data = ['reference','target'].flatMap((key,index)=>Object.keys(labels).flatMap(group=>{
      const points=item.points.filter(p=>Number.isFinite(p[key].width) && kind(p,key)===group);
      if (!points.length) return [];
      const widths = points.map(p=>p[key].width);
      const symbol={forecast:index?'cross':'circle-open',nominal_only:'triangle-up-open',incomparable:'diamond-open'}[group];
      const trace = {type:'scatter', mode:'markers', name:modelName(item.models[index])+labels[group],meta:{evidence:group},
        x:points.map(p=>p.position_mm), y:widths,
        marker:{symbol,size:index?8:13,color:index?'#f2b76d':'#76d6cd'},
        text:points.map(p=>p.issue || p[key].position_issue || (group==='nominal_only'?'该点缺少可核对的误差区间。':'')),
        hovertemplate:'%{x:.6g} mm · %{y:.6g} '+unit+'<br>%{text}<extra>%{fullData.name}</extra>'};
      const ranges=points.map(p=>p[key].reading_interval ?? (item.absolute_width_bound == null ? p[key].position_width_range : null));
      if (ranges.some(Boolean)) trace.error_y={type:'data', symmetric:false,
        array:ranges.map((r,i)=>r ? Math.max(0,r[1]-widths[i]) : null),
        arrayminus:ranges.map((r,i)=>r ? Math.max(0,widths[i]-r[0]) : null), visible:true};
      return [trace];
    }));
    const layout = {autosize:true,margin:{l:70,r:22,b:51,t:29},paper_bgcolor:'#131d2a',plot_bgcolor:'#131d2a',
      font:{color:'#c7d6df'},legend:{orientation:'h',y:1.14},
      xaxis:{title:{text:(item.position_reference==='fixed_z'?'台架坐标 z':'元件入口深度')+' / mm'},gridcolor:'#44515e'},
      yaxis:{title:{text:widthNames[item.definition]+' / '+unit},gridcolor:'#44515e',rangemode:'tozero'}};
    return {data,layout,count:item.points.filter(p=>Number.isFinite(p.reference.width)||Number.isFinite(p.target.width)).length};
  },[item,unit]);
  return view.count>1 && <ScientificPlot data={view.data} layout={view.layout} filename={'width-comparison-'+item.id}
    ariaLabel={'各测点已算出的'+widthNames[item.definition]+'，可比较性及数值见下表'}/>;
}

export function PredictionResult({item,caseId,onAsk,disabled}) {
  const [a,b] = item.models;
  const unit = item.unit === 'um' ? 'μm' : 'mm';
  const best = item.largest_at_mm;
  const bounded = item.absolute_width_bound != null;
  const separated = item.points.filter(p=>p.disjoint);
  return <article className="beam-comparison-result">
    <h4>已存预测比较 #{item.id}</h4>
    <p>{modelName(a)} {a.title} · {a.beam_name} {a.wavelength_nm ?? '—'} nm<br/>
      {modelName(b)} {b.title} · {b.beam_name} {b.wavelength_nm ?? '—'} nm</p>
    <p>{item.region === 'incident_train' ? '接入前支路' : '主光路区段'} · {item.position_reference === 'fixed_z' ? '台架公共坐标' : '距定位元件入口的深度'}。
      {item.boundary_side && (item.boundary_side === 'upstream' ? '界面前取样。' : '界面后取样。')}{item.registration_note}</p>
    {!!item.separated_at_mm?.length && <p><strong>优先补测 {item.separated_at_mm.map(format).join('、')} mm。在所给界限内，两版可能读数的最小间隔约为 {Number(item.maximum_clearance.toPrecision(3))} {unit}。</strong></p>}
    {item.position_bound && <p>轴向位置 ±{format(item.position_bound.plus_minus_mm)} mm · {item.position_bound.source}</p>}
    {best.length > 0 && <p>按标称位置计算，{best.map(format).join('、')} mm 处的预测差最大，为 {format(item.largest_difference)} {unit}。</p>}
    {item.largest_difference === 0 && <p>在可比较的位置，两版预测在计算精度内相同。</p>}
    {item.largest_difference == null && <p>所列位置均缺少可直接比较的预测，原因见下表。</p>}
    {bounded ? <p>仪器读数界限 ±{format(item.absolute_width_bound)} {unit}，依据为{item.bound_source}。
      {separated.length ? '其中 ' + separated.length + ' 个位置的两段可读区间分开。' : item.points.some(p=>p.disjoint===false) ? '已求出的可读区间均有交集。' : '当前测点缺少可比较的读数区间。'}</p>
      : <p>仪器误差界限尚未给出，可先比较预测差值。</p>}
    <ComparisonPlot item={item} unit={unit}/>
    {caseId && <ModelInputComparison key={item.id} caseId={caseId} referenceId={item.reference_revision_id} revision={item.revision_id}/>}
    <details open={item.points.length<=8 || undefined}><summary>各测点预测及定位</summary>
    <table><caption>{item.axis === 'tangential' ? 'T 向' : 'S 向'} · {widthNames[item.definition]} / {unit}</caption>
      <thead><tr><th>{item.position_reference === 'fixed_z' ? 'z' : '深度'} / mm</th><th>{modelName(a)}</th><th>{modelName(b)}</th><th>绝对差 / {unit}</th></tr></thead>
      <tbody>{item.points.map(p=><tr key={p.position_mm}><th>{format(p.position_mm)}</th>
        <WidthCell sample={p.reference}/><WidthCell sample={p.target}/><td>{p.issue || format(p.difference)}{p.sampling_note && <small>{p.sampling_note}</small>}
          {typeof p.disjoint === 'boolean' && <small>{p.disjoint ? '区间分开' : '区间未分开'}{p.clearance>0 && ' · 间隔 '+format(p.clearance)+' '+unit}</small>}</td></tr>)}</tbody></table></details>
    {item.models.flatMap(m=>(m.warnings || []).map(w=><p key={m.revision_id+w}>{modelName(m)} · {w}</p>))}
    <p className="field-note">{item.scope}</p>
    {caseId && <PredictionReadingCheck key={'reading-'+item.id} caseId={caseId} prediction={item} onAsk={onAsk} disabled={disabled}/>}
  </article>;
}

export default function PredictionComparison({caseId, revision, project, readWorkingDraft, disabled, refreshToken, onAsk}) {
  const [open,setOpen]=useState(false), [attempt,setAttempt]=useState(0), [loaded,setLoaded]=useState(null);
  const [form,setForm]=useState(fields), [versions,setVersions]=useState([]), [records,setRecords]=useState([]);
  const [positionLimit,setPositionLimit]=useState({mm:'',source:''});
  const [versionCursor,setVersionCursor]=useState(null), [recordCursor,setRecordCursor]=useState(null);
  const [selected,setSelected]=useState(null), [busy,setBusy]=useState(false), [error,setError]=useState('');
  const key=[caseId,revision,attempt,refreshToken].join('/');
  useEffect(()=>{
    if (!open) return;
    let current=true;
    Promise.all([workbenchApi.modelVersions(caseId),workbenchApi.predictionComparisons(caseId)])
      .then(([available,history])=>{
        if (!current) return;
        setVersions(available.items);setVersionCursor(available.next_before_id);
        setRecords(history.items);setRecordCursor(history.next_before_id);setError('');
      }).catch(cause=>{if(current)setError(cause.message);})
      .finally(()=>{if(current)setLoaded(key);});
    return ()=>{current=false;};
  },[caseId,open,key]);
  const alternatives=versions.filter(v=>readWorkingDraft || v.id!==revision);
  const otherId=form.other || (readWorkingDraft ? revision : null);
  const reference=alternatives.find(v=>v.id===Number(otherId));
  function change(values) { setForm(previous=>({...previous,...values})); }
  async function more(type) {
    setBusy(true); setError('');
    try {
      if(type === 'versions') {
        const page=await workbenchApi.modelVersions(caseId,versionCursor);
        setVersions(v=>[...v,...page.items]);setVersionCursor(page.next_before_id);
      } else {
        const page=await workbenchApi.predictionComparisons(caseId,recordCursor);
        setRecords(v=>[...v,...page.items]);setRecordCursor(page.next_before_id);
      }
    } catch(cause) {setError(cause.message);} finally {setBusy(false);}
  }
  async function compare(event) {
    event.preventDefault(); setError('');
    if(!reference) return;
    const positions=form.positions.trim().split(/[\s,，、;；]+/).filter(Boolean).map(Number);
    if(!positions.length || !positions.every(Number.isFinite) || positions.length>48 || positions.length!==new Set(positions).size) {
      setError('请填写 1 至 48 个互不重复的有限坐标，用空格或逗号分隔。');return;
    }
    setBusy(true);
    try {
      const comparison={
        reference_revision_id:reference.id, beam_uid:form.beam, region:form.region, axis:form.axis,
        definition:form.definition, unit:form.unit, position_reference:form.frame, positions_mm:positions,
        registration_note:form.note.trim(), element_uid:form.frame==='element_depth' ? form.element : null,
        boundary_side:form.frame==='fixed_z' ? form.side || null : null,
        ...(positionLimit.mm!=='' ? {position_bound:{plus_minus_mm:Number(positionLimit.mm),source:positionLimit.source.trim()}} : {}),
        ...(form.bound!=='' ? {absolute_width_bound:Number(form.bound),bound_source:form.source.trim()} : {})
      };
      const result=readWorkingDraft ? await workbenchApi.compareDraftPredictions(caseId,readWorkingDraft(),comparison)
        : await workbenchApi.comparePredictions(caseId,revision,comparison);
      setRecords(old=>[result,...old.filter(r=>r.id!==result.id)]);setSelected(result.id);
      if(readWorkingDraft) setAttempt(n=>n+1);
    } catch(cause) {setError(cause.message);} finally {setBusy(false);}
  }
  const paths=project.incident_paths || [];
  const elements=form.region==='host' ? project.system.elements : paths.find(p=>p.uid===form.beam)?.elements || [];
  const record=records.find(r=>r.id===selected) || records[0];
  return <details className="prediction-comparison" onToggle={e=>{if(e.target===e.currentTarget)setOpen(e.currentTarget.open);}}>
    <summary>比较两版光路的预测</summary>{open && <>
      {loaded!==key && <p role="status">读取模型版本与已存比较…</p>}
      {loaded===key && !alternatives.length && !versionCursor && !error && <p>保存另一版光路或保留一份试算快照后，可以在同一测点比较两个模型。也可以在画布上调整已有版本，直接与原版比较。</p>}
      {(alternatives.length>0 || versionCursor) && <form onSubmit={compare}><fieldset disabled={disabled || busy || loaded!==key}>
        {readWorkingDraft && <p>本次比较会保存用于计算的画布草稿快照。决定采用时，可将草稿另存为工程版本。</p>}
        <label>{readWorkingDraft ? '与画布草稿比较' : '与当前版本 #'+revision+' 比较'}<select required value={reference?.id ?? ''} onChange={e=>change({other:e.target.value})}>
          <option value="" disabled={!!readWorkingDraft && !!reference}>选择工程或试算快照</option>{alternatives.map(v=><option value={v.id} key={v.id}>{modelName(v)} · {v.title} · {v.change_note}</option>)}</select></label>
        {versionCursor && <button type="button" onClick={()=>more('versions')}>更早的模型版本</button>}
        <div className="section-fields"><label>比较哪束光<select value={form.beam} onChange={e=>change({beam:e.target.value,region:'host',element:''})}>
          <option value="primary">主光束 / 腔模</option>{paths.map(p=><option value={p.uid} key={p.uid}>{p.name}</option>)}</select></label>
        {form.beam!=='primary' && <label>取样区段<select value={form.region} onChange={e=>change({region:e.target.value,element:''})}>
          <option value="host">接入后的主光路</option><option value="incident_train">接入前的入射支路</option></select></label>}
        <label>仪器方向<select value={form.axis} onChange={e=>change({axis:e.target.value})}><option value="tangential">切向 T</option><option value="sagittal">弧矢 S</option></select></label>
        <label>仪器宽度定义<select value={form.definition} onChange={e=>change({definition:e.target.value,bound:'',source:''})}>
          {Object.entries(widthNames).filter(([k])=>k!=='unknown').map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>
        <label>读数单位<select value={form.unit} onChange={e=>change({unit:e.target.value,bound:'',source:''})}><option value="um">μm</option><option value="mm">mm</option></select></label>
        <label>候选测点如何定位<select value={form.frame} onChange={e=>change({frame:e.target.value,element:'',positions:''})}>
          <option value="fixed_z">从公共台架零点量起</option><option value="element_depth">从同一元件入口量起</option></select></label>
        {form.frame==='fixed_z' ? <label>恰在界面时<select value={form.side} onChange={e=>change({side:e.target.value})}>
          <option value="">有歧义时标出</option><option value="upstream">界面前</option><option value="downstream">界面后</option></select></label> :
          <label>定位元件<select required value={form.element} onChange={e=>change({element:e.target.value})}><option value="">选择均匀传播段</option>
            {elements.filter(e=>e.kind==='space' && e.uid).map(e=><option key={e.uid} value={e.uid}>{e.name}</option>)}</select></label>}</div>
        <label>可测位置 / mm<input required value={form.positions} placeholder="用空格或逗号分隔" onChange={e=>change({positions:e.target.value})}/></label>
        <label>定位与测量条件<input required maxLength={800} value={form.note} placeholder="两版如何对齐，仪器能到达哪些位置、沿哪个方向读数" onChange={e=>change({note:e.target.value})}/></label>
        <details><summary>计入仪器绝对误差</summary><div className="section-fields">
          <label>单次读数的 ± 界限 / {form.unit==='um'?'μm':'mm'}<input type="number" step="any" min="0" value={form.bound} onChange={e=>change({bound:e.target.value})}/></label>
          <label>界限依据<input maxLength={500} required={form.bound!==''} value={form.source} onChange={e=>change({source:e.target.value})}/></label></div>
          <DetectorPositionBound value={positionLimit} onChange={setPositionLimit}/></details>
        <button className="btn btn-secondary" disabled={!reference || !form.note.trim() || !form.positions.trim()}>计算各测点的预测差</button>
      </fieldset></form>}
      {error && <p role="alert">{error} <button type="button" onClick={()=>setAttempt(n=>n+1)}>重新读取</button></p>}
      {records.length>1 && <label>该案例的已存预测比较<select value={record.id} onChange={e=>setSelected(Number(e.target.value))}>
        {records.map(r=><option key={r.id} value={r.id}>#{r.id} · {r.models.map(modelName).join(' / ')} · {r.registration_note}</option>)}</select></label>}
      {recordCursor && <button type="button" disabled={busy} onClick={()=>more('records')}>更早的预测比较</button>}
      {record && <><PredictionResult item={record} caseId={caseId} onAsk={onAsk} disabled={disabled}/>
        {onAsk && <button type="button" className="btn btn-secondary" onClick={()=>onAsk('关于这份预测比较：\n',
          {artifact_ids:[record.id], project_revision_id:record.revision_id})}>引用这份比较问 Agent</button>}</>}
    </>}
  </details>;
}
