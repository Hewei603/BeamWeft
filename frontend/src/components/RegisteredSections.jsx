import {BeamSourceFields, Num} from './BeamSourceFields';
import PredictionComparison from './PredictionComparison';
import BeamComparison from './BeamComparison';
import ModeOverlap from './ModeOverlap';
import React, { useEffect, useRef, useState } from 'react';
import { localId } from '../api/localId.js';
import {componentLabels} from './layoutSelections';
import { bindSharedRange, instantiateOptic, objectRange, observationControl, reorderedElements, withElementParameter, withPathElements } from './benchGeometry';
import OpticalElementFields from './OpticalElementFields';
import SectionPlot from './SectionPlot';
import SectionReadings from './SectionReadings';
import BeamStudy from './BeamStudy';
import PathPower from './PathPower';
import ElementRemoval from './ElementRemoval';
import {hasLockedParameters, portReferences} from './elementRemovalPlan.js';
import PowerMeasurements from './PowerMeasurements';
import {SharedIndexRow} from './SharedMediumIndices';
import {withIncidentWavelength} from './sharedMedium.js';
import {frozenIndexModel} from './inventoryTargets';
import './RegisteredSections.css';

const fmt = value => Number.isFinite(value) ? Number(value.toPrecision(5)) : '—';

const powerStage=(el,scope)=>({scope,element_uid:el.uid,name:el.name});

