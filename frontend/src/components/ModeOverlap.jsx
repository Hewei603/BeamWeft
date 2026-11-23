import React, { useMemo, useState } from 'react';
import { workbenchApi } from '../api/workbench';
import { useBenchPreview } from './useBenchPreview';

const fmt = value => Number.isFinite(value) ? Number(value.toPrecision(6)).toString() : '—';
const percent = value => fmt(100*value)+'%';

export function ModeOverlapResult({result}) {
  const {plane, beams, intensity, coherent}=result;
  return <div className="mode-overlap-result">
    <p><strong>{beams[0].name} × {beams[1].name}</strong> · {plane.name} · z = {fmt(plane.z_mm)} mm</p>
    <dl><div><dt>横向强度相似度</dt><dd>{intensity.status==='completed' ? percent(intensity.cosine) : intensity.note}</dd></div>
      <div><dt>空间基模投影功率比例</dt><dd>{coherent.status==='completed' ? percent(coherent.power_fraction) : coherent.note}</dd></div></dl>
    <p>强度相似度反映两个光斑在此截面的形状、大小与位置是否一致。基模投影还计入波前曲率与指向，假设偏振相同。</p>
    {result.scope_warnings?.map(line=><p key={line} className="wb-warning">{line}</p>)}
    <details><summary>核对截面参数与假设</summary>
      <table><caption>沿登记光路正向 · 波前曲率 R 为正表示发散</caption>
        <thead><tr><th>光束 / 方向</th><th>波长 nm</th><th>半径 μm</th><th>中心 μm</th><th>角度 mrad</th><th>R mm</th></tr></thead>
        <tbody>{beams.flatMap(b=>['tangential','sagittal'].map(axis=>{
          const f=b.field_axes?.[axis], curvature=f?.reduced_vergence_per_mm;
          return <tr key={b.uid+axis}><th>{b.name} / {axis==='tangential'?'T':'S'}</th><td>{fmt(b.wavelength_nm)}</td>
            <td>{b.radii_mm?.[axis]==null?'—':fmt(b.radii_mm[axis]*1000)}</td><td>{f?.center_mm==null?'—':fmt(f.center_mm*1000)}</td>
            <td>{f?.reduced_angle==null || !b.index?'—':fmt(1000*f.reduced_angle/b.index)}</td>
            <td>{curvature==null || !b.index?'—':curvature===0?'∞':fmt(b.index/curvature)}</td></tr>;
        }))}</tbody></table>
      {intensity.status==='completed' && <p>单位功率的横向强度乘积积分 · {fmt(intensity.normalized_product_per_mm2)} mm⁻²</p>}
      {beams.some(b=>b.launch_basis==='ideal_on_axis') && <p>未指定入射偏移的支路沿理想光轴计算。</p>}
      {result.assumptions.map(line=><p key={line}>{line}</p>)}
    </details>
  </div>;
}

export default function ModeOverlap({caseId, draft, revision, dirty, plane, paths, selectedPath, disabled, onOpenStudy}) {
  const [open,setOpen]=useState(false), [left,setLeft]=useState('primary'), [right,setRight]=useState(null);
  const [pending,setPending]=useState(false), [receipt,setReceipt]=useState(null);
  const options=[{uid:'primary',name:draft.system.mode==='single_pass'?'主光束':'腔基模'},...paths];
  const first=options.find(b=>b.uid===left)?.uid || 'primary';
  const others=options.filter(b=>b.uid!==first);
  const second=others.find(b=>b.uid===right)?.uid || others.find(b=>b.uid===selectedPath)?.uid || others[0]?.uid;
  const request=useMemo(()=>({
    plane_uid:plane.uid,first_beam_uid:first,second_beam_uid:second,
    ...(!dirty && revision ? {revision_id:revision} : {layout:{system:draft.system,
      observation_planes:[plane],incident_paths:(draft.incident_paths || []).filter(p=>p.uid===first || p.uid===second)}})
  }),[plane,draft,first,second,dirty,revision]);
  const {data,error,retry}=useBenchPreview(caseId,request,open && !!second,workbenchApi.modeOverlap,revision);
  const sourceKey=JSON.stringify([caseId,revision,plane.uid,first,second]);
  async function save() {
    setPending(true);
    try {
      const definition=await workbenchApi.studyDefinition(caseId,'compare_section_modes');
      const inputs={plane_uid:plane.uid,first_beam_uid:first,second_beam_uid:second};
      const result=await workbenchApi.runStudy(caseId,definition,inputs,{layout:{
        revision_id:revision,port:'v1/layout',registration:'采用工程中登记的同一观察面和两束光的共向 T/S 坐标。'}});
      setReceipt({sourceKey,id:result.study_record.id});
    } catch(cause){setReceipt({sourceKey,error:cause.message});} finally{setPending(false);}
  }
  if(!second)return null;
  return <details className="mode-overlap" onToggle={e=>{if(e.target===e.currentTarget)setOpen(e.currentTarget.open);}}>
    <summary>比较截面光场</summary>{open && <>
      {options.length>2 && <div className="section-fields"><label>第一束光<select value={first} onChange={e=>setLeft(e.target.value)}>
        {options.map(b=><option key={b.uid} value={b.uid}>{b.name}</option>)}</select></label>
      <label>第二束光<select value={second} onChange={e=>setRight(e.target.value)}>
        {others.map(b=><option key={b.uid} value={b.uid}>{b.name}</option>)}</select></label></div>}
      {error ? <p role="alert">{error} <button onClick={retry}>重新计算</button></p> :
        data ? <ModeOverlapResult result={data}/> : <p role="status">正在计算选定截面的光场…</p>}
      {data && (dirty || !revision ? <small>保存工程或独立方案后，可以把该截面比较存入分析记录。</small> :
        receipt?.id && receipt.sourceKey===sourceKey ? <button type="button" className="btn btn-secondary"
          onClick={()=>onOpenStudy(receipt.id)}>打开截面比较记录 #{receipt.id}</button> :
        <button type="button" className="btn btn-secondary" disabled={disabled || pending} onClick={save}>
          {pending?'正在保存截面比较…':'保存截面比较到分析记录'}</button>)}
      {!dirty && receipt?.sourceKey===sourceKey && receipt.error && <p role="alert">{receipt.error}</p>}
    </>}
  </details>;
}
