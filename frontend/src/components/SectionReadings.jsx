import React, { useState } from 'react';
import { workbenchApi } from '../api/workbench';
import { FilePreview } from './CaseFiles';

import {readingAxis, widthNames as names} from './beamWidths';
const defaults = { attachment:'', locator:'', conditions:'', wavelength:'', definition:'unknown', unit:'um', t:'', s:'', aligned:false };
const number = s => s === '' ? null : Number(s);

function ReadingSource({item,file}) {
  const [open,setOpen]=useState(false);
  return <details onToggle={event=>setOpen(event.currentTarget.open)}>
    <summary>核对登记原件</summary>
    {open && <><p>登记时的 SHA-256 · {item.source_sha256}</p>
      {file ? <FilePreview key={file.id} file={file}/> : <p>当前案例中找不到该原件。登记时的文件名、位置和校验值保留在记录中。</p>}</>}
  </details>;
}

export default function SectionReadings({caseId, revision, plane, model, files, dirty, paths, readings}) {
  const [form,setForm]=useState(defaults), [chosen,setChosen]=useState('primary');
  const [error,setError]=useState(''), [busy,setBusy]=useState(false), [preview,setPreview]=useState(false);
  const [notice,setNotice]=useState('');
  const update=(key,value)=>setForm(old=>({...old,[key]:value}));
  const file=files.find(f=>f.id===Number(form.attachment));
  const beams=(model?.beams || []).filter(b=>b.registration_available!==false);
  const beam=beams.find(b=>b.uid===chosen) || beams[0];
  const beamName=uid=>uid==='primary'?'主光束':paths.find(p=>p.uid===uid)?.name || uid;
  const records=readings.items.filter(x=>x.revision_id===revision && x.plane_uid===plane.uid);
  const save=async event=>{
    event.preventDefault();
    if(busy || dirty || !file || !beam)return;
    setBusy(true);setError('');setNotice('');
    try {
      const item=await workbenchApi.recordBeamReading(caseId,{revision_id:revision,plane_uid:plane.uid,beam_uid:beam.uid,
        attachment_id:file.id,locator:form.locator,conditions:form.conditions,wavelength_nm:number(form.wavelength),
        definition:form.definition,axes_aligned:form.aligned,unit:form.unit,tangential:number(form.t),sagittal:number(form.s)});
      readings.record(item);setNotice('已登记原始读数，记录 #'+item.id+'。');
    } catch(cause){setError(cause.message);} finally{setBusy(false);}
  };
  return <details className="section-readings" id={'register-reading-'+caseId}><summary>实验读数{revision?' · 工程 #'+revision:''} · {records.length} 条</summary>
    {records.map(item=><article key={item.id}><strong>{beamName(item.beam_uid)} · {item.source_path}</strong>
      <p>{item.locator} · {item.conditions} · {item.wavelength_nm??'未知'} nm · z = {item.z_mm} mm</p>
      <p>{names[item.definition]} · {readingAxis('tangential',item.axes_aligned)} {item.tangential??'—'} / {readingAxis('sagittal',item.axes_aligned)} {item.sagittal??'—'} {item.unit==='um'?'μm':'mm'} · {item.transcribed_by==='agent'?'Agent 转录':'手工转录'}</p>
      {item.definition==='fwhm_diameter' && Object.entries(item.native_widths||{}).filter(([,v])=>v.predicted!=null).map(([axis,v])=>
        <p key={axis}>高斯模型预测 FWHM · {axis==='tangential'?'T':'S'} {Number(v.predicted.toPrecision(6))} {item.unit==='um'?'μm':'mm'}
          {' · 实测 − 预测 '}{Number(v.residual.toPrecision(6))} {item.unit==='um'?'μm':'mm'}</p>)}
      {item.comparison_note && <p>{item.comparison_note}</p>}
      {!!Object.keys(item.measured_minus_model_mm).length && <p>实测减版本模型（μm） {Object.entries(item.measured_minus_model_mm).map(([axis,v])=>(axis==='tangential'?'T ':'S ')+Number((v*1000).toPrecision(5))).join(' / ')}</p>}
      {Object.entries(item.axis_notes||{}).map(([a,n])=><p key={a}>{a==='tangential'?'T':'S'} · {n}</p>)}
      <ReadingSource item={item} file={files.find(f=>f.id===item.attachment_id)}/>
    </article>)}
    {!revision || dirty ? <p>保存当前光路后，可把测量登记到该版本的观察面。</p> : !files.length ? <p>先在案例“实验资料”中归档原件，再在这里关联。</p> : <form onSubmit={save}>
      <div className="section-fields"><label>实测光束<select value={beam?.uid??''} onChange={e=>setChosen(e.target.value)}>{beams.map(b=><option key={b.uid} value={b.uid}>{beamName(b.uid)}</option>)}</select></label>
      <label>实验原件<select aria-label="实验原件" required value={form.attachment} onChange={e=>update('attachment',e.target.value)}><option value="">选择原件</option>{files.map(f=><option value={f.id} key={f.id}>{f.relative_path||f.filename}</option>)}</select></label>
      <label>原件内的位置<input required value={form.locator} placeholder="工作表与单元格、图像区域等" onChange={e=>update('locator',e.target.value)}/></label>
      <label>批次与工况<input required value={form.conditions} onChange={e=>update('conditions',e.target.value)}/></label>
      <label>实测波长 / nm<input type="number" step="any" min="0" value={form.wavelength} onChange={e=>update('wavelength',e.target.value)}/></label>
      <label>仪器宽度口径<select value={form.definition} onChange={e=>update('definition',e.target.value)}>{Object.entries(names).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>
      <label>读数单位<select value={form.unit} onChange={e=>update('unit',e.target.value)}><option value="um">μm</option><option>mm</option></select></label>
      <label>实测 T<input type="number" step="any" min="0" value={form.t} onChange={e=>update('t',e.target.value)}/></label>
      <label>实测 S<input type="number" step="any" min="0" value={form.s} onChange={e=>update('s',e.target.value)}/></label></div>
      <label className="section-inline"><input type="checkbox" checked={form.aligned} onChange={e=>update('aligned',e.target.checked)}/>已将仪器读数方向对齐此面的 T/S 轴</label>
      <div className="section-actions">{file && <button type="button" className="btn btn-secondary" onClick={()=>setPreview(!preview)}>{preview?'收起原件':'核对原件'}</button>}
        <button className="btn btn-primary" disabled={busy || !beam || (!form.t && !form.s)}>登记实测读数</button></div>
      {preview && file && <FilePreview key={file.id} file={file}/>}
    </form>}
    {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
    {readings.error && <p role="alert">{readings.error} <button type="button" onClick={readings.refresh}>重读记录</button></p>}
  </details>;
}
