import React, { useEffect, useRef, useState } from 'react';
import { workbenchApi } from '../api/workbench';
import StudyModelReceipt from './StudyModelReceipt';
import ModelInputComparison from './ModelInputComparison';
import BenchToolPanel from './BenchToolPanel';

function versionLabel(row, headId) {
  const label = row.kind === 'optical_snapshot' ? row.variant_name || row.change_note || '独立试算'
    : row.id === headId ? '当前主工程' : '历史主工程';
  return label + ' · #' + row.id;
}

export default function ProjectVariants({caseId, current, headId, dirty, disabled, forkDisabled, refreshToken, onFork, onOpen, onAsk, onAdopting, draft, readWorkingDraft, pending}) {
  const [open, setOpen] = useState(false), [name, setName] = useState(''), [query, setQuery] = useState('');
  const [page, setPage] = useState(null), [loading, setLoading] = useState(false), [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [reference,setReference]=useState(null);
  const generation = useRef(0);
  const currentId = current?.revision_id;
  useEffect(() => {
    const ticket = ++generation.current;
    if (!open) return;
    const timer = setTimeout(() => {
      setPage(null); setLoading(true); setError('');
      workbenchApi.modelVersions(caseId, undefined, query.trim()).then(result => {
        if (generation.current === ticket) setPage({...result, query:query.trim()});
      }).catch(e => { if (generation.current === ticket) setError(e.message); })
        .finally(() => { if (generation.current === ticket) setLoading(false); });
    }, query ? 160 : 0);
    return () => { clearTimeout(timer); generation.current++; };
  }, [open, caseId, query, currentId, headId, refreshToken, attempt]);
  const more = async () => {
    const ticket = generation.current;
    setLoading(true); setError('');
    try {
      const next = await workbenchApi.modelVersions(caseId, page.next_before_id, query.trim());
      if (generation.current === ticket) setPage({...next, query:page.query, items:[...page.items, ...next.items]});
    } catch (e) { if (generation.current === ticket) setError(e.message); }
    finally { if (generation.current === ticket) setLoading(false); }
  };
  const trial = current?.model_kind === 'draft';
  const alternatives = (page?.query===query.trim()?page.items:[]).filter(row=>readWorkingDraft || row.id!==currentId);
  const preferred=reference ?? (readWorkingDraft?currentId:current?.source_revision_id || current?.parent_revision_id);
  const referenceId=alternatives.find(row=>row.id===preferred)?.id ?? alternatives[0]?.id;
  return <BenchToolPanel title="方案与版本" className="op-variants" caseId={caseId} open={open} onOpenChange={setOpen}>
    {open && <section aria-label="方案与版本">
      <form onSubmit={async e => { e.preventDefault(); if (await onFork(name.trim())) setName(''); }}>
        <label>新方案名称<input maxLength={160} value={name} onChange={e => setName(e.target.value)} placeholder="可选"/></label>
        <button className="btn btn-secondary" disabled={forkDisabled}>从当前光路另存方案</button>
      </form>
      <label>按方案名查找<input type="search" value={query} onChange={e => setQuery(e.target.value)} maxLength={160}/></label>
      <label>打开光路版本<select value="" disabled={!page?.items.length || page.query !== query.trim() || disabled || dirty}
        onChange={e => onOpen(Number(e.target.value))}>
        <option value="" disabled>选择要打开的版本</option>
        {page?.items.map(row => <option key={row.id} value={row.id}>{versionLabel(row, headId)}</option>)}
      </select></label>
      {dirty && <small>先保存当前改动，即可切换版本。</small>}
      {loading && <small role="status">读取版本…</small>}
      {page && !loading && !page.items.length && <small>{query ? '没有匹配这个名称的方案。' : '还没有已存版本。'}</small>}
      {error && <small role="alert">{error} <button onClick={() => setAttempt(n => n + 1)}>重新读取</button></small>}
      {page?.next_before_id && <button disabled={loading} onClick={more}>更早版本</button>}
      {!!alternatives.length && (currentId || readWorkingDraft) && <ModelInputComparison key={caseId+'/'+currentId}
        caseId={caseId} revision={currentId} referenceId={referenceId} draft={draft} readWorkingDraft={readWorkingDraft} pending={pending} onAsk={onAsk}>
        <label>基准版本<select value={referenceId} onChange={e=>setReference(Number(e.target.value))}>
          {alternatives.map(row=><option key={row.id} value={row.id}>{versionLabel(row,headId)}</option>)}</select></label>
      </ModelInputComparison>}
      {currentId && currentId !== headId && <button className="btn btn-secondary" disabled={disabled || dirty}
        onClick={() => onOpen(headId)}>返回{headId ? '当前主工程 #' + headId : '空白主工程'}</button>}
      {trial && <StudyModelReceipt key={currentId} fromCanvas caseId={caseId} input={{kind:'draft', ...current}}
        disabled={disabled || dirty} onAsk={onAsk} onAdopting={onAdopting}/>}
    </section>}
  </BenchToolPanel>;
}
