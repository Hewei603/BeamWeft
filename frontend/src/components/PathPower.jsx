import React, { useState } from 'react';
import './PathPower.css';

const fmt=v=>Number.isFinite(v)?String(Number(v.toPrecision(5))):'—';
function interval(pair,factor=1) {
  if(!pair)return '—';
  return pair[0]===pair[1] ? fmt(pair[0]*factor) : fmt(pair[0]*factor)+' 至 '+fmt(pair[1]*factor);
}
const metricText=m=>!m?'未分解':m.watts?interval(m.watts)+' W':interval(m.fraction,100)+'%';
const stageKey=s=>s.scope+'|'+s.element_uid;
const sourceTypes={assumed:'建模假设',datasheet:'器件资料',measured:'实验读数',calculated:'独立计算'};
const parameterNames={retained:'整件保留比例',entry:'入面保留比例',exit:'出面保留比例',alpha_per_mm:'体吸收系数 / mm⁻¹',internal_t:'参考厚度内透过率'};

export function ElementPowerReading({budgets,uid,pathUid,revision,onUseQuantity}) {
  const [chosen,setChosen]=useState('');
  const beams=(budgets||[]).filter(b=>!pathUid || b.path_uid===pathUid)
    .map(b=>({budget:b,row:b.rows?.find(r=>r.scope===(pathUid?'arm':'host') && r.uid===uid)})).filter(entry=>entry.row);
  if(!beams.length)return null;
  const {budget,row}=beams.find(b=>b.budget.path_uid===chosen)||beams[0];
  function reading(metric,quantity,label) {
    if(!metric?.watts || !revision || !onUseQuantity)return metricText(metric);
    return <button type="button" className="bench-quantity" aria-label={'用'+label+'继续分析'}
      onClick={()=>onUseQuantity({revision_id:revision,match:{element_uid:uid,path_uid:budget.path_uid,scope:row.scope},
        quantity,label,location:budget.name+' · '+row.name})}>
      {metricText(metric)}</button>;
  }
  return <section className="bench-power-reading" aria-label="所选元件的单程功率">
    {beams.length>1?<label>查看光束<select value={budget.path_uid} onChange={e=>setChosen(e.target.value)}>
      {beams.map(({budget:b})=><option key={b.path_uid} value={b.path_uid}>{b.name} · {fmt(b.wavelength_nm)} nm</option>)}</select></label>:
      <small>{budget.name} · {fmt(budget.wavelength_nm)} nm</small>}
    <div>进入 {metricText(row.incoming)}<br/>离开 <strong>{reading(row.outgoing,'optics.power','出射功率')}</strong>
      {row.bulk_absorption && <><br/>体吸收 <strong>{reading(row.bulk_absorption,'optics.absorbed-power','体吸收功率')}</strong></>}</div>
    {!budget.input_w && <small>百分比相对于源面输入</small>}
  </section>;
}

function PowerSources({result}) {
  const parameters=[];
  if(result.input_spec)parameters.push(['源面输入 / W',result.input_spec,false]);
  for(const row of result.rows){
    for(const [field,label] of Object.entries(parameterNames)){
      const spec=row.law?.[field];
      if(spec)parameters.push([row.name+' · '+label,spec,field!=='alpha_per_mm',field==='internal_t'?row.law.reference_mm:null]);
    }
  }
  if(!parameters.length)return null;
  return <details><summary>核对参数来源</summary><ul>{parameters.map(([name,spec,percent,thickness],i)=><li key={i}>{name}：
    {interval([spec.low,spec.high??spec.low],percent?100:1)}{percent?'%':''}{thickness!=null?'（'+thickness+' mm）':''}
    {' · '+sourceTypes[spec.basis]}{spec.source?' · '+spec.source:''}</li>)}</ul>
    {result.rows.filter(row=>row.law?.internal_t).map(row=><p key={row.scope+row.uid}>{row.name}的内透过损失
      {row.law.internal_loss_is_absorption?'按可忽略体散射的假设计为吸收。':'归入未分解损耗。'}</p>)}</details>;
}