function PathEditor({catalog,draft,route,trace,materialReadings,change,remove,onEdit,elements,hostWavelength,openRequest,unlock}) {
  const { path, arm } = route;
  const usesIndex=el=>catalog.models[el?.kind]?.parameters.includes('n');
  const [kind,setKind]=useState('gap');
  const editor=useRef(null);
  const sharedFields=useRef(null);
  useEffect(()=>{
    if(openRequest?.uid!==path.uid)return;
    const root=editor.current;
    root.open=true;
    let target=root;
    if(openRequest.index!=null){
      const entry=root.querySelectorAll('.section-train > details')[openRequest.index];
      if(entry){entry.open=true;target=entry;}
    }
    if(openRequest.sharedUid){
      const row=Array.from(root.querySelectorAll('.shared-index-row')).find(item=>item.dataset.elementUid===openRequest.sharedUid);
      if(row){row.closest('details').open=true;target=row;}
    }
    target.scrollIntoView({block:'center'});
    target.querySelector('summary, input:not(:disabled), button')?.focus({preventScroll:true});
  },[openRequest,path.uid]);
  const edit=(k,v)=>change({...path,[k]:v});
  const downstream=path.downstream||[];
  const anchor=elements.findIndex(el=>el.uid===path.target_uid);
  const ends=anchor<0?[]:elements.slice(anchor).filter(el=>el.uid);
  const end=path.through_uid || downstream.at(-1)?.target_uid || path.target_uid;
  const sameWavelength = Number.isFinite(hostWavelength) && Number.isFinite(path.wavelength_nm)
    && Math.abs(hostWavelength - path.wavelength_nm) <= 1e-12 * Math.max(hostWavelength, path.wavelength_nm);
  const media = route.shared.filter(({element})=>usesIndex(element));
  const others = media.filter(({element})=>element.uid !== path.target_uid);
  const renderIndex = element=><SharedIndexRow key={element.uid} element={element} path={path} trace={trace}
    change={change} sameWavelength={sameWavelength}/>;
  const pendingIndependent = others.some(({element})=>path.independent_index_uids?.includes(element.uid)
    && !downstream.some(stage=>stage.target_uid===element.uid && Number.isFinite(stage.n)));
  useEffect(()=>{
    if(pendingIndependent)sharedFields.current.open=true;
  },[pendingIndependent,path.uid]);
  const update=(i,object)=>edit('elements',path.elements.map((e,j)=>j===i?object:e));
  const reorder=(i,offset)=>change(withPathElements(path,reorderedElements(arm,i,offset)));
  const pathLocked = !unlock && arm.parts.some(hasLockedParameters);
  const portUses = portReferences(draft,d => d.path_output?.path_uid === path.uid || d.profile?.beam_uid === path.uid);
  return <details ref={editor} className="section-model"><summary>调整入射光源与耦合光路</summary>
    <div className="section-fields"><label>支路名称<input value={path.name} onChange={e=>edit('name',e.target.value)}/></label>
      <label>支路接入段<select aria-label="支路接入段" value={path.target_uid} onChange={e=>change(bindSharedRange(path,elements,e.target.value,end))}>{anchor<0 && <option value={path.target_uid}>原目标缺失</option>}{elements.filter(e=>e.uid).map(el=><option key={el.uid} value={el.uid}>{el.name}</option>)}</select></label>
      <label>共享光路终点<select aria-label="共享光路终点" value={end} onChange={e=>change(bindSharedRange(path,elements,path.target_uid,e.target.value))}>{!ends.some(el=>el.uid===end) && <option value={end}>终点需要重新绑定</option>}{ends.map(el=><option key={el.uid} value={el.uid}>{el.name}</option>)}</select></label>
      <Num label="入射波长 / nm" value={path.wavelength_nm} change={value=>change(withIncidentWavelength(path,value))}/>
      <label>参数来源或假设<input value={path.source_note ?? ''} onChange={e=>edit('source_note',e.target.value)}/></label>
    </div>
    {media.some(({element})=>element.uid===path.target_uid) && renderIndex(elements[anchor])}
    {path.through_uid && media.length>0 && <label className="section-inline"><input type="checkbox" checked={path.inherit_index_at_same_wavelength ?? true}
      onChange={e=>edit('inherit_index_at_same_wavelength',e.target.checked)}/> 使用支路波长处的材料模型。同波长时，未填写的支路折射率沿用主路值</label>}
    <details key={path.uid} ref={sharedFields}><summary>共享元件与介质折射率</summary>
      <p>{route.shared.map(r=>r.element.name).join(' → ')}</p>
      {route.broken && <p role="alert">原路径已断开，请重新选择入口或终点。</p>}
      {!path.through_uid && !route.broken && media.length>0 && <p>此路径按段登记折射率。切换后，入口至终点间的元件随主光路更新，已填的支路值继续优先。
        <button type="button" onClick={()=>change(bindSharedRange(path,elements,path.target_uid,end))}>改为按端点共享</button></p>}
      {others.map(({element})=>renderIndex(element))}
    </details>
    <BeamSourceFields source={path} change={change}/>
    <p>下方元件从支路光源排到共享入口，随入口一起移动。需要计算绝对功率时，在功率收支中填写各元件的损耗。</p>
    <div className="section-train">{path.elements.map((el,i)=>{
      const {first,last,group}=objectRange(arm,i);
      const part=arm.parts.find(p=>p.element_index===i);
      const locks=unlock ? [] : part?.locked_parameters || [];
      return <details key={el.uid||i}><summary>{i+1} · {el.name}{el.length_mm?' · '+fmt(el.length_mm)+' mm':''}</summary>
      <OpticalElementFields catalog={catalog} element={el} wavelengthNm={path.wavelength_nm} className="section-fields" locked={key=>locks.includes(key)}
        stockBound={part?.inventory_id!=null} frozenModel={part?.inventory_id!=null && frozenIndexModel(draft,path.uid,el.uid)}
        materialReading={materialReadings?.find(row=>row.path_uid===path.uid && row.element_uid===el.uid)}
        change={(key,value)=>update(i,withElementParameter(el,key,value,path.wavelength_nm))}/>
      {part && <p>{part.source}{part.inventory_id != null && ` · 库存 #${part.inventory_id}`}
        {!!locks.length && ' · 在工程约束中解锁后可更改实物参数。'}</p>}
      {el.component_origin && <p>模型来源 · {el.component_origin.name} v{el.component_origin.version}。{el.component_origin.note}</p>}
      <div className="section-actions"><button type="button" disabled={first===0} onClick={()=>reorder(i,-1)}>{group?'上移「'+group.name+'」':'上移'}</button><button type="button" disabled={last===path.elements.length-1} onClick={()=>reorder(i,1)}>{group?'下移「'+group.name+'」':'下移'}</button>
        <ElementRemoval project={draft} pathUid={path.uid} index={i} unlocked={unlock} onEdit={onEdit}/></div>
    </details>;})}</div>
    <div className="section-actions"><label>添加到支路末端<select value={kind} onChange={e=>setKind(e.target.value)}>{Object.entries(catalog.presets).map(([k,m])=><option key={k} value={k}>{m.name}</option>)}</select></label>
      <button type="button" className="btn btn-secondary" disabled={path.elements.length>=80} onClick={()=>edit('elements',[...path.elements,instantiateOptic(catalog.presets[kind])])}>添加支路元件</button>
      <details><summary>移除整条支路</summary>
        <p>将从草稿撤下「{path.name}」、它的 {path.elements.length} 个私有元件{draft.assemblies?.some(g=>g.path_uid===path.uid) && '及其组合'}{path.power_budget && '和本支路功率预算'}。共用元件属于主光路。</p>
        <button type="button" onClick={remove} disabled={pathLocked || portUses.length > 0}>移除此支路</button>
        {pathLocked && <small>这条支路含有锁定参数，请先在工程参数中解锁。</small>}
        {portUses.map((message,i)=><small key={i}>{message}</small>)}</details></div>
  </details>;
}

