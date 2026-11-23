import React, { useState } from 'react';
import { workbenchApi } from '../api/workbench';

const fmt = n => Number.isFinite(n) ? String(Number(n.toPrecision(6))) : '—';
const useLabels = {fit_input:'原拟合输入', fit_acquisition:'与拟合输入同次采集', unresolved_locator:'原件内位置待核对', repeated_cell:'重复引用', scored:'计入误差'};
const widthLabel = {d4sigma_diameter:'D4σ 全直径', diameter_1e2:'1/e² 全直径', radius_1e2:'1/e² 半径'};
const basisLabel = meta => meta.width_basis==='second_moment'?'D4σ 全直径':'1/e² 全直径';

export function PredictedWidths({result}) {
  return <div className="fit-data-table">{result.reference_study_id && <p>预测 #{result.id} · {result.recorded_at?.replace('T',' ')} · 采用拟合 #{result.reference_study_id}</p>}
    <table><caption>拟合 #{result.reference_study_id || result.id} · {result.metadata.axis} 向 · {basisLabel(result.metadata)}</caption>
    <thead><tr><th>拟合坐标 / mm</th><th>预测直径 / μm</th><th>采样范围</th></tr></thead>
    <tbody>{result.predictions.map((p,i)=><tr key={i}><td>{fmt(p.z_mm)}</td><td>{fmt(p.diameter_mm*1000)}</td><td>{p.extrapolated?'外推':'区间内'}</td></tr>)}</tbody>
  </table>{result.fit_warnings?.map(w=><p key={w} className="beam-fit-caution">{w}</p>)}</div>;
}

export function PredictAt({caseId, study, onSaved}) {
  const [input,setInput]=useState(''),[result,setResult]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const submit=async event=>{
    event.preventDefault();
    const positions=input.trim().split(/[\s,，]+/).filter(Boolean).map(Number);
    if(!positions.length || positions.some(v=>!Number.isFinite(v)) || positions.length>300){
      setError('请填写 1 至 300 个有限坐标，可用空格或逗号分隔。');return;
    }
    setBusy(true);setError('');setResult(null);
    try {
      setResult(await workbenchApi.runBeamStudy(caseId,{kind:'prediction',study_id:study.id,positions_mm:positions}));
      onSaved?.();
    }
    catch(e){setError(e.message);}finally{setBusy(false);}
  };
  return <details><summary>查看指定位置的预测</summary>
    <form onSubmit={submit}><p>{study.metadata.reference}。沿同一连续均匀介质取样。</p>
      <label>预测位置 / mm<input required disabled={busy} value={input} onChange={e=>{setInput(e.target.value);setResult(null);setError('');}} /></label>
      <button className="btn btn-secondary" disabled={busy}>{busy?'正在计算…':'计算并保存预测'}</button></form>
    {result && <PredictedWidths result={result}/>}
    {error && <p role="alert">{error}</p>}
  </details>;
}

function ResidualPlot({points,holdout=false}) {
  const rows=holdout ? points : points.filter(p=>p.use==='scored');
  if(rows.length<2)return null;
  const position=p=>holdout?p.z_mm:p.model_z_mm;
  const scale=holdout?1000:2000, quantity=holdout?'半径':'全直径';
  const zs=rows.map(position), lo=Math.min(...zs), hi=Math.max(...zs);
  if(hi===lo)return null;
  const max=Math.max(...rows.map(p=>Math.abs(p.residual_mm)));
  const x=z=>62+(z-lo)/(hi-lo)*530;
  const y=r=>82-(max?r/max:0)*44;
  return <figure className="fit-figure fit-residual"><svg viewBox="0 0 636 164" role="img" aria-label={quantity+'预测偏差随测量位置的变化'}>
    <text x="62" y="25">实测 − 预测 · {quantity} / μm</text>
    {[-1,0,1].filter(sign=>max || sign===0).map(sign=><g key={sign}>
      <line x1="62" x2="592" y1={y(sign*max)} y2={y(sign*max)} stroke={sign?'#354957':'#a6bdc9'} strokeDasharray={sign?'2 4':undefined}/>
      <text x="55" y={y(sign*max)+4} textAnchor="end">{Number((scale*max*sign).toPrecision(3))}</text></g>)}
    {rows.map((p,i)=><circle key={i} cx={x(position(p))} cy={y(p.residual_mm)} r="4" fill={p.extrapolated?'#eeb57c':'#72d7ce'}>
      <title>z = {fmt(position(p))} mm · {fmt(scale*p.residual_mm)} μm</title></circle>)}
    <text x="62" y="149">{fmt(lo)} mm</text><text x="592" y="149" textAnchor="end">{fmt(hi)} mm</text>
  </svg><figcaption>零线上方表示实测光斑更宽。横轴沿用拟合的坐标基准。</figcaption></figure>;
}

export function HoldoutResult({item}) {
  const [all,setAll]=useState(false);
  const h=item.holdout;
  if(!h?.points?.length)return null;
  const requested=h.requested_positions_mm, additional=h.positions_mm.filter(z=>!requested.includes(z));
  const scored=h.status==='scored';
  return <section className="fit-holdout" aria-label="留出位置的预测检验">
    <h4>留出位置的预测检验</h4>
    <p>拟合选取 {h.training_positions_mm.length} 个位置，同一组资料的另外 {h.positions_mm.length} 个位置仅用于检验。
      {scored?<>各位置等权的半径误差均方根为 <strong>{fmt(h.station_rms_radius_mm*1000)} μm</strong>，单条读数的最大绝对半径误差为 {fmt(h.max_abs_radius_error_mm*1000)} μm。</>:'训练读数尚不能确定传播参数，留出读数已保留，预测及残差待定。'}</p>
    {additional.length>0 && <p>以下位置与所选位置共用采集标识，一并留出：{additional.map(z=>fmt(z)).join('、')} mm。</p>}
    <details><summary>查看留出测点与残差</summary>
      {scored && <ResidualPlot points={h.points} holdout/>}
      <div className="fit-data-table" tabIndex={0} role="region" aria-label="留出测点明细，可滚动"><table><caption>留出读数，残差按实测减预测计算</caption>
        <thead><tr><th>z / mm</th><th>半径残差 / μm</th><th>原始宽度</th><th>预测宽度</th><th>原件位置</th></tr></thead>
        <tbody>{(all?h.points:h.points.slice(0,25)).map((p,i)=>{
          const unit=(p.width_unit||item.source.width_unit)==='um'?'μm':'mm';
          const definition=widthLabel[p.definition||item.source.width_definition];
          const file=h.files?.find(f=>f.attachment_id===p.attachment_id);
          return <tr key={i}><td data-label="z / mm">{fmt(p.z_mm)}{p.extrapolated?' · 外推':''}</td>
            <td data-label="半径残差 / μm">{scored?fmt(p.residual_mm*1000):'待定'}</td><td data-label="原始宽度">{fmt(p.raw_width)} {unit} · {definition}</td>
            <td data-label="预测宽度">{scored?fmt(p.predicted_width)+' '+unit:'待定'}</td>
            <td data-label="原件位置">{file && <>{file.path} · </>}{p.row?'第 '+p.row+' 行':p.locator||'#'+p.reading_id}
              {p.acquisition_id && !file && <> · {p.acquisition_id}</>}</td></tr>;
        })}</tbody></table></div>
      {h.points.length>25 && <button type="button" onClick={()=>setAll(!all)}>{all?'收起其余留出读数':'查看全部 '+h.points.length+' 条留出读数'}</button>}
      <details><summary>核对采样分组与原件</summary>
        <p>参与拟合的位置为 {h.training_positions_mm.map(z=>fmt(z)).join('、')} mm。留出 {h.points.length} 条读数，同位置的重复读数全部留出。</p>
        {h.grouping_note && <p>{h.grouping_note}</p>}
        {h.uncertainty_note && <p>{h.uncertainty_note}</p>}
        {h.files?.map(f=><p key={f.attachment_id}>{f.path}<br/>SHA-256 · {f.sha256 || '未登记'}</p>)}
      </details>
    </details>
  </section>;
}

export function FitCheckResult({item}) {
  const [all,setAll]=useState(false);
  const {check,metadata,source,points}=item;
  const used=check.counts.scored || 0;
  const peak=check.largest_error, unit=peak?.unit==='um'?'μm':'mm';
  const zRange=points.filter(p=>p.extrapolated).length;
  return <article className="beam-fit-result" aria-label="已存拟合的测量对照">
    <h4>对照 #{item.id} · {metadata.axis} 向 · {metadata.conditions}</h4>
    <p>预测取自拟合 #{check.reference_study_id}，拟合参数保持固定。{used} / {points.length} 条读数计入误差。
      {used>0 && <>全直径误差均方根为 <strong>{fmt(check.diameter_rms_mm*1000)} μm</strong>，对应半径误差均方根为 {fmt(check.radius_rms_mm*1000)} μm。</>}</p>
    {check.forecast_id && <><p>对照采用预测 #{check.forecast_id} 中已保存的数值。</p>{check.archive_evidence.map(f=><p key={f.attachment_id}>{f.path} · {{
      already_present:'保存预测时，案例已有同内容原件。',added_later:'对照原件在预测之后归档。',uncertain:'早期归档缺少校验值，原件先后关系待核对。'}[f.state]}</p>)}</>}
    {used<points.length && <p>表内标出了重复使用的读数及仍需核对的原件位置。</p>}
    {peak && <p>最大偏差在 <strong>z = {fmt(peak.model_z_mm)} mm</strong>。{widthLabel[peak.definition]}实测 {fmt(peak.raw_width)} {unit}，预测 {fmt(peak.predicted_width)} {unit}，相差 {fmt(peak.width_residual)} {unit}。</p>}
    {check.scored_positions<used && <p>{check.scored_positions} 个不同位置有 {used} 条读数。误差均方根按读数等权，重复采样的位置占有较多权重。</p>}
    {zRange>0 && <p className="beam-fit-caution">{zRange} 条读数落在原拟合采样范围外，采用外推值。</p>}
    {check.fit_warnings.map(w=><p className="beam-fit-caution" key={w}>{w}</p>)}
    {!!check.unverified_conditions?.length && <p className="beam-fit-caution">宽度残差按所填实验配准计算。两次测量的{check.unverified_conditions.join('、')}尚未核实。</p>}
    <ResidualPlot points={points}/>
    <div className="fit-data-table"><table><caption>按仪器宽度口径比较</caption><thead><tr>
      <th>原件位置</th><th>拟合 z / mm</th><th>仪器读数</th><th>预测</th><th>实测 − 预测</th><th>用途</th></tr></thead><tbody>
      {points.slice(0,all?points.length:25).map((p,i)=>{
        const unit=(p.width_unit||source.width_unit)==='um'?'μm':'mm';
        const definition=p.definition||source.width_definition;
        const instrument=widthLabel[definition];
        return <tr key={i}><td>{p.row?'第 '+p.row+' 行':p.locator} · {instrument}</td><td>{fmt(p.model_z_mm)}{p.extrapolated?' · 外推':''}</td>
          <td>{fmt(p.raw_width)} {unit}</td><td>{fmt(p.predicted_width)} {unit}</td><td>{fmt(p.width_residual)} {unit}</td><td>{useLabels[p.use]}</td></tr>;
      })}</tbody></table></div>
    {points.length>25 && <button type="button" onClick={()=>setAll(!all)}>{all?'收起其余读数':'查看全部 '+points.length+' 条'}</button>}
    <details><summary>对齐依据与原件</summary>
      <p>拟合坐标 = 新读数坐标 + ({fmt(check.z_offset_mm)} mm)。{check.registration_note}</p>
      <p>拟合基准 · {check.reference_metadata.reference} · {check.reference_metadata.conditions}</p>
      <p>新读数基准 · {metadata.reference} · {metadata.wavelength_nm} nm · n = {metadata.medium_n}</p>
      {[['拟合用原件',check.reference_metadata],['对照用原件',metadata]].map(([label,m])=><div key={label}>
        {m.files.map(f=><p key={f.attachment_id}>{label} · {f.path}<br/>SHA-256 · {f.sha256}</p>)}</div>)}
    </details>
  </article>;
}