function Quantity({label,value,change,factor=1,max}) {
  const read=s=>s===''?null:Number(s)/factor;
  return <div className="power-quantity"><label>{label}<input aria-label={label} type="number" step="any" min="0" max={max} placeholder="未知"
    value={value?.low==null?'':Number((value.low*factor).toPrecision(14))}
    onChange={e=>{const low=read(e.target.value);change(low==null?null:{...value,low});}}/></label>
    <details><summary>范围与来源</summary><label>可选上限<input aria-label={label+'上限'} disabled={value?.low==null} type="number" step="any" min="0" max={max}
      value={value?.high==null?'':Number((value.high*factor).toPrecision(14))} onChange={e=>change({...value,high:read(e.target.value)})}/></label>
    <label>来源类别<select aria-label="来源类别" disabled={!value} value={value?.basis||'assumed'} onChange={e=>change({...value,basis:e.target.value})}>
      <option value="assumed">建模假设</option><option value="datasheet">器件资料</option><option value="measured">实验读数</option><option value="calculated">独立计算</option></select></label>
    <label>来源位置或说明<input disabled={!value} value={value?.source||''} onChange={e=>change({...value,source:e.target.value})}/></label></details>
  </div>;
}

export function PowerResult({result,plane}) {
  if(!result)return null;
  if(!result.summary)return <p role="status">{result.message}</p>;
  const end=result.summary;
  const parts=[['沿光路剩余',end.outgoing],['已归因的体吸收',end.modeled_bulk_absorption],['其余移出光路',end.unassigned]];
  const hasAbsorptionModel=result.rows.some(row=>row.bulk_absorption!=null);
  const exact=parts.every(([,v])=>v.fraction[0]===v.fraction[1]);
  const section=result.planes.find(p=>p.plane_uid===plane?.uid);
  return <section className="power-result" aria-label="功率计算结果">
    <h4>{result.route_complete?'路径末端':'已连通部分的末端'} · {result.name} · {fmt(result.wavelength_nm)} nm</h4>
    {!result.input_w && <p>源面瓦数未知。下列百分比以源面输入功率为基准。</p>}
    {exact && <div className="power-bar" aria-hidden="true">{parts.map(([name,m],i)=><span key={name} className={'power-color-'+i} style={{width:(100*m.fraction[0])+'%'}}/>)}</div>}
    <div className="power-totals">{parts.map(([name,m],i)=><div key={name}><span className={'power-key power-color-'+i}/>{name}
      <strong>{i===1 && !hasAbsorptionModel && end.unassigned.fraction[1]>0?'未单独建模':metricText(m)}</strong></div>)}</div>
    {section && <p className="power-plane">在{plane.name}（z = {fmt(section.z_mm)} mm），介质内前向功率为 <b>{metricText(section.forward)}</b>。{section.absorbed_in_segment && <>该段入口至此的体吸收为 {metricText(section.absorbed_in_segment)}。</>}{section.note}</p>}
    <details><summary>逐件核对功率去向</summary><table><thead><tr><th>元件</th><th>进入</th><th>离开</th><th>体吸收</th><th>其余损耗</th></tr></thead><tbody>
      {result.rows.map((row,i)=><tr key={row.scope+(row.uid||i)}><th>{row.name}</th><td>{metricText(row.incoming)}</td><td>{metricText(row.outgoing)}</td>
        <td>{metricText(row.bulk_absorption)}</td><td>{metricText(row.unassigned)}</td></tr>)}</tbody></table>
    <p>体吸收只累计已指定吸收系数或已将体损耗归为吸收的段。反射、散射以及无法分解的损耗计入“其余移出光路”。区间端点可能对应不同条件。</p>
    </details>
    <PowerSources result={result}/>
    {result.missing.length>0 && <details><summary>待补信息 · {result.missing.length} 项</summary><ul>{result.missing.map((m,i)=><li key={i}>{m}</li>)}</ul></details>}
  </section>;
}

