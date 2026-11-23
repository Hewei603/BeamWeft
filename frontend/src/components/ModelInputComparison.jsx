import React, {useEffect, useRef, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import {modelFields} from './modelFields';
import {JsonDetails} from './DeferredDetails';
import {SolverReceipt} from './SolverReceipt';

const actions={added:'新增', removed:'移除', reordered:'调整顺序'};
const nameOf = model => model.kind==='canvas' ? '画布草稿' :
  (model.variant_name || model.title)+' #'+model.revision_id;

function Cell({row, side}) {
  if (!Object.hasOwn(row,side)) return <small>未记录此项</small>;
  const value=row[side];
  if (value===null) return <><small>空值（null）</small>{row.definition && <small>{row.definition}</small>}</>;
  if (row.kind==='reordered') {
    const names=value.map(obj=>obj.name);
    return value.map((obj,i)=>names.indexOf(obj.name)!==i || names.lastIndexOf(obj.name)!==i
      ? obj.name+' ('+obj.uid+')' : obj.name).join(' → ');
  }
  if (Array.isArray(value) && value.every(x=>typeof x==='string')) {
    return value.length ? <ul>{value.map((entry,i)=><li key={i}>{entry}</li>)}</ul> : <small>空列表</small>;
  }
  if (typeof value==='object') return <JsonDetails title={value.name || '查看完整参数'} value={value}/>;
  return value==='' ? <small>空文本</small> : String(value);
}

export function InputDifferences({result}) {
  return <div className="model-input-differences">
    <p role="status">{nameOf(result.reference)} → {nameOf(result.target)} · {result.total ? '共 '+result.total+' 项输入差异' : '工程输入相同'}</p>
    {result.target.kind!=='canvas' && <SolverReceipt saved={result.reference.solver_receipt} current={result.target.solver_receipt ?? null}
      left={'基准 #'+result.reference.revision_id} right={'对照 #'+result.target.revision_id}/>}
    {result.items.length>0 && result.items.length<result.total && <small>显示第 {(result.offset || 0)+1} 至 {(result.offset || 0)+result.items.length} 项</small>}
    {!!result.items.length && <table><thead><tr><th>项目</th><th>基准输入</th><th>对照输入</th></tr></thead>
      <tbody>{result.items.map(row=><tr key={JSON.stringify(row.path)}>
        <th scope="row">{row.path.map((key,i)=><React.Fragment key={i}>{i>0 && ' / '}
          {typeof key==='string' ? (row.path.slice(0,i).some(k=>k==='component_context' || k==='parameters')?key:modelFields[key] || key) : <span title={key.uid}>{key.name}
            {row.kind==='added' && key.after_position && '（第 '+key.after_position+' 项）'}
            {row.kind==='removed' && key.before_position && '（原第 '+key.before_position+' 项）'}
            {row.kind==='changed' && key.after_position && ' ['+key.after_position+']'}</span>}</React.Fragment>)}
          {actions[row.kind] && <small>{actions[row.kind]}</small>}</th>
        <td><Cell row={row} side="before"/></td><td><Cell row={row} side="after"/></td>
      </tr>)}</tbody></table>}
  </div>;
}

export default function ModelInputComparison({caseId, referenceId, revision, readWorkingDraft, draft, pending, onAsk, children}) {
  const [result,setResult]=useState(null), [error,setError]=useState(''), [busy,setBusy]=useState(false);
  const request=useRef(null);
  useEffect(()=>()=>request.current?.abort(),[]);
  const targetRevision=readWorkingDraft?null:revision;
  const current=result && result.reference.revision_id===referenceId && result.target.revision_id===targetRevision
    && (!readWorkingDraft || result.query.working_draft.project===draft && !pending);
  async function read(more=false) {
    request.current?.abort();
    const controller=new AbortController();
    request.current=controller;setError('');setBusy(true);
    try {
      const query=more ? {...result.query,offset:result.next_offset}
        : {reference_revision_id:referenceId,...(readWorkingDraft?{working_draft:readWorkingDraft()}:{revision_id:revision})};
      const data=await workbenchApi.compareProjectInputs(caseId,query,controller.signal);
      if (!controller.signal.aborted) setResult({...data,query,offset:0,items:more?[...result.items,...data.items]:data.items});
    } catch (e) { if (!controller.signal.aborted) setError(e.message); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  return <details className="model-input-comparison"><summary>对照工程输入</summary>
    <div className="model-input-choice">{children}
    <button type="button" className="btn btn-secondary" disabled={busy || !referenceId || (!readWorkingDraft && !revision)}
      onClick={()=>read()}>{busy?'正在读取…':'查看输入差异'}</button></div>
    {error && <p role="alert">{error}</p>}
    {!current && !busy && readWorkingDraft && result?.target.kind==='canvas' && result.reference.revision_id===referenceId
      && <p role="status">画布输入已变更，重新对照可查看当前改动。</p>}
    {current && <><InputDifferences result={result}/>
      {result.next_offset!=null && <button type="button" disabled={busy} onClick={()=>read(true)}>更多差异</button>}
      {onAsk && <button type="button" className="bench-text-btn" onClick={()=>onAsk(
        '请对照基准版本 #'+referenceId+' 与'+nameOf(result.target)+' 的工程输入。\n',
        {project_revision_id:targetRevision,followCanvas:!targetRevision,artifact_ids:[referenceId]})}>引用这两份模型问 Agent ↗</button>}
    </>}
  </details>;
}
