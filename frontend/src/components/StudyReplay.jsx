import React, {useEffect, useRef, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import ScientificPlot from './ScientificPlot';
import {curveTraces} from './studyTable.js';
import {SolverReceipt} from './SolverReceipt';

const actions={reuse:'沿用',replace:'替换来源',recalculate:'待重算',computed:'已复算',unavailable:'需要处理',waiting:'等待上游'};
const states={completed:'已计算',partial:'部分结果',needs_input:'待补输入',unsupported:'超出模型范围'};
const reading=value=>value==null?'—':typeof value==='number'?String(Number(value.toPrecision(8))):String(value);

function CurveComparison({caseId,original,newId}) {
  const [current,setCurrent]=useState(null),[error,setError]=useState(''),[choice,setChoice]=useState('');
  const [opened,setOpened]=useState(false),[attempt,retry]=useState(0);
  useEffect(()=>{
    if(!opened)return;
    let live=true;
    workbenchApi.studyRun(caseId,newId).then(data=>{if(live){setCurrent(data);setError('');}})
      .catch(cause=>{if(live)setError(cause.message);});
    return ()=>{live=false;};
  },[caseId,newId,opened,attempt]);
  const [xTitle,yTitle,series]=original.definition.chart;
  const chart=current?.definition.chart;
  const pairs=chart && chart[0]===xTitle && chart[1]===yTitle ? series.flatMap(spec=>{
    const match=chart[2].find(other=>other[1]===spec[1] && other[2]===spec[2] && other[3]===spec[3]);
    if(!match)return [];
    const [old]=curveTraces([spec],original.output), [next]=curveTraces([match],current.output);
    return old && next ? [{label:spec[0],key:JSON.stringify(spec),old,next}] : [];
  }):[];
  const active=pairs.find(item=>item.key===choice) || pairs[0];
  const layout={autosize:true,margin:{l:70,r:20,t:20,b:65},xaxis:{title:{text:xTitle}},yaxis:{title:{text:yTitle}},
    legend:{orientation:'h',y:-.25},paper_bgcolor:'#131d2a',plot_bgcolor:'#131d2a',font:{color:'#d2dfe5'}};
  return <details onToggle={e=>{if(e.currentTarget.open)setOpened(true);}}><summary>叠加原曲线与新曲线</summary>
    {opened && !current && !error && <p role="status">正在读取已存曲线…</p>}
    {error && <p role="alert">{error} <button type="button" onClick={()=>retry(n=>n+1)}>重读曲线</button></p>}
    {current && !active && <p>两次结果没有同时发布可按相同坐标叠加的曲线。</p>}
    {active && <>{pairs.length>1 && <label>对照曲线<select aria-label="对照曲线" value={active.key} onChange={e=>setChoice(e.target.value)}>
      {pairs.map(item=><option key={item.key} value={item.key}>{item.label}</option>)}</select></label>}
      <ScientificPlot ariaLabel={'复算曲线对照 · '+active.label} filename="study-comparison" layout={layout}
        data={[{...active.old,name:'原 #'+original.id,line:{dash:'dash',color:'#f0ba79'},marker:{symbol:'circle-open',color:'#f0ba79'}},
          {...active.next,name:'新 #'+newId,line:{color:'#72d7ce'},marker:{symbol:'cross',color:'#72d7ce'}}]}/></>}
  </details>;
}

function ReplacementOptions({caseId,source,value,onChange}) {
  const [page,setPage]=useState(null), [error,setError]=useState(''), [loading,setLoading]=useState(false);
  const [attempt,retry]=useState(0);
  const load=source.kind==='project'?workbenchApi.modelVersions:workbenchApi.studyRuns;
  useEffect(()=>{
    let active=true;
    load(caseId).then(result=>{if(active){setPage(result);setError('');}})
      .catch(cause=>{if(active)setError(cause.message);});
    return ()=>{active=false;};
  },[caseId,load,attempt]);
  async function more() {
    setLoading(true);
    try {
      const result=await load(caseId,page.next_before_id);
      setPage({...result,items:[...page.items,...result.items]});setError('');
    } catch(cause){setError(cause.message);} finally{setLoading(false);}
  }
  const options=(page?.items || []).filter(item=>item.id!==source.id);
  return <><label>替代来源<select aria-label="替代来源" value={value} onChange={e=>onChange(Number(e.target.value))}>
    <option value="">{source.kind==='project'?'选择另一个工程版本或试算':'选择另一次已存计算'}</option>
    {value && !options.some(item=>item.id===value) && <option value={value}>#{value}</option>}
    {options.map(item=><option key={item.id} value={item.id}>#{item.id} · {item.variant_name || item.title}{item.change_note && ' · '+item.change_note}</option>)}
  </select></label>
    {page && !options.length && !page.next_before_id && <small>此案例还没有其他同类来源。</small>}
    {page?.next_before_id && <button type="button" disabled={loading} onClick={more}>更早的来源</button>}
    {error && <p role="alert">{error} <button type="button" onClick={()=>page?more():retry(n=>n+1)}>重读来源</button></p>}
  </>;
}

function ReplayEditor({caseId,record,initial,onOpen,onSaved}) {
  const [sourceId,setSource]=useState(initial?.sourceId || 0), [replacement,setReplacement]=useState(initial?.replacementId || 0);
  const [notes,setNotes]=useState({}), [preview,setPreview]=useState(null), [result,setResult]=useState(null);
  const [attempt,retry]=useState(0), [running,setRunning]=useState(false), [error,setError]=useState('');
  const request=JSON.stringify({run_id:record.id,...(sourceId && replacement?{source_id:sourceId,replacement_id:replacement}:{})});
  useEffect(()=>{
    const controller=new AbortController();
    workbenchApi.replayStudy(caseId,JSON.parse(request),controller.signal).then(data=>{
      if(!controller.signal.aborted){setPreview({request,data});setError('');}
    }).catch(cause=>{if(!controller.signal.aborted)setError(cause.message);});
    return ()=>controller.abort();
  },[caseId,request,attempt]);
  const plan=preview?.request===request?preview.data:null;
  const source=preview?.data.sources.find(s=>s.id===sourceId);
  const displayed=result || plan;
  const resultsById=new Map(displayed?.steps.map(step=>[step.id,step.result_id]));
  const dependency=(kind,id,next)=>kind+' #'+id+(next && next!==id?' → #'+next:'');
  function replace(id) {setReplacement(id);setNotes({});setResult(null);setError('');}
  async function submit(e) {
    e.preventDefault();setRunning(true);setError('');
    try {
      const reply=await workbenchApi.replayStudy(caseId,{...JSON.parse(request),execute:true,registrations:notes});
      setResult(reply);if(reply.steps.some(s=>s.action==='computed'))onSaved();
    } catch(cause){setError(cause.message);} finally{setRunning(false);}
  }
  return <form onSubmit={submit} className="study-replay"><fieldset disabled={running}>
    <label>替换哪一项来源<select aria-label="替换哪一项来源" value={sourceId} onChange={e=>{setSource(Number(e.target.value));replace(0);}}>
      <option value="0">选择原来源</option>{preview?.data.sources.map(item=><option key={item.id} value={item.id}>
        {item.kind==='project'?'工程':'计算'} #{item.id} · {item.title}</option>)}</select></label>
    {source && <ReplacementOptions key={source.id} caseId={caseId} source={source} value={replacement || ''} onChange={replace}/>}
    {!plan && !error && <small role="status">正在读取计算依赖…</small>}
    {displayed && <ol className="replay-steps">{displayed.steps.map(step=><li key={step.id}>
      <strong>#{step.id} · {step.title}</strong> · {step.execution==='reused' ? '已沿用计算' : actions[step.action]}
      {step.science_status && step.science_status!=='completed' && <>（{states[step.science_status] || step.science_status}）</>}
      {step.result_id && step.result_id!==step.id && <> → 计算 #{step.result_id}</>}
      {step.installed_solver_receipt && <SolverReceipt saved={step.saved_solver_receipt} current={step.installed_solver_receipt}
        left="原计算" right="本次方法"/>}
      {step.action==='computed' && step.id!==record.id && <button type="button" className="bench-text-btn"
        onClick={()=>onOpen(step.result_id)}>查看计算 #{step.result_id}</button>}
      {(step.projects.length>0 || step.parents.length>0) && <small>{[
        ...step.projects.map(id=>dependency('工程',id,id===sourceId && source?.kind==='project'?replacement:null)),
        ...step.parents.map(id=>dependency('计算',id,resultsById.get(id)))].join('、')}</small>}
      {step.message && <small role="alert">{step.message}</small>}
      {step.row_matches?.map(match=><small key={match.input}>{match.label} · 原第 {match.old_row} 行 → 新第 {match.new_row} 行
        {' · '+Object.entries(match.coordinates).map(([key,value])=>key+' = '+value).join('，')}</small>)}
    </li>)}</ol>}
    {plan?.steps.flatMap(step=>Object.entries(step.registrations || {}).map(([field,info])=><label key={step.id+':'+field}>
      #{step.id} · {info.label}{info.required?'（必填）':'（需要跨工况引用时填写）'}
      {info.previous && <small>原先的依据 · {info.previous}</small>}
      <input aria-label={'计算 #'+step.id+' · '+info.label} required={info.required} maxLength={500} value={notes[step.id]?.[field] || ''} onChange={e=>{
        setNotes({...notes,[step.id]:{...notes[step.id],[field]:e.target.value}});setResult(null);
      }}/></label>))}
    {plan?.replacement_id && <button className="btn btn-primary" type="submit"
      disabled={!plan.steps.some(s=>s.action==='recalculate')}>{running?'正在复算…':'重算受影响步骤'}</button>}
    {error && <p role="alert">{error} <button type="button" onClick={()=>retry(n=>n+1)}>重新读取</button></p>}
    {result?.result_id && <section aria-label="两次计算对照"><p>新计算 #{result.result_id} · {states[result.status] || result.status}</p>
      {result.comparison?.length>0 && <div className="study-scroll"><table><thead><tr><th>结果</th><th>原计算 #{record.id}</th><th>新计算 #{result.result_id}</th></tr></thead>
        <tbody>{result.comparison.map((item,i)=><tr key={i}><th scope="row">{item.label}</th>
          <td>{reading(item.previous)}</td><td>{reading(item.current)}</td></tr>)}</tbody></table></div>}
      {record.definition.chart && <CurveComparison key={result.result_id} caseId={caseId} original={record} newId={result.result_id}/>}
      <button type="button" className="btn btn-secondary" onClick={()=>onOpen(result.result_id)}>查看新计算 #{result.result_id}</button>
    </section>}
  </fieldset></form>;
}

export default function StudyReplay(props) {
  const [visited,setVisited]=useState(Boolean(props.initial));
  const details=useRef(null);
  useEffect(()=>{
    if(props.initial && details.current) {
      details.current.open=true;
      details.current.querySelector('summary')?.focus();
    }
  },[props.initial]);
  if(!Object.values(props.record.connections || {}).some(r=>r.run_id || r.revision_id))return null;
  return <details ref={details} onToggle={e=>{if(e.target===e.currentTarget && e.currentTarget.open)setVisited(true);}}>
    <summary>计算依赖与换来源复算</summary>{visited && <ReplayEditor key={props.initial ? props.initial.sourceId+'/'+props.initial.replacementId : 'manual'} {...props}/>}
  </details>;
}
