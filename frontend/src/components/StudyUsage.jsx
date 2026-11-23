import React, {useEffect, useState} from 'react';
import {workbenchApi} from '../api/workbench';

const stateNames = {same:'沿途引用的工程输入相同', changed:'直接引用的数值或模型已变', conditions:'引用条件有变化', unavailable:'引用的工程量待核对', review:'上游有待复核的输入'};
const valueLabel = (value,unit) => (Array.isArray(value) ? (value[0]===value[1]?value[0]:value.join(' – ')) : value) + ' ' + unit;

function InputUse({item}) {
  const role = item.input_label || item.input || item.input_path?.map(key=>typeof key==='number'?'第 '+(key+1)+' 项':key).join(' › ') || '整份原件';
  return <li>{role}{item.column && ' · 原件第 '+item.column+' 列'}{item.label && item.label!==role && ' · '+item.label}
    {item.relative_path && ' · '+item.relative_path}
    {item.sha256 && <small>保存时的 SHA-256 · {item.sha256}</small>}
    {item.previous != null && <> · {valueLabel(item.previous,item.previous_unit)}{item.value_changed || item.previous_unit!==item.current_unit
      ? ' → '+valueLabel(item.current,item.current_unit) : '（数值相同）'}</>}
    {item.basis_changed && !item.condition_changes?.length && ' · 物理量声明有变化'}
    {item.condition_changes?.map(change=><small key={change.key}>{change.label} · {String(change.previous ?? '未记录')} → {String(change.current ?? '未记录')}</small>)}
    {item.reference_changes?.map(change=><small key={change.key}>{change.label} · {change.previous} → {change.current}（来源记录）</small>)}
    {item.state==='unavailable' && (item.missing==='previous'?' · 原版本缺少该输出':' · 新版本缺少该输出')}</li>;
}

export function StudyUsageResults({data,onOpen}) {
  const {source_id:sourceId,compare_revision_id:compareId}=data;
  const warning=data.unreadable_binding_count>0 && <p role="status">{data.unreadable_binding_count} 条记录的来源格式损坏，引用范围和统计可能不完整。</p>;
  if(!data.total)return <>{warning}<p>{warning?'可核对的绑定中没有找到对这一来源的引用。':'已存记录中尚无对这一来源的引用。'}</p></>;
  return <>{warning}<p>关联 {data.total} 条已存记录，其中 {data.direct_count} 条直接引用，{data.terminal_count} 条位于计算链末端。
    {compareId && <> 与工程 #{compareId} 对照，有 {data.review_count} 条需要复核。</>}</p>
    <ul className="usage-list">{data.items.map(run=>{
      const assessment=run.content_type==='study_search';
      const title='#'+run.id+' · '+(assessment?'工况评价 · ':'')+(run.title || '未命名');
      const view=run.workbench_view;
      return <li key={run.id}>
        {assessment && !view ? <span>{title}<small>{run.navigation_issue}</small></span>
          : <button type="button" className="bench-text-btn" onClick={()=>view ? onOpen(view.run_id,null,view.comparison_id) : onOpen(run.id)}>{title}</button>}
        <small>{[run.terminal?'链末端':'中间计算',stateNames[run.state]].filter(Boolean).join(' · ')}</small>
        {!!run.inputs.length && <ul>{run.inputs.map((item,index)=><InputUse key={index} item={item}/>)}</ul>}
        {!!run.via.length && <small>经计算 {run.via.map(id=>'#'+id).join('、')}{run.upstream_needs_review && ' · 上游有待复核的输入'}</small>}
        {!assessment && compareId && run.state!=='same' && <button type="button" className="btn btn-secondary"
          aria-label={'预览计算 #'+run.id+' 的复算'} onClick={()=>onOpen(run.id,{sourceId,replacementId:compareId})}>预览复算</button>}
      </li>;
    })}</ul>
  </>;
}

function UsagePage({caseId,sourceId,fileId,compareId,onOpen}) {
  const [offset,setOffset]=useState(0),[attempt,retry]=useState(0),[reply,setReply]=useState(null);
  useEffect(()=>{
    const controller=new AbortController();
    workbenchApi.studyUsage(caseId,{...(fileId?{attachment_id:fileId}:{source_id:sourceId}),
      ...(compareId?{compare_revision_id:compareId}:{}),offset,limit:10},controller.signal)
      .then(data=>{if(!controller.signal.aborted)setReply({offset,attempt,data});})
      .catch(cause=>{if(!controller.signal.aborted)setReply({offset,attempt,error:cause.message});});
    return ()=>controller.abort();
  },[caseId,sourceId,fileId,compareId,offset,attempt]);
  if(reply?.offset!==offset || reply.attempt!==attempt)return <p role="status">正在查找已存计算…</p>;
  if(reply.error)return <p role="alert">{reply.error} <button type="button" onClick={()=>retry(n=>n+1)}>重新查找</button></p>;
  const data=reply.data;
  return <><StudyUsageResults data={data} onOpen={onOpen}/>
    {(offset>0 || data.next_offset!=null) && <nav aria-label="关联计算分页">
      <button type="button" disabled={!offset} onClick={()=>setOffset(n=>Math.max(0,n-10))}>上一页</button>
      <button type="button" disabled={data.next_offset==null} onClick={()=>setOffset(data.next_offset)}>下一页</button>
    </nav>}
  </>;
}

export default function StudyUsage({caseId,recordId,fileId,versions,parentId,onOpen}) {
  const [opened,setOpened]=useState(false);
  const [source,setSource]=useState(parentId && versions?.some(v=>v.id===parentId) ? parentId : recordId);
  const compareId=versions && source!==recordId ? recordId : null;
  return <details className="study-usage" onToggle={e=>{if(e.target===e.currentTarget)setOpened(e.currentTarget.open);}}>
    <summary>使用这一来源的计算</summary>
    {opened && <>{versions && <label>查找使用哪个版本的计算
      <select aria-label="查找使用哪个版本的计算" value={source} onChange={e=>setSource(Number(e.target.value))}>
        {!versions.some(v=>v.id===recordId) && <option value={recordId}>#{recordId}（正在查看）</option>}
        {versions.map(v=><option key={v.id} value={v.id}>工程 #{v.id} · {v.title}{v.id===recordId?'（正在查看）':''}</option>)}
      </select></label>}
      {compareId && <small>工程 #{source} → 已存工程 #{compareId}。这里只核对输入，复算后可比较结论。</small>}
      <UsagePage key={String(source)+'/'+compareId} caseId={caseId} sourceId={source} fileId={fileId} compareId={compareId} onOpen={onOpen}/>
    </>}
  </details>;
}
