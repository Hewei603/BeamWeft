import PredictionComparison from './PredictionComparison';
import BeamComparison from './BeamComparison';
import React, { useEffect, useState } from 'react';
import { workbenchApi } from '../api/workbench';
import DraftReview from './DraftReview';
import {JsonDetails} from './DeferredDetails';
import { equalValues, planDraftMerge } from './projectDraft';

const label = model => model?.kind === 'empty' ? '提问时尚未建立光路' :
  (model?.variant_name || (model?.kind === 'draft' ? '试算快照' : '已存工程')) + ' #' + model.revision_id;

export default function StudyModelReceipt({ caseId, input, result, disabled, onAsk, onAdopting, fromCanvas = false }) {
  const [open, setOpen] = useState(false), [chosen, setChosen] = useState(null);
  const [record, setRecord] = useState(null), [failure, setFailure] = useState(null);
  const [busy, setBusy] = useState(false), [adoptions, setAdoptions] = useState({}), [attempt, setAttempt] = useState(0);
  const [review, setReview] = useState(null);
  const versions = [input];
  if (result?.revision_id && result.revision_id !== input.revision_id) versions.push(result);
  const version = versions.find(v => v.revision_id === chosen) || versions.at(-1);
  const id = version.revision_id;
  useEffect(() => {
    if (!open || !id) return;
    let active = true;
    workbenchApi.getModelInput(caseId, id).then(data => { if (active) { setRecord(data); setFailure(null); } })
      .catch(e => { if (active) setFailure({id, message:e.message}); });
    return () => { active = false; };
  }, [caseId, id, open, attempt]);
  const saved = record?.revision_id === id ? record : null;
  const project = saved?.project;
  const loadReview = async () => {
    const [latest, base] = await Promise.all([
      workbenchApi.getModelInput(caseId),
      version.base_revision_id ? workbenchApi.getModelInput(caseId, version.base_revision_id) : null,
    ]);
    if (!latest.project) throw new Error('未取得正式工程，请重新读取。');
    const plan = base ? planDraftMerge(base.project, project, latest.project) : null;
    setReview({id, latest, plan});
  };
  const adopt = async (merged = null) => {
    const commitFinished = onAdopting();
    setBusy(true); setFailure(null);
    try {
      let notice, revision;
      if (merged && equalValues(merged, review.latest.project)) {
        const current = await workbenchApi.getModelInput(caseId);
        if (current.revision_id !== review.latest.revision_id) { await loadReview(); return; }
        revision = current.revision_id;
        notice = '已核对并保留工程 #' + current.revision_id;
      } else {
        const selection = merged ? {reviewed_against:review.latest.revision_id, reviewed_project:merged} : {};
        const accepted = await workbenchApi.adoptSnapshot(caseId, id, version.base_revision_id, selection);
        revision = accepted.revision_id;
        notice = (merged ? '核对后保存为工程 #' : '已采用为工程 #') + accepted.revision_id;
      }
      setAdoptions(all => ({...all, [id]:notice}));
      setReview(null);
      try { await commitFinished(revision); }
      catch (e) { setFailure({id, message:notice + '。刷新工作台失败，可重新载入。' + e.message}); }
    } catch (e) {
      if (e.status === 409) {
        try { await loadReview(); }
        catch (readError) { setFailure({id, message:'试算记录已保留。读取待核对版本失败。' + readError.message}); }
      } else setFailure({id, message:e.message});
    } finally { setBusy(false); }
  };
  return <details className="study-model-receipt" onToggle={e => { if (e.target === e.currentTarget) setOpen(e.currentTarget.open); }}>
    <summary>{fromCanvas ? '查看与采用方案' : '本轮光路'} · {label(input)}{versions.length === 2 && ' → ' + label(result)}</summary>
    {open && <>
      {versions.length === 2 && <label>查看光路<select disabled={busy} value={id ?? ''} onChange={e => {setReview(null); setChosen(e.target.value ? Number(e.target.value) : null);}}>
        {versions.map((v, i) => <option key={i} value={v.revision_id ?? ''}>{i ? '修改结果' : '提问输入'} · {label(v)}</option>)}</select></label>}
      {!id && <p>这条消息从案例资料开始，没有附带光路模型。</p>}
      {id && !project && failure?.id !== id && <p role="status">读取本轮使用的模型…</p>}
      {project && <><p><strong>{project.title}</strong> · {project.system.wavelength_nm} nm · {project.system.elements.length} 个主光路元素</p>
        {version.kind === 'draft' && <p>{version.source_revision_id ? '源自版本 #' + version.source_revision_id : '来源为尚未保存的光路'}。
          {version.base_revision_id ? '从主工程 #' + version.base_revision_id + ' 分出。' : '从空白主工程分出。'}</p>}
        <ol>{project.system.elements.map(el => <li key={el.uid}>{el.name}
          {el.length_mm > 0 ? ' · ' + el.length_mm + ' mm' : el.focal_mm != null ? ' · f = ' + el.focal_mm + ' mm' : ''}</li>)}</ol>
        {!!project.incident_paths?.length && <p>入射支路 · {project.incident_paths.map(p => p.name).join('、')}</p>}
        <JsonDetails key={'inputs-'+id} title="查看完整计算输入" value={project}/>
        <PredictionComparison key={'predictions-'+id} caseId={caseId} revision={id} project={project} onAsk={onAsk} disabled={disabled || busy}/>
        <BeamComparison key={caseId+'-'+id} caseId={caseId} revision={id} paths={project.incident_paths} onAsk={onAsk} disabled={disabled || busy} />
        {version.kind === 'draft' && review?.id !== id && <button className="btn btn-secondary" disabled={disabled || busy || !!adoptions[id]} onClick={() => adopt()}>
          {adoptions[id] || '采用此快照为工程新版本'}</button>}
        {review?.id === id && (review.plan ?
          <DraftReview key={review.latest.revision_id} plan={review.plan} latestId={review.latest.revision_id} disabled={disabled || busy}
            localLabel={'试算快照 #' + id} explanation="试算期间，正式工程有过修改。核对双方的改动后，系统会用选定参数重新计算。"
            actionLabel="确认核对结果" onApply={adopt} /> :
          <section aria-label="比较独立建立的两份光路"><h3>试算期间建立了工程 #{review.latest.revision_id}</h3>
            <p>两份光路分别从空工程建立。请对照完整模型，选择要保留的一份。</p>
            <JsonDetails key={review.latest.revision_id} title={'查看工程 #'+review.latest.revision_id+' · '+review.latest.project.title}
              value={review.latest.project}/>
            <button disabled={busy || disabled} onClick={() => adopt(review.latest.project)}>保留该工程</button>
            <button disabled={busy || disabled} onClick={() => adopt(project)}>用整份试算模型建立后续版本</button>
          </section>)}
      </>}
      {failure?.id === id && <p role="alert" className="wb-error">{failure.message}
        {!project && <button onClick={() => { setFailure(null); setAttempt(n => n+1); }}>重试读取</button>}</p>}
    </>}
  </details>;
}