function LawFields({law,set}) {
  const [unit,setUnit]=useState('mm'), [method,setMethod]=useState(law.internal_t || law.reference_mm!=null ? 'internal':'alpha');
  const quantity=(key,label,factor=1,max)=><Quantity label={label} value={law[key]} change={value=>set({...law,[key]:value})} factor={factor} max={max}/>;
  if(law.mode==='net')return <>{quantity('retained','整件沿本支路保留 / %',100,100)}
    <p>可以填写透射、折转反射或实测耦合后保留的功率比例。整件值覆盖其边界之间的全部损耗。</p></>;
  const activeMethod=law.internal_t || law.reference_mm!=null?'internal':law.alpha_per_mm?'alpha':method;
  return <><div className="power-fields">{quantity('entry','入面保留 / %',100,100)}{quantity('exit','出面保留 / %',100,100)}
    <label>体吸收输入<select aria-label="体吸收输入" value={activeMethod} onChange={e=>{
      const value=e.target.value;setMethod(value);set({...law,internal_t:null,reference_mm:null,alpha_per_mm:null,internal_loss_is_absorption:false});
    }}><option value="alpha">功率吸收系数</option><option value="internal">参考厚度的内透过率</option></select></label>
    {activeMethod==='internal'?<>{quantity('internal_t','参考厚度内透过率 / %',100,100)}
      <label>参考厚度 / mm<input type="number" min="0" step="any" value={law.reference_mm??''} onChange={e=>set({...law,reference_mm:e.target.value===''?null:Number(e.target.value)})}/></label></>
      :<><label>吸收系数单位<select aria-label="吸收系数单位" value={unit} onChange={e=>setUnit(e.target.value)}><option value="mm">mm⁻¹</option><option value="cm">cm⁻¹</option></select></label>
        {quantity('alpha_per_mm','体吸收系数 / '+(unit==='cm'?'cm⁻¹':'mm⁻¹'),unit==='cm'?10:1)}</>}
    </div>{activeMethod==='internal' && <label className="section-inline"><input type="checkbox" checked={!!law.internal_loss_is_absorption}
      onChange={e=>set({...law,internal_loss_is_absorption:e.target.checked})}/>假设体散射可忽略，将内透过损失归为吸收</label>}
    <p>材料按均匀、轴向传播建模。相邻段共用的接合面只计在一侧。理想无损的面可填 100%。</p></>;
}

export default function PathPower({title,budget,wavelength,stages,result,plane,change,disabled}) {
  const [selection,setSelection]=useState('');
  const stage=stages.find(s=>stageKey(s)===selection)||stages[0];
  const rules=budget?.stages||[];
  const rule=stage && rules.find(s=>stageKey(s)===stageKey(stage));
  const update=(key,value)=>change({...budget,[key]:value});
  const editLaw=law=>update('stages',[...rules.filter(s=>stageKey(s)!==stageKey(stage)),{scope:stage.scope,element_uid:stage.element_uid,law}]);
  const create=()=>change({wavelength_nm:wavelength??null,input_w:null,stages:[]});
  return <details className="path-power"><summary>{title}</summary>
    <fieldset disabled={disabled}>{!budget?<><p>沿光路核算单色单程功率。折射率决定光束传播，功率收支另取对应波长下的器件数据。</p>
      <button type="button" className="btn btn-secondary" onClick={create}>建立功率预算</button></>:<>
      <div className="power-fields"><Quantity label="源面输入光功率 / W" value={budget.input_w} change={v=>update('input_w',v)}/>
        <label>能量参数波长 / nm<input type="number" min="0" step="any" value={budget.wavelength_nm??''}
          onChange={e=>update('wavelength_nm',e.target.value===''?null:Number(e.target.value))}/></label></div>
      {stage && <details className="power-editor"><summary>设置元件的功率参数</summary><div className="power-fields">
        <label>功率元件<select aria-label="功率元件" value={stageKey(stage)} onChange={e=>setSelection(e.target.value)}>
          {stages.map((s,i)=><option key={stageKey(s)} value={stageKey(s)}>{i+1} · {s.name}</option>)}</select></label>
        <label>损耗描述<select aria-label="损耗描述" value={rule?.law.mode||''} onChange={e=>{
          if(!e.target.value)update('stages',rules.filter(s=>stageKey(s)!==stageKey(stage)));else editLaw({mode:e.target.value});
        }}><option value="">暂未指定</option><option value="net">整件功率保留率</option><option value="bulk">入面、体吸收与出面</option></select></label></div>
        {rule && <LawFields key={stageKey(stage)+rule.law.mode} law={rule.law} set={editLaw}/>}
        <button type="button" className="btn btn-secondary" onClick={()=>editLaw({mode:'net',retained:{low:1,basis:'assumed',source:'手工选定该元件在此支路理想无损'}})}>将该元件设为理想无损</button>
      </details>}
      {!!result?.unused_stages?.length && <button type="button" onClick={()=>update('stages',rules.filter(s=>!result.unused_stages.some(o=>stageKey(o)===stageKey(s))))}>移除未接入的旧参数</button>}
      <details><summary>撤下功率预算</summary><p>这会从当前光路草稿移除源功率和这份预算的 {rules.length} 项元件损耗设置。</p>
        <button type="button" onClick={()=>change(null)}>移除这份功率预算</button></details>
    </>}</fieldset>
    {budget && <PowerResult result={result} plane={plane}/>}
  </details>;
}
