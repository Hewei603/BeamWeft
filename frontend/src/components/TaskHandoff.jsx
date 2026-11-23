import React, {useState} from 'react';
import {AgentSettingsLauncher} from './AgentSettings';

const progressPriority = {calculation:0, snapshot:1, source:2, operation:3, discovery:4};
function relevantProgress(rows) {
  return [...rows].sort((a,b) => ((progressPriority[a.kind] ?? 5) - (progressPriority[b.kind] ?? 5)) || b.tool_call_id - a.tool_call_id);
}

function HandoffRecords({entries, calls, onRecord, onFile}) {
  return <ul className="wb-handoff-records">{entries.map(entry => {
    const call = calls.find(item => item.id === entry.tool_call_id);
    const file = call?.input?.attachment_id;
    return <li key={entry.tool_call_id}><span>{entry.summary}</span>
      <span className="wb-handoff-links"><button type="button" onClick={() => onRecord(entry.tool_call_id)}>记录 #{entry.tool_call_id}</button>
        {Number.isInteger(file) && <button type="button" onClick={() => onFile(file)}>查看原件 #{file}</button>}</span>
    </li>;
  })}</ul>;
}

export function HandoffMessage({handoff}) {
  const firstStep = handoff.next_steps?.[0];
  const progress = handoff.progress_summary || relevantProgress(handoff.progress || []).slice(0,3)
    .map(r => r.summary+'（记录 #'+r.tool_call_id+'）').join(' ');
  return <div className="wb-handoff-message">
    <p>{handoff.reason}</p>
    <p><strong>已保留 </strong>{progress || '本轮没有已完成的工具结果。'}</p>
    {handoff.remaining && <p><strong>待完成 </strong>{handoff.remaining}</p>}
    {firstStep && <p><strong>下一步 </strong>{firstStep.action}</p>}
  </div>;
}

export function ResumeTask({bases, onResume, disabled}) {
  const [choice,setChoice] = useState('');
  const selected = bases.find(base => base.key === choice) || bases[0];
  return <div className="wb-resume">
    {bases.length > 1 ? <label>继续时使用的工程<select value={selected.key} onChange={e => setChoice(e.target.value)}>
      {bases.map(base=><option key={base.key} value={base.key}>{base.label}</option>)}</select></label>
      : selected && <small>{selected.label}</small>}
    <button type="button" className="btn btn-secondary" disabled={disabled} onClick={()=>onResume(selected)}>继续任务</button>
  </div>;
}

export default function TaskHandoff({handoff, calls, requestedFiles, onRecord, onFile, onResume, bases, busy}) {
  const {reason, basis, progress = [], diagnostics = [], remaining, next_steps:steps = []} = handoff;
  const actions = new Set(steps.map(step => step.kind));
  const needsRepair = actions.has('review_settings') || actions.has('external_action');
  const canResume = handoff.category !== 'owner_unverified' && !actions.has('check_owner') && (needsRepair ||
    actions.has('continue_after_fix') || actions.has('supply_information') || actions.has('inspect_record'));
  const linkedFiles = new Set([...progress,...diagnostics].map(entry => calls.find(call=>call.id===entry.tool_call_id)?.input?.attachment_id));
  const otherFiles = requestedFiles.filter(id => !linkedFiles.has(id));
  const records = relevantProgress(progress);
  return <section className="wb-handoff" aria-label="任务停下时的交接记录" data-category={handoff.category}>
    <h4>停在哪里</h4><p>{reason}</p>
    {basis && <><h4>判断依据</h4><p>{basis}</p></>}
    {diagnostics.length > 0 && <HandoffRecords entries={diagnostics} calls={calls} onFile={onFile} onRecord={onRecord}/>}
    <h4>已经完成</h4>{records.length ? <HandoffRecords entries={records.slice(0,3)} calls={calls} onFile={onFile} onRecord={onRecord}/>
      : <p>本轮没有已完成的工具结果。</p>}
    {records.length > 3 && <details><summary>其余 {records.length-3} 条过程记录</summary>
      <HandoffRecords entries={records.slice(3)} calls={calls} onFile={onFile} onRecord={onRecord}/></details>}
    {otherFiles.length > 0 && <div><h4>随本轮提交的原件</h4><div className="wb-handoff-links">
      {otherFiles.map(id=><button type="button" key={id} onClick={()=>onFile(id)}>查看原件 #{id}</button>)}</div></div>}
    {remaining && <><h4>还缺什么</h4><p>{remaining}</p></>}
    {steps.length > 0 && <><h4>怎样继续</h4><ol>{steps.map((step, index) => <li key={index}>
      {step.actor && <strong>{step.actor} </strong>}{step.action}</li>)}</ol></>}
    <div className="wb-handoff-actions">
      {actions.has('review_settings') && <AgentSettingsLauncher className="btn btn-secondary"/>}
      {canResume && <ResumeTask bases={bases} onResume={onResume} disabled={busy}/>}
    </div>
    {canResume && !actions.has('continue_after_fix') && <p className="wb-handoff-hint">{needsRepair ? '处理好上述问题后，点击继续任务可带回原目标和记录，核对草稿后再发送。'
      : '点击继续任务可带回原目标和记录，核对草稿后再发送。'}</p>}
  </section>;
}
