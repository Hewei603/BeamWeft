import React, {useEffect, useMemo, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import {useStudyDefinition} from './CapabilityBrowser';
import {InputField, shape, valueFor} from './ScientificInput';
import {ScientificStudyOutput} from './StudyPanels';
import {useBenchPreview} from './useBenchPreview';
import {compatiblePorts, exclusiveFields, fitsPort, sourceKey} from './studyConnections';
import ExclusiveStudyInput from './ExclusiveStudyInput';
import SourceConditions from './SourceConditions';
import StudyStateValue from './StudyStateValue';
import {studyFieldSelections} from './layoutSelections';

const empty = {};

function useLiveSources(caseId,revision,needed) {
  const [history,setHistory]=useState(null), [model,setModel]=useState(null);
  const [historyError,setHistoryError]=useState(''),[modelError,setModelError]=useState('');
  const [before,setBefore]=useState(null), [attempt,retry]=useState(0);
  useEffect(()=>{
    if(!needed)return;
    let live=true;
    workbenchApi.studyRuns(caseId,before).then(page=>{
      if(live){setHistory(old=>before && old ? {...page,items:[...old.items,...page.items]} : page);setHistoryError('');}
    }).catch(e=>{if(live)setHistoryError(e.message);});
    return ()=>{live=false;};
  },[caseId,before,attempt,needed]);
  useEffect(()=>{
    if(!needed)return;
    let live=true;
    if(revision)workbenchApi.projectOutputs(caseId,revision).then(page=>{if(live){setModel(page);setModelError('');}})
      .catch(e=>{if(live)setModelError(e.message);});
    return ()=>{live=false;};
  },[caseId,revision,attempt,needed]);
  function reload(){setBefore(null);retry(n=>n+1);}
  return {records:history?.items || [],outputs:revision && model?.revision_id===revision?model.outputs:{},
    error:historyError || (revision && modelError),reload,next:history?.next_before_id,loadingMore:before===history?.next_before_id,setBefore};
}

function LiveSourceInput({caseId,sources,revision,target,title,reference,sharedTitle,conditionMatch,onChange,manual}) {
  const options=compatiblePorts(target,sources.records);
  for(const [port,item] of Object.entries(sources.outputs)){
    if(fitsPort(target,item.port))options.push({revision_id:revision,port,declaration:item.port,label:`工程 #${revision} · ${item.location} · ${item.port.label}`});
  }
  if(reference && !options.some(item=>sourceKey(item)===sourceKey(reference)))
    options.push({...reference,label:(reference.revision_id?'工程 #'+reference.revision_id:'计算 #'+reference.run_id)+' · '+reference.port});
  const picked=reference && options.find(item=>sourceKey(item)===sourceKey(reference));
  return <div className="study-input live-state-source">
    {(options.length>0 || sources.next) && <label>{title}的来源
      <select aria-label={title+'的来源'} value={reference?sourceKey(reference):''} onChange={e=>{
        const item=options.find(o=>sourceKey(o)===e.target.value);
        onChange(item ? {...(item.revision_id?{revision_id:item.revision_id}:{run_id:item.run_id}),port:item.port} : null);
      }}><option value="">手动填写或留空</option>
        {options.map(item=><option key={sourceKey(item)} value={sourceKey(item)}>{item.label}</option>)}</select></label>}
    {reference ? <><SourceConditions source={picked?.declaration} target={target}/>
      {target.shape==='state' && <StudyStateValue key={sourceKey(reference)} title="查看引用状态" port={target} caseId={caseId} source={reference}/>}
      {(target.shape==='state' || conditionMatch || sharedTitle) && <label>{target.registration_label || (conditionMatch?'跨工况引用依据':'参考面与轴向对应关系')}
      <input aria-label={title+'配准依据'} maxLength={500} value={reference.registration || ''}
        placeholder={sharedTitle?'留空沿用「'+sharedTitle+'」':undefined}
        onChange={e=>onChange({...reference,registration:e.target.value})}/></label>}</> : manual}
  </div>;
}

function resolvedParameters(definition, values, connections, fieldSelections, selectedPlane, alternatives, choice) {
  const schema=definition.inputs, inputs={};
  for(const [key,field] of Object.entries(schema.properties)){
    const label=field.title || key;
    try {
      if(key===definition.live_layout || (alternatives.includes(key) && key!==choice))continue;
      if(connections[key]){
        const shared=definition.registration_inputs?.[key];
        if(definition.input_ports?.[key]?.shape==='state' && !connections[key].registration?.trim() && !values[shared]?.trim())
          return {problem:'请说明「'+label+'」的来源面与本段光路的配准依据。'};
        continue;
      }
      const binding=fieldSelections[key];
      const followed=key===definition.layout_prefix_inputs?.plane ? selectedPlane || binding?.current : binding?.current;
      const raw=Object.hasOwn(values,key) ? values[key] : followed;
      if(raw!=null && raw!==''){
        if(binding && !binding.choices.some(item=>item.value===raw))return {problem:'原选择已移除，请重新选择「'+binding.label+'」。'};
        inputs[key]=valueFor(shape(field),raw);
      } else if(key===choice)return {problem:'请选择已存来源，或填写「'+label+'」。'};
      else if(schema.required?.includes(key) || (values[key]==='' && field.default!=null))return {problem:'请填写「'+label+'」。'};
    } catch(error) {
      return {problem:'请检查「'+label+'」的输入。'+(error instanceof SyntaxError?'内容需要符合 JSON 格式。':error.message)};
    }
  }
  return {parameters:JSON.stringify(inputs)};
}

function LiveMethod({caseId, definition, layout, revision, previewRevision, selections, disabled, selectedPlane, sections, onOpenStudy, values, setValues,
  bindings,setBindings,choice,setChoice}) {
  const [saving,setSaving]=useState(false), [receipt,setReceipt]=useState(null);
  const schema=definition.inputs;
  const fieldSelections=studyFieldSelections(schema,selections);
  const alternatives=useMemo(()=>exclusiveFields(schema),[schema]);
  const connections=useMemo(()=>alternatives.length ? Object.fromEntries(Object.entries(bindings)
    .filter(([key])=>!alternatives.includes(key) || key===choice)) : bindings,[bindings,alternatives,choice]);
  const fields=Object.entries(schema.properties).filter(([key])=>key!==definition.live_layout);
  const sources=useLiveSources(caseId,revision,fields.some(([key])=>definition.input_ports?.[key]));
  // Pinning a probe makes the numerical job independent of inspector navigation.
  const {parameters,problem}=alternatives.length && !choice ? {problem:'请选择本次使用的输入。'}
    : resolvedParameters(definition,values,connections,fieldSelections,selectedPlane,alternatives,choice);
  const support=definition.layout_prefix_inputs;
  const fullEndpoint=support && values[support.plane]===null;
  const job=useMemo(()=>{
    if(parameters===undefined)return null;
    const inputs=JSON.parse(parameters);
    let prefix;
    if(support && selectedPlane && !fullEndpoint) {
      const plane=inputs[support.plane] || selectedPlane;
      prefix={plane,path:inputs[support.path] || 'primary'};
      inputs[support.path]='primary';
      inputs[support.plane]=plane;
    }
    return {name:definition.name,version:definition.version,provider:definition.provider,prefix,connections,
      inputs:{...inputs,[definition.live_layout]:layout}};
  },[definition,layout,parameters,connections,selectedPlane,support,fullEndpoint]);
  const prefix=job?.prefix;
  const station=prefix && sections?.planes?.find(p=>p.uid===prefix.plane);
  const beam=station?.beams?.find(b=>b.uid===prefix.path);
  const sourcePort=prefix ? beam?.layout_prefix_port : 'v1/layout';
  const endpoints=prefix && [
    selections['path-uid'].choices.find(p=>p.value===prefix.path)?.label || prefix.path,
    selections['plane-uid'].choices.find(p=>p.value===prefix.plane)?.label || prefix.plane,
  ];
  const preview=useBenchPreview(caseId,job,!!job,workbenchApi.previewStudy,previewRevision);
  const lastJob=preview.previous?.system;
  const sameTarget=job && lastJob && job.connections===lastJob.connections && prefix?.path===lastJob.prefix?.path && prefix?.plane===lastJob.prefix?.plane &&
    fields.every(([key])=>!fieldSelections[key] || job.inputs[key]===lastJob.inputs[key]);
  const output=preview.data || (sameTarget && preview.previous?.data);
  const keep=async()=>{
    const { [definition.live_layout]: model, ...inputs }=job.inputs;
    if(!model || !revision || !sourcePort)return;
    setSaving(true);
    try{
      const saved=await workbenchApi.runStudy(caseId,definition,inputs,{...job.connections,[definition.live_layout]:{
        revision_id:revision,port:sourcePort,registration:prefix ?
          endpoints[0]+'光源至「'+endpoints[1]+'」，局部 z 从该光源量起，沿用工程 T/S 轴。' :
          '采用本版工程登记的光路、端口参考面与 T/S 轴。'}});
      setReceipt({job,revision,id:saved.study_record.id,text:'已存入分析记录 #'+saved.study_record.id});
      sources.reload();
    }catch(error){setReceipt({job,revision,text:error.message});}
    finally{setSaving(false);}
  };
  const basic=[],more=[];
  for(const entry of fields){
    const [key,field]=entry, spec=shape(field);
    const structured=!spec.enum && !['number','integer','string','boolean'].includes(spec.type);
    if(alternatives.includes(key) && key!==choice)continue;
    const choiceRequired=key===choice;
    const optionalStructure=!schema.required?.includes(key) && !choiceRequired && !fieldSelections[key] &&
      (structured || definition.input_ports?.[key]?.shape==='state');
    (optionalStructure ? more : basic).push(entry);
  }
  const filled=more.filter(([key,field])=>connections[key] || ((values[key] ?? field.default)!=null && (values[key] ?? field.default)!==''));
  const names=items=>items.map(([key,field])=>field.title || key).join('、');
  function control([key,field]) {
    const change=value=>setValues(prev=>({...prev,[key]:value}));
    const binding=fieldSelections[key];
    if(binding){
      const pinned=values[key];
      const current=binding.choices.find(item=>item.value===binding.current);
      const scopedPlane=support?.plane===key && selectedPlane;
      const canChooseEnd=scopedPlane && !schema.required?.includes(key);
      // The end of the train has no object UID. Keep its menu value distinct from user-defined UIDs.
      let endValue='@end';
      while(binding.choices.some(c=>c.value===endValue))endValue+='.';
      return <label key={key}>{binding.label}<select aria-label={'随动'+binding.label} value={pinned ?? (canChooseEnd && pinned===null ? endValue : '')} onChange={e=>{
        const value=e.target.value;
        if(value==='')setValues(prev=>{const next={...prev};delete next[key];return next;});
        else change(canChooseEnd && value===endValue ? null : value);
      }}>
        <option value="">{support?.plane===key && selectedPlane
          ? '跟随图中观察面 · '+(binding.choices.find(p=>p.value===selectedPlane)?.label || selectedPlane)
          : binding.follow+(current ? ' · '+current.label : '')}</option>
        {canChooseEnd && <option value={endValue}>计算至光路末端</option>}
        {pinned && !binding.choices.some(c=>c.value===pinned) && <option value={pinned}>原选择已移除，请重新选择</option>}
        {binding.choices.map(c=><option key={c.value} value={c.value}>{c.label}</option>)}</select></label>;
    }
    const spec=shape(field), initial=spec.default==null?'':(spec.enum || typeof spec.default==='object'?JSON.stringify(spec.default):String(spec.default));
    const manual=<InputField key={key} name={key} field={field} value={values[key]??initial} schema={schema}
      markOptional={false} required={schema.required?.includes(key)} onChange={change}/>;
    const target=definition.input_ports?.[key];
    if(!target)return manual;
    const shared=definition.registration_inputs?.[key];
    return <LiveSourceInput key={key} caseId={caseId} sources={sources} revision={revision} target={target} title={field.title || key}
      reference={connections[key]} sharedTitle={schema.properties[shared]?.title || shared} manual={manual}
      conditionMatch={definition.condition_matches?.some(match=>match.other===key)}
      onChange={ref=>setBindings(old=>{
        const changed={...old};if(ref)changed[key]=ref;else delete changed[key];return changed;
      })}/>;
  }
  return <><ExclusiveStudyInput fields={alternatives} schema={schema} choice={choice} onChange={setChoice}/>
    <div className="bench-parameter-grid">{basic.map(control)}</div>
    {more.length>0 && <details className="bench-more-inputs"><summary>更多输入 · {filled.length?'已填写 '+names(filled):names(more)}</summary>
      <div className="bench-parameter-grid">{more.map(control)}</div></details>}
    {sources.next && <button type="button" disabled={sources.loadingMore} onClick={()=>sources.setBefore(sources.next)}>查找更早的来源</button>}
    {sources.error && <p role="alert">{sources.error} <button type="button" onClick={sources.reload}>重新读取来源</button></p>}
    {prefix && <p className="bench-hint" role="note">本次仅计算从{endpoints[0]}光源到「{endpoints[1]}」的光路。局部 z 从该光源量起。</p>}
    {preview.error?<p role="alert">{preview.error} <button type="button" onClick={preview.retry}>重新计算</button></p>
      :output?<><p role="status">{!preview.data?'正在复算 · 下方显示上一次结果':output.status==='completed'?(prefix?'所选片段计算完成':'当前光路计算完成'):output.status==='partial'?'当前结果需要复核':'请核对模型条件'}</p>
      <div className={!preview.data?'bench-pending-output':undefined} aria-busy={!preview.data}><ScientificStudyOutput definition={definition} output={output} compact/></div></>:<p role="status">{problem || '正在计算当前光路…'}</p>}
    {preview.data && (revision ? sourcePort ? <button type="button" className="btn btn-secondary" disabled={disabled || saving} onClick={keep}>保存此计算到分析记录</button>
      :<small>此版本尚未提供该段光路的引用，请核对观察面及其计算条件。</small>
      :<small>保存工程版本后可将计算与该版光路一起存档。</small>)}
    {receipt?.job===job && receipt?.revision===revision && <p role="status">{receipt.id && onOpenStudy ?
      <button type="button" className="bench-text-btn" onClick={()=>onOpenStudy(receipt.id)}>{receipt.text}</button> : receipt.text}</p>}
  </>;
}

export default function LiveLayoutStudy({caseId, methods, layout, opticalLayout, revision, previewRevision=revision, selections, disabled, selectedPlane, sections, onOpenStudy}) {
  const [open,setOpen]=useState(false), [choice,setChoice]=useState(''),[attempt,retry]=useState(0);
  const [drafts,setDrafts]=useState({});
  const [bindings,setBindings]=useState({});
  const [alternatives,setAlternatives]=useState({});
  const applicable=(methods || []).filter(m=>(m.selectors || []).every(k=>Object.hasOwn(selections,k)));
  const name=applicable.find(m=>m.name===choice)?.name || applicable[0]?.name;
  const reply=useStudyDefinition(caseId,open?name:null,attempt);
  const draftKey=reply?.data && JSON.stringify([caseId,name,reply.data.provider,reply.data.version]);
  if(!name)return null;
  return <details className="bench-live-study" onToggle={e=>{if(e.target===e.currentTarget)setOpen(e.currentTarget.open);}}>
    <summary>随光路更新的计算</summary>{open && <><label>计算方法<select aria-label="随动计算方法" value={name} onChange={e=>setChoice(e.target.value)}>
      {applicable.map(m=><option key={m.name} value={m.name}>{m.title}</option>)}</select></label>
    {reply?.error?<p role="alert">{reply.error.message} <button onClick={()=>retry(n=>n+1)}>重读方法</button></p>
      :reply?.data?<LiveMethod key={draftKey} caseId={caseId} definition={reply.data}
        layout={reply.data.ignores_diagram_positions && opticalLayout ? opticalLayout : layout} revision={revision} previewRevision={previewRevision}
        selections={selections} disabled={disabled} selectedPlane={selectedPlane} sections={sections} onOpenStudy={onOpenStudy}
        values={drafts[draftKey] || {}} setValues={change=>setDrafts(prev=>({...prev,[draftKey]:change(prev[draftKey] || {})}))}
        choice={alternatives[draftKey] || ''} setChoice={v=>setAlternatives(prev=>({...prev,[draftKey]:v}))}
        bindings={bindings[draftKey] || empty} setBindings={change=>setBindings(prev=>({...prev,[draftKey]:change(prev[draftKey] || empty)}))}/>:<p role="status">正在读取计算方法…</p>}</>}
  </details>;
}
