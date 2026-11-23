import React, {useEffect, useId, useRef, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import {axisField, Limits} from './StudyTolerance';
import {adjustmentRoles as roleLabels, selectedDesignIndex} from './studyAdjustmentRoles';
import ScenarioWeights, {Share, ShareRange} from './ScenarioWeights';
import {basisLabels, distributionDraft, readDistribution, requestDistribution, sameDistribution, scenarioCoordinates} from './studyScenarioWeights';
const states={pass:'达标',outside:'超限',unknown:'待核对'};
const coverage={pass:'可覆盖全部采样工况',outside:'无法覆盖全部采样工况',unknown:'能否覆盖全部采样工况待核对'};
const policyLabels={fixed:'固定设定',adaptive:'按可辨别工况调节'};

function coordinates(fields,values) {
  return fields.map((f,i)=>f.label+' = '+String(values[i])).join('，');
}

function countsText(counts) {
  return Object.entries(states).map(([key,label])=>counts[key]+' 个'+label).join('，');
}

function Status({state}) {
  return <strong className={'adjustment-status '+state}>{states[state]}</strong>;
}

function Picker({label,value,choices,onChange}) {
  return <label>{label}<select aria-label={label} value={value} onChange={e=>onChange(Number(e.target.value))}>
    {choices.map(({id,text})=><option value={id} key={id}>{text}</option>)}
  </select></label>;
}

const designsPerPage=6;
function DesignTable({designs,fields,adaptive,selected,page,onPage,onSelect}) {
  const start=page*designsPerPage,end=Math.min(start+designsPerPage,designs.length);
  return <><table className="adjustment-summary"><caption>比较 {designs.length} 个方案的采样结果</caption>
    <thead><tr><th scope="col">{fields.length===1?fields[0].label:'方案参数'}</th><th scope="col">固定设定</th>
      {adaptive && <th scope="col">按工况调节</th>}</tr></thead>
    <tbody>{designs.slice(start,end).map((d,i)=>{
      const id=start+i,text=coordinates(fields,d.design);
      return <tr key={id} className={id===selected?'selected':''} onClick={()=>onSelect(id)}>
        <th scope="row"><button type="button" className="bench-text-btn" aria-label={'查看方案 '+text} aria-pressed={id===selected}>
          {fields.length===1?String(d.design[0]):text}</button></th>
        <td><Status state={d.fixed.state}/>{d.fixed.mass && <small>加权达标 <ShareRange mass={d.fixed.mass}/></small>}</td>
        {adaptive && <td><Status state={d.adaptive.state}/>{d.adaptive.mass && <small>加权达标 <ShareRange mass={d.adaptive.mass}/></small>}</td>}</tr>;
    })}</tbody></table>
    {designs.length>designsPerPage && <nav className="adjustment-pages" aria-label="方案表分页">
      <button type="button" disabled={start===0} onClick={()=>onPage(page-1)}>上一页</button>
      <span>第 {start+1}–{end} 个，共 {designs.length} 个</span>
      <button type="button" disabled={end===designs.length} onClick={()=>onPage(page+1)}>下一页</button>
    </nav>}</>;
}

function AcceptedControls({available,fields,adaptive,chose,onSelect}) {
  const passed=available.map((o,index)=>({...o,index})).filter(o=>o.state==='pass');
  const scope=adaptive?'此工况':'全部工况';
  function settings(list) {
    return <ul className="adjustment-settings">{list.map(o=><li key={o.index}><button type="button" className="bench-text-btn"
      aria-pressed={chose===o.index} onClick={()=>onSelect(o.index)}>{coordinates(fields,o.control)}</button></li>)}</ul>;
  }
  return <div className="accepted-controls" aria-label="达标调节设定">
    <p>覆盖{scope}的调节设定 · {passed.length} / {available.length} 组达标</p>
    {settings(passed.slice(0,3))}
    {passed.length>3 && <details><summary>其余 {passed.length-3} 组达标设定</summary>{settings(passed.slice(3))}</details>}
  </div>;
}

function CellEvidence({cell,report,limits,onOpen,selection}) {
  return <div className="adjustment-evidence"><p><Status state={cell.state}/> · {cell.reason}</p>
    {cell.rows.map((r,i)=>{
      const row=Number.isInteger(r.row_index) ? report.rows[r.row_index] : null;
      return <div key={i} className="adjustment-record">
        {Number.isInteger(r.row_index) && <small>原比较第 {r.row_index+1} 行 · {states[r.state]}</small>}
        {r.reason && r.reason!==cell.reason && <small>{r.reason}</small>}
        {r.run_id && r.solver_identity==='unavailable' && <small>原计算未留可核对的方法指纹。</small>}
        {row && <small>{r.state==='unknown' && !r.issues.length?'原比较保存的读数，当前依据不足以核验。':'原比较保存的读数'}</small>}
        {row && limits.map(m=>{
          const issue=r.issues?.find(v=>v.output===m.output);
          const inherited=row?.root_measurements?.includes(m.output) && (row.measurement_source_search_id ?? report.root_search_id);
          const v=row?.values?.[m.output];
          return <div key={m.output}><span>{m.label} · {Number.isFinite(v)?[v,m.unit].filter(x=>x!=null && x!=='').join(' '):'缺少读数'}</span>
            {issue?.message && <small>{issue.message}</small>}
            {inherited && <small>读数沿用首轮比较 #{inherited}</small>}</div>;
        })}
        {r.run_id && <button type="button" className="bench-text-btn" onClick={()=>onOpen(r.run_id,selection)}>查看计算 #{r.run_id}</button>}
      </div>;
    })}
  </div>;
}

function WeightedSetting({strategy,fields,selected,onSelect}) {
  const low=strategy.lower_option_indices,high=strategy.upper_option_indices;
  const tied=JSON.stringify(low)===JSON.stringify(high);
  const indices=tied ? [['达标占比上下界均最优',low]] : [['达标占比下界最高',low],['达标占比上界最高',high]];
  return <div className="weighted-settings">{indices.map(([label,choices])=>
    <div key={label}><span>{label}</span>{choices.map(i=><button key={i} type="button" className="bench-text-btn"
      aria-pressed={selected===i} onClick={()=>onSelect(i)}>{coordinates(fields,strategy.options[i].control)}</button>)}</div>)}</div>;
}

function distributionReference(data,report) {
  if (!data.distribution) return '';
  const dist=requestDistribution(data.distribution);
  const files=(dist.originals || []).map(f=>'#'+f.attachment_id+' SHA-256 '+f.sha256).join('、');
  const source='工况权重为'+basisLabels[dist.basis]+'，依据为「'+dist.source+'」。'+(files?'核对的原件为 '+files+'。':'');
  if (report.adjustment_evaluation===data) return source+'使用比较 #'+report.id+' 保存的完整联合分布。';
  const full=JSON.stringify(dist.scenarios);
  if (full.length<12000) return source+'按原参数列顺序给出各联合工况与权重 '+full+'。';
  const refs=dist.scenarios.map((s,i)=>[data.cells.findIndex(c=>c.scenario_index===i),s.weight]);
  return source+'以该策略核对结果的 cells（从0计）标识各工况的一个代表单元。以下每对数字依次为单元索引、联合权重 '+JSON.stringify(refs)+'。';
}

function PolicyResult({data,report,groups,roles,view,setView,onOpen,onAsk,formView,openFiles}) {
  const fieldsFor=role=>groups.flatMap((g,i)=>roles[i]===role?g:[]);
  const designFields=fieldsFor('design'),observedFields=fieldsFor('observed'),controlFields=fieldsFor('control');
  const allFields=groups.flat();
  const designIndex=selectedDesignIndex(data,view);
  const design=data.designs[designIndex];
  const canAdapt=observedFields.length>0 && controlFields.length>0;
  const retuningRequired=data.designs.filter(d=>d.fixed.state==='outside' && d.adaptive.state==='pass').length;
  const fixedUnresolved=data.designs.filter(d=>d.fixed.state==='unknown' && d.adaptive.state==='pass').length;
  const policy=canAdapt && view.policy==='adaptive' ? 'adaptive':'fixed';
  const adaptive=policy==='adaptive';
  const groupIndex=design.adaptive.groups[view.groupIndex]?view.groupIndex:design.adaptive.groups.findIndex(g=>g.state===design.adaptive.state);
  const group=design.adaptive.groups[groupIndex];
  const available=adaptive?group.options:design.fixed.options;
  const optionIndex=available[view.optionIndex]?view.optionIndex:available.findIndex(o=>o.state===(adaptive?group:design.fixed).state);
  const option=available[optionIndex];
  const cellIndex=option.cell_indices.includes(view.cellIndex)?view.cellIndex:option.cell_indices.find(index=>data.cells[index].state===option.state);
  const cell=data.cells[cellIndex];
  const selection={mode:'adjustment',roles,designIndex,...(designFields.length>0 && {focus_design:design.design}),
    policy,groupIndex,optionIndex,cellIndex,...formView};
  const switchDesign=id=>setView({designIndex:id,policy});
  const switchSetting=id=>setView({designIndex,policy,groupIndex,optionIndex:id});
  function ask() {
    const assignments=groups.map((g,i)=>g.map(f=>f.label).join(' ＋ ')+' → '+roleLabels[roles[i]]);
    const context='比较 #'+report.id+'，装调假设中的参数组依序为 '+assignments.join('、')+'。角色 '+JSON.stringify(roles)+'。';
    const hypothesis=canAdapt?'按工况调节以调节前能区分工况、实物能达到设定为前提。':'';
    const subject=designFields.length?'当前方案 '+coordinates(designFields,design.design)+'（按方案参数原序排列为 '+JSON.stringify(design.design)+'）':'当前方案';
    const findings=subject+'的采样结果为固定设定'+states[design.fixed.state]+(canAdapt?'、按可辨别工况调节'+states[design.adaptive.state]:'')+'。';
    onAsk(context+'\n'+findings+hypothesis+distributionReference(data,report)+'当前查看'+policyLabels[policy]+'，证据取值为 '+coordinates(allFields,cell.coordinates)+'（'+states[cell.state]+'）。\n',
      {artifact_ids:[report.id],...(data.distribution?.originals?.length && {attachment_ids:data.distribution.originals.map(f=>f.attachment_id)})});
  }
  return <section className="tolerance-result adjustment-result" aria-label="调节策略比较结果">
    <Limits limits={data.limits}/>
    {data.distribution && <section aria-label="所选方案的加权达标占比" className="weighted-result">
      <h5>{designFields.length?coordinates(designFields,design.design)+' · ':''}达标占比</h5>
      <dl>{(canAdapt?['fixed','adaptive']:['fixed']).map(key=><div key={key}>
        <dt>{policyLabels[key]}</dt><dd><ShareRange mass={design[key].mass}/>
          <small className="coverage-status">{coverage[design[key].state]}</small></dd>
      </div>)}</dl>
      <p className="scenario-basis"><strong>{basisLabels[data.distribution.basis]}</strong> · {data.distribution.source}</p>
      <small>达标判定来自已存的模型结果。下界只计已有达标证据的权重，上界也计入待核对工况。两端可能由不同调节设定达到。</small>
    </section>}
    {data.distribution?.originals?.length>0 && <details className="scenario-originals"><summary>本次权重引用的原件</summary>
      <small>文件指纹用于核对引用的原件，工况权重由填写者提供。</small>
      {data.distribution.originals.map(f=><div key={f.attachment_id}>
        {openFiles?<button type="button" className="bench-text-btn" onClick={()=>openFiles(f.attachment_id)}>{f.relative_path || '原件'} · #{f.attachment_id}</button>
          :<span>{f.relative_path || '原件'} · #{f.attachment_id}</span>}<small>SHA-256 · {f.sha256}</small></div>)}</details>}
    {designFields.length>0 && <DesignTable designs={data.designs} fields={designFields} adaptive={canAdapt} selected={designIndex}
      page={view.page ?? Math.floor(designIndex/designsPerPage)} onPage={page=>setView({...view,page})} onSelect={switchDesign}/>}
    {canAdapt && data.designs.length>1 && retuningRequired>0 && <p>有 {retuningRequired} 个方案的固定设定均曾超限，按工况选择设定后可覆盖全部采样工况。</p>}
    {canAdapt && data.designs.length>1 && fixedUnresolved>0 && <p>有 {fixedUnresolved} 个方案已找到按工况调节的达标设定，固定设定的依据仍不完整。</p>}
    <small>固定设定要求同一组调节值覆盖全部采样工况。{canAdapt && '按工况调节假设能在调节前辨别所选工况，每种可辨别工况可以采用不同设定。'}
      {roles.includes('hidden') && ' 无法辨别的变化须由同一设定覆盖。'}
      {data.distribution && ' 全部工况的判定也涵盖零权重采样点。'}
      {controlFields.length>0 && ' 采用某个设定还需确认实物可调到对应取值。'} 采样点之间的表现仍待验证。</small>
    <div className="grid-fields tolerance-selectors">
      {designFields.length>0 && <Picker label="查看方案" value={designIndex} choices={data.designs.map((d,id)=>({id,
        text:coordinates(designFields,d.design)+' · 固定'+states[d.fixed.state]+(canAdapt?' / 按工况'+states[d.adaptive.state]:'')}))}
        onChange={switchDesign}/>}
      {canAdapt && <label>查看策略<select aria-label="查看策略" value={policy} onChange={e=>setView({designIndex,policy:e.target.value})}>
        {Object.entries(policyLabels).map(([k,l])=><option key={k} value={k}>{l}</option>)}</select></label>}
    </div>
    {!data.distribution && <p>{designFields.length?coordinates(designFields,design.design):'本次比较的方案'} · 固定设定 <Status state={design.fixed.state}/>
      {canAdapt && <> · 按工况调节 <Status state={design.adaptive.state}/></>}</p>}
    <div className="grid-fields tolerance-selectors">
      {adaptive && <Picker label="可辨别工况" value={groupIndex} choices={design.adaptive.groups.map((g,id)=>({id,
        text:coordinates(observedFields,g.observed)+' · '+states[g.state]}))}
        onChange={id=>setView({designIndex,policy,groupIndex:id})}/>}
      {controlFields.length>0 && <Picker label="调节设定" value={optionIndex} choices={available.map((o,id)=>({id,
        text:coordinates(controlFields,o.control)+' · '+states[o.state]}))}
        onChange={switchSetting}/>}
    </div>
    {adaptive && <small>此工况 · {coordinates(observedFields,group.observed)}</small>}
    {adaptive && group.mass && <p>所选可辨别工况占全部权重的 <Share mass={group} field="fraction"/>，
      可贡献的达标占比为 <ShareRange mass={group.mass}/>。</p>}
    {option.mass && controlFields.length>0 && <WeightedSetting strategy={adaptive?group:design.fixed}
      fields={controlFields} selected={optionIndex} onSelect={switchSetting}/>}
    {controlFields.length>0 && <AcceptedControls {...{available,adaptive}} fields={controlFields} chose={optionIndex} onSelect={switchSetting}/>}
    <p>{controlFields.length?coordinates(controlFields,option.control):'当前方案'}。在该设定对应的采样组合中，{countsText(option.counts)}。</p>
    {option.mass && <p>按全部工况的总权重计，此设定已达标 <Share mass={option.mass} field="pass"/>，
      已超限 <Share mass={option.mass} field="outside"/>，待核对 <Share mass={option.mass} field="unknown"/>。</p>}
    <Picker label="采样证据" value={cellIndex} choices={option.cell_indices.map(i=>({id:i,
      text:states[data.cells[i].state]+' · '+coordinates(allFields,data.cells[i].coordinates)}))}
      onChange={id=>setView({...selection,cellIndex:id})}/>
    <p className="adjustment-coordinates">{coordinates(allFields,cell.coordinates)}</p>
    {data.distribution && <p>这组证据对应的联合工况占全部权重的 <Share mass={data.distribution.scenarios[cell.scenario_index]} field="fraction"/>。</p>}
    <CellEvidence {...{cell,report,onOpen,selection}} limits={data.limits}/>
    {onAsk && <button type="button" className="bench-text-btn" onClick={ask}>引用调节策略问 Agent</button>}
  </section>;
}

export default function StudyAdjustment({caseId,report,options,onOpen,onAsk,initial,hasLimits,onSetLimits,onSavePolicy,files=[],openFiles}) {
  const archived=report.adjustment_evaluation;
  const [open,setOpen]=useState(Boolean(initial));
  const [roles,setRoles]=useState(()=>initial?.roles || archived?.roles || report.recipe.axes.map(()=>''));
  const [view,setView]=useState(initial || {});
  const [distribution,setDistribution]=useState(()=>requestDistribution(initial?initial.distribution:archived?.distribution));
  const [draft,setDraft]=useState(()=>initial?.weightDraft || distributionDraft(distribution));
  const [editorOpen,setEditorOpen]=useState(Boolean(initial?.weightEditorOpen));
  const [saveError,setSaveError]=useState(''),[saving,setSaving]=useState(false);
  const previousConditions=useRef(new Map());
  const [reply,setReply]=useState(null),[attempt,retry]=useState(0);
  const header=useRef(null);
  const fieldId=useId();
  const complete=roles.length===report.recipe.axes.length && roles.every(r=>Object.hasOwn(roleLabels,r));
  const showArchive=archived && JSON.stringify(roles)===JSON.stringify(archived.roles) && sameDistribution(distribution,archived.distribution);
  useEffect(()=>{if(initial)header.current?.focus();},[initial]);
  useEffect(()=>{
    if(!open || !complete || !hasLimits || showArchive)return;
    const controller=new AbortController();
    const request={search_id:report.id,roles,...(distribution && {distribution})};
    workbenchApi.adjustmentPolicy(caseId,request,controller.signal)
      .then(data=>{if(!controller.signal.aborted)setReply({roles,distribution,attempt,data});})
      .catch(error=>{if(!controller.signal.aborted)setReply(old=>({roles,distribution,attempt,data:old?.data,error:error.message}));});
    return ()=>controller.abort();
  },[open,complete,roles,distribution,attempt,caseId,report.id,hasLimits,showArchive]);
  const current=showArchive?{data:archived}:reply?.roles===roles && reply.distribution===distribution && reply.attempt===attempt ? reply : null;
  const groups=open && hasLimits?report.recipe.axes.map(a=>a.paths.map(p=>axisField(p,options))):[];
  const conditionFields=groups.flatMap((g,i)=>['observed','hidden'].includes(roles[i])?g:[]);
  const points=scenarioCoordinates(reply?.data?.cells || archived?.cells || report.rows,groups,roles);
  let dirty=Boolean(draft.basis || draft.source || draft.originals?.length || Object.keys(draft.weights).length);
  if (distribution) {
    try {dirty=!sameDistribution(readDistribution(draft,points),distribution);}
    catch {dirty=true;}
  }
  function changeRole(index,value) {
    const next=roles.map((r,i)=>i===index?value:r);
    const dimensions=values=>values.map(r=>['observed','hidden'].includes(r)?1:0).join('');
    if (dimensions(roles)!==dimensions(next)) {
      previousConditions.current.set(dimensions(roles),{draft,distribution});
      const previous=previousConditions.current.get(dimensions(next));
      setDistribution(previous?.distribution || null);
      setDraft(previous?.draft || distributionDraft(null));
    }
    setRoles(next);setView({});setSaveError('');
  }
  async function save() {
    setSaving(true);setSaveError('');
    const weights=requestDistribution(current.data.distribution);
    const focus=current.data.designs[selectedDesignIndex(current.data,view)].design;
    try {await onSavePolicy({search_id:report.id,roles,...(focus.length>0 && {focus_design:focus}),
      ...(weights && {distribution:weights})});}
    catch(cause){setSaveError(cause.message);}
    finally {setSaving(false);}
  }
  return <details className="study-adjustment" open={open} onToggle={e=>{if(e.target===e.currentTarget)setOpen(e.currentTarget.open);}}>
    <summary ref={header}>比较固定与按工况调节</summary>
    {open && !hasLimits && <div><p>这次比较只指定了优化方向。要判断设定能否达标，请给至少一个评价量设置允许上限或下限，再保存新评价。</p>
      <button type="button" className="bench-text-btn" onClick={onSetLimits}>设置验收限值</button></div>}
    {open && hasLimits && <><p>为参与比较的参数指定角色。同一参数组保留原来的联动取值。</p>
      <div className="grid-fields tolerance-selectors">{groups.map((g,i)=><label key={i}><span id={fieldId+'-'+i}>{g.map(f=>f.label).join(' ＋ ')}</span>
        <select aria-label={'参数组 '+(i+1)+' 的角色'} aria-describedby={fieldId+'-'+i} disabled={saving} value={roles[i]} onChange={e=>changeRole(i,e.target.value)}>
          <option value="">选择角色</option>{Object.entries(roleLabels).map(([k,l])=><option key={k} value={k}>{l}</option>)}
        </select></label>)}</div>
      {complete && (conditionFields.length>0 || distribution) && <ScenarioWeights fields={conditionFields} points={points} files={files}
        draft={draft} onChange={value=>{setDraft(value);setSaveError('');}} applied={distribution} dirty={dirty}
        open={editorOpen} onToggle={setEditorOpen} onApply={value=>{setDistribution(value);setSaveError('');}}
        onClear={()=>{setDistribution(null);setDraft(distributionDraft(null));setSaveError('');}}/>}
      {complete && !editorOpen && dirty && <small role="status">工况权重有尚未应用的修改。</small>}
      {complete && (!current?<p role="status">正在核对已存采样…</p>: current.error?<p role="alert">{current.error}
        <button type="button" onClick={()=>retry(n=>n+1)}>重试</button></p>:<>
        {showArchive && <small>正在查看已保存的工况评价 #{report.id}。</small>}
        <PolicyResult data={current.data} {...{view,setView,report,groups,roles,onOpen,onAsk,openFiles}}
          formView={{distribution:requestDistribution(current.data.distribution),weightEditorOpen:editorOpen,...(dirty && {weightDraft:draft})}}/>
        {onSavePolicy && !dirty && !showArchive && <button type="button" className="btn btn-secondary"
          disabled={saving} onClick={save}>{saving?'正在保存…':'保存本次工况评价'}</button>}
        {saveError && <p role="alert">{saveError}</p>}</>)}
    </>}
  </details>;
}
