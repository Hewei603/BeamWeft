import React, {useEffect, useRef, useState} from 'react';
import {workbenchApi} from '../api/workbench';

export default function MeasuredLaunch({caseId, fitId, current, disabled, dirty, onBind, onClose}) {
  const [fits, setFits] = useState(null), [retry, setRetry] = useState(0);
  const singlePass = current?.project.system.mode === 'single_pass';
  const [fit, setFit] = useState(fitId ? String(fitId) : '');
  const [path, setPath] = useState(!fitId && singlePass ? 'primary' : '');
  const [first, setFirst] = useState(''), [last, setLast] = useState('');
  const [axis, setAxis] = useState(''), [position, setPosition] = useState(''), [note, setNote] = useState('');
  const [normalExit, setNormalExit] = useState(false);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const panel = useRef(null);
  useEffect(() => { panel.current?.scrollIntoView({block:'nearest'}); panel.current?.focus({preventScroll:true}); }, []);
  useEffect(() => {
    let active=true;
    workbenchApi.readBeamStudies(caseId).then(items=>{
      if(active) {setFits(items.filter(item=>item.type==='beam_caustic_study'));setError('');}
    }).catch(e=>{if(active)setError(e.message);});
    return ()=>{active=false;};
  },[caseId,retry]);
  const elements=current?.project.system.elements || [], paths=current?.project.incident_paths || [];
  const entry=first || elements[0]?.uid, exit=last || elements.at(-1)?.uid;
  const selected=fits?.find(item=>item.id===Number(fit));
  const inMaterial = selected?.medium_n != null && selected.medium_n !== 1;
  async function submit(event) {
    event.preventDefault();setBusy(true);setError('');
    try {
      await onBind({revision_id:current.revision_id,study_id:Number(fit),axis,source_z_mm:Number(position),
        registration:note.trim(),...(inMaterial && normalExit ? {normal_exit_to_air:true} : {}),
        ...(path ? {path_uid:path} : {entry_uid:entry,exit_uid:exit})});
    } catch(cause) {setError(cause.message);} finally {setBusy(false);}
  }
  return <section className="measured-launch" ref={panel} tabIndex={-1} aria-label="用实测拟合建立入射光束">
    <header><h3>用实测拟合建立入射光束</h3><button onClick={onClose} disabled={busy}>关闭</button></header>
    {!elements.length || dirty ? <p>先在画布上保存要使用的顺序光路，再把实测光束接到这个版本。</p> :
      fits && !fits.length ? <p>在实验资料中选择一组轴向位置和光斑宽度，保存拟合后即可带入光路。</p> :
      <form onSubmit={submit}><fieldset disabled={busy || disabled || !fits}>
        <label>使用的光斑拟合<select required value={fit} onChange={e=>{setFit(e.target.value);setNormalExit(false);}}>
          <option value="">选择拟合</option>{fits?.map(item=><option key={item.id} value={item.id}>#{item.id} · {item.conditions} · {item.axis} 向</option>)}</select></label>
        <label>接入哪束光<select value={path} onChange={e=>setPath(e.target.value)}><option value="">新增一束沿工程元件传播的光</option>
          {singlePass && <option value="primary">更新主光束的入射轴</option>}
          {paths.map(p=><option key={p.uid} value={p.uid}>{p.name}</option>)}</select></label>
        {!path && <><label>首个经过的元件<select value={entry} onChange={e=>{setFirst(e.target.value);setLast('');}}>
          {elements.map(el=><option key={el.uid} value={el.uid}>{el.name}</option>)}</select></label>
          <label>最后经过的元件<select value={exit} onChange={e=>setLast(e.target.value)}>
            {elements.slice(elements.findIndex(el=>el.uid===entry)).map(el=><option key={el.uid} value={el.uid}>{el.name}</option>)}</select></label></>}
        <label>实测{selected ? ' '+selected.axis+' 向' : '方向'}对应的模型轴<select required aria-label="对应的模型轴" value={axis} onChange={e=>setAxis(e.target.value)}>
          <option value="">选择方向</option><option value="tangential">T 切向</option><option value="sagittal">S 弧矢</option></select></label>
        <label>源面在原测量坐标中的位置 / mm<input required type="number" step="any" value={position} onChange={e=>setPosition(e.target.value)}/></label>
        {inMaterial && <label><input type="checkbox" required checked={normalExit} onChange={e=>setNormalExit(e.target.checked)}/>
          此坐标是 n = {selected.medium_n} 到空气的垂轴平面界面。测量在介质内侧，工程入射面在空气侧。</label>}
        <label>坐标和工况如何对应<input required maxLength={800} value={note} onChange={e=>setNote(e.target.value)}
          placeholder="说明同一工况、轴向正方向、横轴及源面前的连续介质段"/></label>
        <button className="btn btn-primary" disabled={!selected || !note.trim()}>建立试算并查看光路</button>
      </fieldset></form>}
    {error && <p role="alert">{error} {!fits && <button onClick={()=>setRetry(n=>n+1)}>重新读取拟合</button>}</p>}
  </section>;
}