export default function RegisteredSections({catalog,draft, positions, routes, current, readWorkingDraft, selectedElement, meterSelection, result, caseId, files, disabled, unlock, dirty, onEdit,beginGesture,refreshToken,onAsk,onUseFit,onUseQuantity,onOpenStudy,chosen,setChosen,chosenPath,setChosenPath,onAddPath,openRequest,beamReadings}) {
  const readings=beamReadings.items;
  const planes=draft.observation_planes || [], paths=draft.incident_paths || [];
  const planeNames=componentLabels(planes);
  const segments=draft.system.elements.filter(e=>e.kind==='space' && e.uid);
  const plane=planes.find(p=>p.uid===chosen)||planes[0];
  const planePortUses=plane ? portReferences(draft,d=>d.profile?.plane_uid === plane.uid) : [];
  const model=result?.sections?.planes.find(p=>p.uid===plane?.uid);
  function beamLabel(beam) {
    const name=beam.uid==='primary' ? (draft.system.mode==='single_pass'?'主光束':'腔本征模') : paths.find(p=>p.uid===beam.uid)?.name;
    if (dirty || !current?.revision_id) return name;
    const use=(port,label)=>onUseQuantity({revision_id:current.revision_id,port,label,location:plane.name});
    return <>{beam.continuation_port ? <button type="button" className="bench-quantity" aria-label={`使用${plane.name}的${name}继续分析`}
      onClick={()=>use(beam.continuation_port,name)}>{name} ↗</button> : name}
      {beam.layout_prefix_port && <small><button type="button" className="bench-quantity" aria-label={`使用${name}光源至${plane.name}的光路计算`}
        onClick={()=>use(beam.layout_prefix_port,name+'光源至此面的光路')}>使用光源至此面的光路 ↗</button></small>}
    </>;
  }
  const fixed=plane?.position_reference==='fixed_z';
  const hostUid=fixed ? model?.target_uid : plane?.target_uid;
  // Keep the path editor mounted while a parameter is being typed. Only the
  // current calculation can supply a displayed result or authorize a new link.
  const resolvedPlanes=result?.sections?.planes;
  const [previousPlanes,rememberPlanes]=useState(resolvedPlanes);
  if(resolvedPlanes && resolvedPlanes!==previousPlanes)rememberPlanes(resolvedPlanes);
  const editingHost=p=>p?.target_uid ?? (resolvedPlanes??previousPlanes)?.find(s=>s.uid===p?.uid)?.target_uid;
  const targets=routes.filter(r=>r.shared.some(row=>row.element.uid===editingHost(plane))).map(r=>r.path);
  const path=chosenPath ? targets.find(p=>p.uid===chosenPath) : targets[0];
  const comparable = dirty ? [] : readings.filter(r=>r.revision_id===current?.revision_id && r.plane_uid===plane?.uid &&
    (r.beam_uid==='primary' || r.beam_uid===path?.uid) && Object.keys(r.measured_minus_model_mm).length);
  const newPlane=()=>{
    const target=segments.find(el=>el.uid===selectedElement?.uid)||segments[0];
    const item={uid:localId(),name:'观察面 '+(planes.length+1),target_uid:target.uid,depth_mm:0};
    onEdit({...draft,observation_planes:[...planes,item]});setChosen(item.uid);
  };
  const replacePlane=next=>onEdit({...draft,observation_planes:planes.map(p=>p.uid===plane.uid?next:p)});
  const updatePlane=(key,value)=>replacePlane({...plane,[key]:value});
  const switchReference=reference=>{
    const identity={uid:plane.uid,name:plane.name};
    if(reference==='fixed_z'){
      const length=segments.find(el=>el.uid===hostUid)?.length_mm;
      const side=plane.depth_mm===0?'downstream':plane.depth_mm===length?'upstream':null;
      const coordinate=observationControl(plane,positions)?.value;
      replacePlane({...identity,position_reference:reference,z_mm:coordinate??null,boundary_side:side});
    } else if(model?.target_uid && Number.isFinite(model.depth_mm)){
      replacePlane({...identity,target_uid:model.target_uid,depth_mm:model.depth_mm});
    }
  };
  const newPath=()=>onAddPath(hostUid,false);
  const alter=p=>onEdit({...draft,incident_paths:paths.map(item=>item.uid===p.uid?p:item)});
  const editingPath=paths.find(p=>p.uid===chosenPath) || path || paths[0];
  const budgets=result?.sections?.power_budgets||[];
  const pathControls=p=><React.Fragment key={p.uid}><fieldset disabled={disabled}><PathEditor catalog={catalog} draft={draft} route={routes.find(r=>r.path.uid===p.uid)} hostWavelength={draft.system.wavelength_nm} unlock={unlock}
    elements={draft.system.elements} trace={result?.sections?.paths?.find(t=>t.uid===p.uid)} materialReadings={result?.material_indices}
    openRequest={openRequest} change={alter} onEdit={onEdit} remove={()=>onEdit({...draft,incident_paths:paths.filter(item=>item.uid!==p.uid),assemblies:(draft.assemblies || []).filter(g=>g.path_uid!==p.uid)})}/></fieldset>
    <PathPower title={p.name+' · 功率收支'} budget={p.power_budget} wavelength={p.wavelength_nm} disabled={disabled}
      stages={[...p.elements.filter(e=>e.uid).map(e=>powerStage(e,'arm')),...routes.find(r=>r.path.uid===p.uid).shared.map(r=>powerStage(r.element,'host'))]}
      result={budgets.find(b=>b.path_uid===p.uid)} plane={plane} change={v=>alter({...p,power_budget:v,elements:p.elements.map(el=>el.uid?el:{...el,uid:localId()})})}/>
  </React.Fragment>;
  return <section className="registered-sections" aria-label="共面光束对照"><div className="section-heading"><div><h3>观察面</h3>
    {!plane && <p>在空气或均匀介质中选一个截面，对齐腔模、入射光束和实验读数。</p>}</div>
    <button className="btn btn-secondary" id={plane?undefined:'register-reading-'+caseId} disabled={disabled || !segments.length || planes.length>=48} onClick={newPlane}>添加观察面</button></div>
    {plane && <><fieldset disabled={disabled}><div className="section-fields"><label>当前观察面<select aria-label="当前观察面" value={plane.uid} onChange={e=>setChosen(e.target.value)}>{planes.map(p=><option key={p.uid} value={p.uid}>{planeNames.get(p.uid)}</option>)}</select></label>
      <label>观察面定位<select value={fixed?'fixed_z':'element_depth'} onChange={e=>switchReference(e.target.value)}>
        <option value="element_depth" disabled={fixed && !model?.target_uid}>随元件移动</option><option value="fixed_z">固定在主光路坐标</option></select></label>
      {fixed ? <>
        <Num label="固定坐标 z / mm" value={plane.z_mm} change={v=>updatePlane('z_mm',v)}/>
        <label>恰在界面时取样<select value={plane.boundary_side||''} onChange={e=>updatePlane('boundary_side',e.target.value||null)}>
          <option value="">有歧义时提示</option><option value="upstream">界面前</option><option value="downstream">界面后</option></select></label>
      </> : <><label>定位元件<select value={plane.target_uid} onChange={e=>updatePlane('target_uid',e.target.value)}>{!segments.some(s=>s.uid===plane.target_uid)&&<option value={plane.target_uid}>原目标缺失</option>}{segments.map(el=><option key={el.uid} value={el.uid}>{el.name}</option>)}</select></label>
      <Num label="从入射面起的深度 / mm" value={plane.depth_mm} change={v=>updatePlane('depth_mm',v)}/></>}
      {targets.length>0 && <label>显示入射支路<select value={path?.uid||''} onChange={e=>setChosenPath(e.target.value)}>
        {!path && <option value="" disabled>选择经过此面的支路</option>}{targets.map(p=><option key={p.uid} value={p.uid}>{p.name}</option>)}</select></label>}
      <button type="button" className="btn btn-secondary" disabled={paths.length>=12 || !segments.some(s=>s.uid===hostUid)} onClick={newPath}>接入光束</button></div>
    {fixed && <p className="bench-hint">坐标从主光路起点沿传播方向量取。{hostUid && <>当前落在「{segments.find(el=>el.uid===hostUid)?.name}」内，距入口 {fmt(model.depth_mm)} mm。</>}</p>}
    <details><summary>观察面名称与移除</summary><label>名称<input value={plane.name} onChange={e=>updatePlane('name',e.target.value)}/></label>
      <button disabled={planePortUses.length>0} onClick={()=>onEdit({...draft,observation_planes:planes.filter(p=>p.uid!==plane.uid)})}>移除观察面</button>
      {planePortUses.map((message,i)=><small key={i}>{message}</small>)}</details></fieldset>
    <SectionPlot draft={draft} result={result} host={positions} route={routes.find(r=>r.path.uid===path?.uid)} section={model} readings={comparable} disabled={disabled} beginGesture={beginGesture}/>
    <div className="section-legend"><span>主光束</span>{path && <span>{path.name}</span>}<small>实线 T · 虚线 S · 十字标示该截面的计算值{comparable.length>0 && ' · 圆圈为实测 T，方框为实测 S'}</small></div>
    {model?.message && <p role="status">{model.message}</p>}
    {model?.beams?.length>0 && <table><caption>{plane.name} · z = {fmt(model.z_mm)} mm</caption><thead><tr><th>光束</th><th>波长 / nm</th><th>T 半径 / μm</th><th>S 半径 / μm</th></tr></thead><tbody>{model.beams.map(b=>
      <tr key={b.uid}><th scope="row">{beamLabel(b)}</th><td>{fmt(b.wavelength_nm)}</td>
        {['tangential','sagittal'].map(axis=><td key={axis}>{b.notes[axis] || <>{Number.isFinite(b.radii_mm[axis]) ? fmt(b.radii_mm[axis]*1000) : '—'}{b.width_basis?.[axis]==='second_moment'?' · 2σ':b.width_basis?.[axis]==='equivalent'?' · 等效':''}{b.radius_origin?.[axis]==='source_input' && ' · 光源输入'}{b.radius_origin?.[axis]==='conjugate' && ' · 由成像倍率确定'}</>}
          {b.launch_basis === 'specified' && <small className="section-beam-center">{Number.isFinite(b.field_axes?.[axis]?.center_mm)
            ? '束心 '+fmt(b.field_axes[axis].center_mm)+' mm' : '束心未确定'}</small>}
          {b.field_axes?.[axis]?.transfer_warning && <small>{b.field_axes[axis].transfer_warning}</small>}</td>)}</tr>)}</tbody></table>}
    <ModeOverlap caseId={caseId} draft={draft} revision={current?.revision_id} dirty={dirty} plane={plane} paths={targets} selectedPath={path?.uid} onOpenStudy={onOpenStudy} disabled={disabled}/>
    <SectionReadings key={(current?.revision_id||0)+'-'+plane.uid} caseId={caseId} revision={current?.revision_id} plane={plane} model={model} files={files} dirty={dirty} paths={paths} readings={beamReadings}/>
    {!dirty && readings.some(r=>r.revision_id===current?.revision_id) && <BeamStudy key={current.revision_id}
      caseId={caseId} readings={readings} revision={current.revision_id} disabled={disabled} refreshToken={refreshToken} onUseFit={onUseFit}/>}
    </>}
    {editingPath && <div aria-label={'支路建模 '+editingPath.name}><h4>{editingPath.name}</h4>{pathControls(editingPath)}</div>}
    {(draft.system.mode==='single_pass' || draft.single_pass_power) && <PathPower title="主光路 · 单程功率收支" budget={draft.single_pass_power}
      wavelength={draft.system.wavelength_nm} disabled={disabled} stages={draft.system.elements.map(e=>powerStage(e,'host'))}
      result={budgets.find(b=>b.path_uid==='primary')} plane={plane} change={v=>onEdit({...draft,single_pass_power:v})}/>}
    {(current?.revision_id || dirty) && <PredictionComparison key={'predictions-'+(current?.revision_id || 0)} caseId={caseId}
      revision={current?.revision_id} project={draft} onAsk={onAsk} disabled={disabled} refreshToken={refreshToken}
      readWorkingDraft={readWorkingDraft}/>}
    {current?.revision_id && !dirty && <BeamComparison key={caseId+'-compare-'+current.revision_id}
      caseId={caseId} revision={current.revision_id} paths={paths} onAsk={onAsk} disabled={disabled} refreshToken={refreshToken + ':' + readings.length}/>}
    <PowerMeasurements key={caseId+'-'+(current?.revision_id||0)} caseId={caseId} revision={current?.revision_id}
      files={files} selected={meterSelection} refreshToken={refreshToken} readOnly={dirty||disabled}/>
  </section>;
}
