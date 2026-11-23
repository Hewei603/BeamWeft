import React, {useState} from 'react';
import {basisLabels, percent, readDistribution} from './studyScenarioWeights';
import OriginalFileInput from './OriginalFileInput';

export function Share({mass,field}) {
  const exact=mass.exact?.[field];
  return <span title={exact || String(mass[field])}>{percent(mass[field],exact)}
    {exact && <small>精确占比 {exact}</small>}</span>;
}

export function ShareRange({mass}) {
  if (!mass) return null;
  if (mass.lower===mass.upper && mass.exact?.lower===mass.exact?.upper) return <Share mass={mass} field="lower"/>;
  return <><Share mass={mass} field="lower"/> 至 <Share mass={mass} field="upper"/></>;
}

export default function ScenarioWeights({fields,points,draft,onChange,applied,dirty,onApply,onClear,open,onToggle,files}) {
  const [error,setError]=useState('');
  function update(patch) {
    setError('');
    onChange({...draft,...patch});
  }
  function paste(event,start) {
    const text=event.clipboardData?.getData('text/plain')?.trim();
    if (!text || !/[\t\n\r]/.test(text)) return;
    event.preventDefault();
    const entries=text.split(/\r?\n|\t/).map(v=>v.trim());
    if (start+entries.length>points.length || entries.some(v=>!v || !Number.isFinite(Number(v)) || Number(v)<0)) {
      setError('请粘贴一列非负数，并确认从当前行起有足够的工况。');
      return;
    }
    const weights={...draft.weights};
    entries.forEach((v,i)=>{weights[JSON.stringify(points[start+i])]=v;});
    update({weights});
  }
  function apply(event) {
    event.preventDefault();
    try { onApply(readDistribution(draft,points)); setError(''); }
    catch(cause) { setError(cause.message); }
  }
  const originals=draft.originals || [];
  return <details className="scenario-weights" open={open} onToggle={e=>{
    if(e.target===e.currentTarget)onToggle(e.currentTarget.open);
  }}>
    <summary>按工况出现频率评价</summary>
    <form onSubmit={apply}><label>权重来源<select aria-label="权重来源" required value={draft.basis}
      onChange={e=>update({basis:e.target.value})}><option value="">选择依据</option>
      {Object.entries(basisLabels).map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></label>
      <label>测量来源或假设依据<textarea required maxLength={1500} rows={2} value={draft.source}
        onChange={e=>update({source:e.target.value})}/></label>
      <label>关联实验原件（可选）<OriginalFileInput files={files.filter(f=>!originals.some(ref=>ref.attachment_id===f.id))}
        aria-label="关联工况原件" disabled={!files.length || originals.length>=32}
        onSelect={ref=>{if(ref)update({originals:[...originals,ref]});}}/></label>
      {originals.length>=32 && <small>本次已关联 32 份原件。</small>}
      {originals.length>0 && <ul className="scenario-originals">{originals.map(ref=>{
        const file=files.find(f=>f.id===ref.attachment_id);
        return <li key={ref.attachment_id}>{file?.relative_path || file?.filename || '原件'} · #{ref.attachment_id}
          <button type="button" className="bench-text-btn" onClick={()=>update({originals:originals.filter(f=>f!==ref)})}>移除引用</button></li>;
      })}</ul>}
      <table><caption>联合工况及其相对权重</caption><thead><tr><th scope="col">工况</th><th scope="col">权重</th></tr></thead>
        <tbody>{points.map((coords,i)=>{
          const key=JSON.stringify(coords),label=fields.map((f,j)=>f.label+' = '+coords[j]).join('，') || '唯一工况';
          return <tr key={key}><th scope="row">{label}</th><td>
            <input type="number" aria-label={'工况权重 · '+label} min="0" step="any" required value={draft.weights[key] ?? ''}
              onPaste={e=>paste(e,i)} onChange={e=>update({weights:{...draft.weights,[key]:e.target.value}})}/></td></tr>;
        })}</tbody></table>
      <small>可填写次数或相对权重，计算使用它们占总权重的比例。可从表格粘贴一列；明确未出现的工况填 0。</small>
      {dirty && <p role="status">{applied?'这些修改尚未应用，当前结论仍使用上次应用的权重。':'权重尚未应用，当前仅核对各采样工况是否达标。'}</p>}
      <button type="submit" className="btn btn-secondary">应用工况权重</button>
      {(applied || dirty) && <button type="button" className="bench-text-btn" onClick={()=>{setError('');onClear();}}>清除工况权重</button>}
      {error && <p role="alert">{error}</p>}
    </form>
  </details>;
}
