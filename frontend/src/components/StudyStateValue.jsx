import React, {useEffect, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import {FitOriginRecord} from './BeamStudy';
import {DeferredDetails, JsonDetails, JsonText} from './DeferredDetails';

const reading = value => Number.isFinite(value) ? String(Number(value.toPrecision(8))) : '未知';

function BeamAtPlane({state, legacy}) {
  const axes = [state.tangential, state.sagittal];
  const ray = state.launch_ray;
  const rayValue = (key, axis) => {
    const field = (axis === 0 ? 't_' : 's_') + key;
    return ray == null || !Object.hasOwn(ray,field) ? 0 : ray[field];
  };
  const wavefront = axis => axis?.wavefront === 'waist' ? '平面（束腰）'
    : axis?.wavefront === 'curved' ? reading(axis.curvature_mm) : '未知';
  const width = (axis,index) => {
    const basis = state.width_basis?.[index === 0 ? 'tangential' : 'sagittal'];
    if (!basis && axis?.m2 == null && axis?.radius_mm == null && !axis?.fit_origin) return '未知';
    return basis === 'second_moment' || axis?.second_moment_radius ? '二阶矩 2σ'
      : basis === 'equivalent' || axis?.equivalent_radius ? '等效半径' : axis?.m2 === 1 && !axis.fit_origin ? '高斯 1/e²' : '等效半径';
  };
  const rows = legacy ? [
    ['q 实部 / mm', ...axes.map(axis => reading(axis?.real_mm))],
    ['q 虚部 / mm', ...axes.map(axis => reading(axis?.imag_mm))],
    ['M²', reading(state.beam_quality_m2), reading(state.beam_quality_m2)],
  ] : [
    ['光束半径 / mm', ...axes.map(axis => reading(axis?.radius_mm))],
    ['半径口径', ...axes.map(width)],
    ['波前 R / mm', ...axes.map(wavefront)],
    ['M²', ...axes.map(axis => reading(axis?.m2))],
    ['束心偏移 / mm', ...axes.map((_, i) => reading(rayValue('offset_mm', i)))],
    ['指向 / mrad', ...axes.map((_, i) => reading(rayValue('angle_mrad', i)))],
  ];
  return <>
    <table><caption>{state.medium_n == null ? '空气参考面' : '介质参考面 · n = '+reading(state.medium_n)} · 真空波长 {reading(state.wavelength_nm)} nm</caption>
      <thead><tr><th scope="col">物理量</th><th scope="col">切向 T</th><th scope="col">弧矢 S</th></tr></thead>
      <tbody>{rows.map(([label, t, s]) => <tr key={label}><th scope="row">{label}</th>
        {[t,s].map((value,index) => <td key={index}>{Number.isFinite(Number(value)) ? <span className="state-number">{value}</span> : value}</td>)}
      </tr>)}</tbody>
    </table>
    {state.medium_n != null && <details><summary>折射率与矩阵坐标的换算</summary>
      <p>上表是当地介质内的物理读数。矩阵传播使用折合指向 nθ 和折合波前曲度 n/R。</p>
      {axes.map((axis,i)=>{
        const angle=rayValue('angle_mrad',i);
        const vergence=axis?.wavefront==='waist' ? 0 : axis?.wavefront==='curved' && axis.curvature_mm ? state.medium_n/axis.curvature_mm : null;
        return <p key={i}>{i===0?'T':'S'} · nθ = {reading(angle==null?null:state.medium_n*angle)} mrad · n/R = {reading(vergence)} mm⁻¹</p>;
      })}
    </details>}
    {!!Object.keys(state.origins || {}).length && <p>
      {Object.keys(state.origins).map(axis => axis === 'tangential' ? 'T' : 'S').join('、')} 轴保留了更早参考面的传播约束。
    </p>}
    {state.model_warnings?.map(note => <p key={note}>{note}</p>)}
    {!legacy && axes.map((axis, i) => axis?.fit_origin && <FitOriginRecord key={i} prefix={i === 0 ? 'T ' : 'S '} origin={axis.fit_origin}/>)}
  </>;
}

function CoherentFieldAtPlane({state}) {
  const {plane, operations} = state;
  const stops = operations.filter(element => element.clear_aperture).length;
  const rows = [
    ['参考面', plane.name || plane.plane_uid || '所选光路末端'],
    ['所属光路', plane.beam_uid === 'primary' ? '单程主光路' : plane.beam_uid],
    ['此段参考面位置 z', reading(plane.z_mm) + ' mm'],
    ['自光源起累计传播', reading(state.distance_mm) + ' mm'],
    ['已通过的有限孔径', stops + ' 处'],
  ];
  return <>
    <table><caption>相干复振幅 · 真空波长 {reading(state.wavelength_nm)} nm</caption>
      <tbody>{rows.map(([label, value]) => <tr key={label}><th scope="row">{label}</th><td>{value}</td></tr>)}</tbody>
    </table>
    <p>续算按新视场和采样从原始光源重算，保留孔径截光和横向相位。各段通过功率均以最初入射总功率为 1。</p>
    <p>请将此面与下一段入口重合，对齐展开光轴、横向原点和 T/S 方向。</p>
    {state.model_notes?.map((note, index) => <p key={index}>{note}</p>)}
  </>;
}

function OrderedLayout({state}) {
  const {system}=state, elements=system.elements;
  const modes={single_pass:'单程光路',standing_wave:'驻波腔',ring:'环形腔'};
  return <table><caption>{modes[system.mode] || '工程光路'} · {reading(system.wavelength_nm)} nm</caption>
    <tbody><tr><th scope="row">顺序元件</th><td>{elements.length} 件</td></tr>
      {!!elements.length && <tr><th scope="row">首末元件</th><td>{elements[0].name}
        {elements.length>1 && ' → '+elements.at(-1).name}</td></tr>}
      {!!state.observation_planes?.length && <tr><th scope="row">观察面</th>
        <td>{state.observation_planes.map(p=>p.name).join('、')}</td></tr>}
      {!!state.incident_paths?.length && <tr><th scope="row">入射支路</th><td>{state.incident_paths.length} 条</td></tr>}
    </tbody></table>;
}

function IntensityMap({state}) {
  const {x_edges_mm:x,y_edges_mm:y}=state;
  const interval=edges=>reading(edges[0])+' 至 '+reading(edges.at(-1))+' mm';
  const rows=[['网格（列 × 行）',(x.length-1)+' × '+(y.length-1)],
    [state.x_label,interval(x)],[state.y_label,interval(y)+(state.y_down?'，图中向下增加':'，图中向上增加')],
    ['单元积分单位',state.signal_unit]];
  if(state.plane)rows.push(['参考面',state.plane]);
  if(state.wavelength_nm!=null)rows.push(['真空波长',reading(state.wavelength_nm)+' nm']);
  return <><table><caption>横向分布的坐标与标定</caption><tbody>{rows.map(([term,value],i)=>
    <tr key={i}><th scope="row">{term}</th><td>{value}</td></tr>)}</tbody></table>
    <p>{state.basis}</p>{state.warnings?.map((message,i)=><p className="study-result-warning" key={i}>{message}</p>)}</>;
}

export default function StudyStateValue({title, port, value, caseId, source, onCopy}) {
  const [opened, setOpened] = useState(false);
  const [read, setRead] = useState(null);
  const [attempt, retry] = useState(0);
  const run = source?.run_id, revision = source?.revision_id, path = source?.port;
  useEffect(() => {
    if (!opened || value !== undefined || !path) return;
    let active = true;
    const controller = new AbortController();
    const request = run ? workbenchApi.studyOutput(caseId, run, path, controller.signal).then(record => record.value)
      : workbenchApi.projectOutputs(caseId, revision, path, controller.signal).then(record => record.outputs[path]?.value);
    request.then(result => {
      if (active) setRead(result == null ? {error:'来源中缺少所选状态。'} : {value:result});
    }).catch(error => {if (active) setRead({error:error.message});});
    return () => {active = false;controller.abort();};
  }, [opened, value, caseId, run, revision, path, attempt]);
  const state = value ?? read?.value;
  const legacy = port?.quantity === 'optics.centered-paraxial-field.ts-air.v1' && state?.frame === 'air.aligned-forward-ts.v1';
  const beam = (port?.quantity === 'optics.paraxial-beam.ts-air.v2' && state?.frame === 'air.aligned-forward-ts.v2')
    || (port?.quantity === 'optics.paraxial-beam.ts-medium.v3' && state?.frame === 'medium.aligned-forward-ts.v3');
  const coherent = port?.quantity === 'optics.coherent-fresnel-field.v1' && state?.schema_version === 1
    && state.plane && Array.isArray(state.operations);
  const layout = port?.quantity === 'optics.ordered-layout.v1' && Array.isArray(state?.system?.elements);
  const intensityMap = port?.quantity === 'optics.transverse-intensity-map.v1' && Array.isArray(state?.x_edges_mm) && Array.isArray(state?.y_edges_mm);
  return <DeferredDetails title={title} className="study-state-value" onOpen={()=>setOpened(true)}>
    {state != null ? <>
      {(legacy || beam || coherent || layout || intensityMap) ? <>
        {intensityMap ? <IntensityMap state={state}/> : layout ? <OrderedLayout state={state}/> : coherent ? <CoherentFieldAtPlane state={state}/> : <BeamAtPlane state={state} legacy={legacy}/>}
        <JsonDetails title="完整状态数据" value={state}/>
      </> : <JsonText value={state}/>}
      {onCopy && <button type="button" className="btn btn-secondary" onClick={() => onCopy(state)}>复制为手填值并解除引用</button>}
    </> : read?.error ? <p role="alert">{read.error} <button type="button" onClick={() => {setRead(null);retry(n => n+1);}}>重读状态</button></p>
      : <p role="status">{source ? '正在读取来源…' : '来源中缺少所选状态。'}</p>}
  </DeferredDetails>;
}
