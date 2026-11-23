import React, {useEffect, useRef, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import {compatiblePorts, fitsPort, sourceKey} from './studyConnections';
import TransverseCenterResult from './TransverseCenterResult';

const method='compare_transverse_centers';
const manualFields=[
  ['offset_x_mm','相机原点在 T 轴的位置 / mm'],
  ['offset_y_mm','相机原点在 S 轴的位置 / mm'],
  ['rotation_deg','相机横轴相对 T 轴转角 / °'],
];

function mergeRuns(...lists) {
  const records=new Map(lists.flat().map(record=>[record.id,record]));
  return [...records.values()].sort((a,b)=>b.id-a.id);
}

function useRunPages(caseId, query, refresh) {
  const [page,setPage]=useState(null),[more,setMore]=useState(false),[error,setError]=useState('');
  const queryText=JSON.stringify(query);
  useEffect(()=>{
    const controller=new AbortController();
    workbenchApi.studyRuns(caseId,null,JSON.parse(queryText),controller.signal)
      .then(data=>{if(!controller.signal.aborted){
        setPage(prev=>({...data,caseId,queryText,items:mergeRuns(prev?.queryText===queryText && prev.caseId===caseId?prev.items:[],data.items)}));
        setError('');
      }})
      .catch(e=>{if(!controller.signal.aborted)setError(e.message);});
    return ()=>controller.abort();
  },[caseId,queryText,refresh]);
  async function older() {
    if(more || !page?.next_before_id)return;
    setMore(true);
    try {
      const data=await workbenchApi.studyRuns(caseId,page.next_before_id,query);
      setPage(prev=>prev===page?{...prev,...data,items:mergeRuns(prev.items,data.items)}:prev);setError('');
    } catch(e){setError(e.message);} finally{setMore(false);}
  }
  return {items:page?.items || [],ready:!!page,more:page?.next_before_id,busy:more,error,older};
}

function ResultRecord({caseId,id,onOpenStudy,files,onOpenFile,focus}) {
  const [reply,setReply]=useState(null),[attempt,retry]=useState(0);
  const heading=useRef(null);
  useEffect(()=>{
    const controller=new AbortController();
    workbenchApi.studyRun(caseId,id,controller.signal)
      .then(record=>{if(!controller.signal.aborted)setReply({record});})
      .catch(e=>{if(!controller.signal.aborted)setReply({error:e.message});});
    return ()=>controller.abort();
  },[caseId,id,attempt]);
  useEffect(()=>{if(focus && reply?.record)heading.current?.focus();},[focus,reply]);
  if(!reply)return <p role="status">正在读取比较 #{id}…</p>;
  if(reply.error)return <p role="alert">{reply.error} <button type="button" onClick={()=>retry(n=>n+1)}>重读比较</button></p>;
  const {output,connections,inputs}=reply.record;
  const measured=connections?.observed?.run_id, calibration=connections?.registration?.run_id;
  const original=measured && files?.find(file=>file.id===inputs?.observed?.attachment_id);
  return <TransverseCenterResult output={output} headingRef={heading}><div className="center-links">
    <button type="button" onClick={()=>onOpenStudy(id)}>打开比较记录 #{id}</button>
    {measured && <button type="button" onClick={()=>onOpenStudy(measured)}>原始质心分析 #{measured}</button>}
    {original && <button type="button" onClick={()=>onOpenFile(original.id)}>查看原图 · {original.relative_path || original.filename}</button>}
    {calibration && <button type="button" onClick={()=>onOpenStudy(calibration)}>坐标标定 #{calibration}</button>}
  </div><p>预测来自工程 #{connections?.reference?.revision_id} · {output.identity?.plane_uid} · {output.identity?.beam_uid}
    {Number.isFinite(output.identity?.z_mm) && ' · z = '+output.identity.z_mm+' mm'}</p>
    <p>{measured?'测量来源 #'+measured:'本次记录直接提供了测量坐标'}{inputs?.observed?.frame && ' · 第 '+inputs.observed.frame+' 帧'}
      {output.observed_wavelength_nm != null && ' · '+output.observed_wavelength_nm+' nm'}</p>
    {output.observed_basis && <details><summary>采集坐标与物方尺度</summary><p>{output.observed_basis}</p></details>}
    {connections?.observed?.registration && <p>采集对应依据 · {connections.observed.registration}</p>}
  </TransverseCenterResult>;
}

function SavedComparisons({caseId,revision,port,recent,refresh,onOpenStudy,files,onOpenFile}) {
  const [chosen,setChosen]=useState(null);
  const history=useRunPages(caseId,{name:method,input_key:'reference',revision_id:revision,port},refresh);
  const items=recent && !history.items.some(row=>row.id===recent)?[{id:recent},...history.items]:history.items;
  const selected=chosen ?? recent ?? items[0]?.id;
  return <div className="center-history">
    {items.length>0 && <label>此光束的已存比较<select aria-label="此光束的已存比较" value={selected ?? ''}
      onChange={e=>setChosen(Number(e.target.value))}>{items.map(row=><option key={row.id} value={row.id}>
        {'比较 #'+row.id}</option>)}</select></label>}
    {history.more && <button type="button" disabled={history.busy} onClick={history.older}>读取更早的比较</button>}
    {history.error && <p role="alert">{history.error}</p>}
    {selected && <ResultRecord key={selected} caseId={caseId} id={selected} onOpenStudy={onOpenStudy} files={files} onOpenFile={onOpenFile} focus={selected===recent}/>}
  </div>;
}

function SourceOptions({options}) {
  return options.map(item=><option key={sourceKey(item)} value={sourceKey(item)}>{item.label}</option>);
}

function StationForm({caseId,revision,definition,station,sources,onMethod,onOpenStudy,files,onOpenFile,refresh,onRefresh}) {
  const observedOptions=compatiblePorts(definition.input_ports.observed,sources.items);
  const calibrationOptions=compatiblePorts(definition.input_ports.registration,sources.items);
  const [observed,setObserved]=useState(''),[calibration,setCalibration]=useState('manual');
  const [manual,setManual]=useState({}),[basis,setBasis]=useState('');
  const [pending,setPending]=useState(false),[error,setError]=useState(''),[recent,setRecent]=useState(null);
  const measured=observedOptions.find(option=>sourceKey(option)===observed);
  const fitted=calibrationOptions.find(option=>sourceKey(option)===calibration);
  function manualValue(key,value){setManual(prev=>({...prev,[key]:value}));}
  async function compare(e){
    e.preventDefault();
    if(pending || !measured || (!fitted && calibration!=='manual'))return;
    setPending(true);setError('');
    try{
      const note=basis.trim();
      const connections={
        observed:{run_id:measured.run_id,port:measured.port,registration:note},
        reference:{revision_id:revision,port:station[0],registration:note},
      };
      const inputs={};
      if(fitted)connections.registration={run_id:fitted.run_id,port:fitted.port,registration:note};
      else inputs.registration={offset_x_mm:Number(manual.offset_x_mm),offset_y_mm:Number(manual.offset_y_mm),
        rotation_deg:Number(manual.rotation_deg),flip_y:manual.direction==='flip',basis:manual.basis.trim()};
      const result=await workbenchApi.runStudy(caseId,definition,inputs,connections);
      setRecent(result.study_record.id);onRefresh();
    }catch(e){setError(e.message);}finally{setPending(false);}
  }
  return <>
    <SavedComparisons key={recent || 'saved'} caseId={caseId} revision={revision} port={station[0]} recent={recent} refresh={refresh}
      onOpenStudy={onOpenStudy} files={files} onOpenFile={onOpenFile}/>
    <form onSubmit={compare} className="center-form"><fieldset disabled={pending}>
      <label>实测质心来源<select aria-label="实测质心来源" required value={observed} onChange={e=>setObserved(e.target.value)}>
        <option value="">选择已保存的质心测量</option><SourceOptions options={observedOptions}/></select></label>
      <div className="center-links">{measured && <button type="button" onClick={()=>onOpenStudy(measured.run_id)}>核对所选测量 #{measured.run_id}</button>}
        <button type="button" onClick={()=>onMethod('measure_image_moments')}>从相机原件计算质心</button></div>
      {sources.ready && !observedOptions.length && <p>已载入的记录中没有物方毫米质心。选择相机原图，填写两轴物方像素尺度并注明线性响应与背景依据，即可保存测量。</p>}
      <label>横向坐标标定<select aria-label="横向坐标标定" required value={calibration} onChange={e=>setCalibration(e.target.value)}>
        <option value="manual">输入已知坐标变换</option><SourceOptions options={calibrationOptions}/></select></label>
      <div className="center-links">{fitted && <button type="button" onClick={()=>onOpenStudy(fitted.run_id)}>核对所选标定 #{fitted.run_id}</button>}
        <button type="button" onClick={()=>onMethod('fit_transverse_registration')}>用标记点拟合坐标标定</button></div>
      {calibration==='manual' && <div className="center-transform">{manualFields.map(([key,label])=><label key={key}>{label}
        <input aria-label={label} required type="number" step="any" value={manual[key] ?? ''} onChange={e=>manualValue(key,e.target.value)}/></label>)}
        <label>旋转前的纵轴方向<select aria-label="旋转前的纵轴方向" required value={manual.direction || ''} onChange={e=>manualValue('direction',e.target.value)}>
          <option value="">请选择方向</option><option value="keep">保留纵轴方向</option><option value="flip">翻转纵轴方向</option></select></label>
        <p>输入物方毫米。先确定相机纵轴方向，再从 T 朝 S 的正向旋转，最后平移原点。</p>
        <label>坐标标定依据<textarea aria-label="坐标标定依据" required maxLength={2000} value={manual.basis || ''} onChange={e=>manualValue('basis',e.target.value)}/></label></div>}
      <label>采集与此观察面的对应依据<textarea aria-label="采集与此观察面的对应依据" required maxLength={500}
        placeholder="记录相机对应的物方面、所测光束，以及本次采集采用的标定。"
        value={basis} onChange={e=>setBasis(e.target.value)}/></label>
      <button type="submit" className="btn btn-primary" disabled={!measured || !basis.trim() || (calibration==='manual' && !manual.basis?.trim())}>
        {pending?'正在比较…':'比较并保存束心'}</button>
    </fieldset></form>
    {error && <p role="alert">{error}</p>}
  </>;
}

function StationContent({caseId,revision,plane,initialBeam,onMethod,onOpenStudy,files,onOpenFile,refreshToken}) {
  const [reply,setReply]=useState(null),[refresh,reload]=useState(0),[beam,setBeam]=useState(initialBeam);
  const sources=useRunPages(caseId,{},refresh+':'+refreshToken);
  useEffect(()=>{
    const controller=new AbortController(),signal=controller.signal;
    Promise.all([workbenchApi.studyDefinition(caseId,method,signal),workbenchApi.projectOutputs(caseId,revision,null,signal)])
      .then(([definition,model])=>{if(!signal.aborted)setReply({definition,model});})
      .catch(e=>{if(!signal.aborted)setReply({error:e.message});});
    return ()=>controller.abort();
  },[caseId,revision,refresh]);
  const stations=reply?.model?Object.entries(reply.model.outputs).filter(([,item])=>
    item.plane_uid===plane.uid && fitsPort(reply.definition.input_ports.reference,item.port)):[];
  const station=stations.find(([,item])=>item.path_uid===beam) || stations[0];
  return <div className="center-content">
    <p>工程 #{revision} · {plane.name || plane.uid}</p>
    <div className="center-links"><button type="button" onClick={()=>reload(n=>n+1)}>刷新计算来源</button>
      {sources.more && <button type="button" disabled={sources.busy} onClick={sources.older}>读取更早的计算</button>}</div>
    {(reply?.error || sources.error) && <p role="alert">{reply?.error || sources.error}</p>}
    {!reply && <p role="status">正在读取此版本的观察面坐标…</p>}
    {reply?.model && !stations.length && <p>这个工程版本在此面没有已保存的束心坐标。请检查光束是否到达观察面以及输入条件，再计算并保存工程。</p>}
    {station && <><label>对照光束<select aria-label="对照光束" value={station[1].path_uid} onChange={e=>setBeam(e.target.value)}>
      {stations.map(([port,item])=><option key={port} value={item.path_uid}>{item.location} · {item.path_uid}</option>)}</select></label>
      <StationForm key={station[0]} caseId={caseId} revision={revision} definition={reply.definition} station={station}
        sources={sources} onMethod={onMethod} onOpenStudy={onOpenStudy} files={files} onOpenFile={onOpenFile}
        refresh={refresh+':'+refreshToken} onRefresh={()=>reload(n=>n+1)}/></>}
  </div>;
}

export default function StationCenterComparison(props) {
  const [visited,setVisited]=useState(false);
  return <details className="station-center" onToggle={e=>{if(e.target===e.currentTarget && e.currentTarget.open)setVisited(true);}}>
    <summary>对照实测束心</summary>
    {visited && (props.revision?<StationContent {...props}/>:<p>保存当前光路后，即可把相机测量与这个版本的预测关联。</p>)}
  </details>;
}
