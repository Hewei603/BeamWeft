import React, {useEffect, useState} from 'react';
import { providerLabel } from '../brand';
import {workbenchApi} from '../api/workbench';

export function useStudyDefinition(caseId, name, attempt) {
  const [reply, setReply] = useState(null);
  useEffect(() => {
    if (!name) return;
    const controller = new AbortController();
    workbenchApi.studyDefinition(caseId,name,controller.signal)
      .then(data => { if (!controller.signal.aborted) setReply({caseId,name,data}); })
      .catch(error => { if (!controller.signal.aborted) setReply({caseId,name,error}); });
    return () => controller.abort();
  }, [caseId,name,attempt]);
  return reply?.caseId === caseId && reply.name === name ? reply : null;
}

export default function CapabilityBrowser({caseId, onSelect, selected='', selectedTitle, disabled=false,
  originals=false, attachmentId=null, source=null, ready=true}) {
  const [query,setQuery] = useState(''), [offset,setOffset] = useState(0);
  const [reply,setReply] = useState(null), [attempt,retry] = useState(0);
  const limit = 20;
  const body = JSON.stringify({query,offset,limit,...(onSelect && {kind:'study'}),
    original_files:originals,...(attachmentId && {attachment_id:attachmentId}),...(source && {source})});
  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      workbenchApi.searchCapabilities(caseId,JSON.parse(body),controller.signal)
        .then(data => { if (!controller.signal.aborted) setReply({caseId,body,data}); })
        .catch(error => { if (!controller.signal.aborted) setReply({caseId,body,error:error.message}); });
    }, query ? 120 : 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [caseId,body,ready,attempt,query]);
  const current = ready && reply?.body === body && reply.caseId === caseId ? reply : null;
  const page = current?.data;
  const items = page?.items ?? [];
  const option=s=><option key={s.name} value={s.name}>{s.ambiguous_title ? s.title+' · '+providerLabel(s.provider)+' · '+s.name : s.title}</option>;
  const fileOptions=(match,label)=>{
    const group=items.filter(s=>s.original_match===match);
    return group.length>0 && <optgroup label={label}>{group.map(option)}</optgroup>;
  };
  return <div className="capability-browser">
    <label>{onSelect?'查找计算方法':'查找工具与方法'}<input type="search" value={query} maxLength={500}
      placeholder="输入物理量、方法名称或提供方" onChange={e=>{setQuery(e.target.value);setOffset(0);}}/></label>
    {onSelect ? <label>选择计算<select aria-label="选择计算" value={selected} disabled={disabled}
      onChange={e=>{const item=items.find(item=>item.name===e.target.value);
        onSelect(e.target.value,item?.matching_inputs || {},item?.original_inputs);}}><option value="">请选择计算方法</option>
      {selected && !items.some(s=>s.name===selected) && <option value={selected}>当前编辑 · {selectedTitle || selected}</option>}
      {attachmentId ? <>{fileOptions('recognized','已识别文件格式')}{fileOptions('unverified','需要试读')}</> : items.map(option)}</select></label>
      : items.map(s=><details key={s.name}><summary>{s.title} · {s.name}</summary><p>{s.scope}</p><small>{providerLabel(s.provider)}{s.version && ' · v'+s.version}</small></details>)}
    {ready && !current && <small role="status">正在查找…</small>}
    {page && <div className="capability-pages"><small role="status">{page.total
      ? page.total<=limit ? `${page.total} 项匹配` : `第 ${offset+1}–${offset+items.length} 项，共 ${page.total} 项`
      : query?'没有匹配的方法。可以换用物理量、提供方或更短的关键词。':'当前条件下没有兼容方法。'}</small>
      {offset>0 && <button type="button" onClick={()=>setOffset(Math.max(0,offset-limit))}>上一页</button>}
      {page.next_offset!=null && <button type="button" onClick={()=>setOffset(page.next_offset)}>下一页</button>}</div>}
    {current?.error && <p role="alert">{current.error} <button type="button" onClick={()=>retry(n=>n+1)}>重新查找</button></p>}
    {page?.issues.map((issue,i)=><p key={i}>扩展 {issue.name} 未载入。{issue.reason}</p>)}
  </div>;
}
