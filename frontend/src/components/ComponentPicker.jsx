import React, {useEffect, useRef, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import ComponentEditor, {crystalDescription} from './ComponentEditor';
import {engineeringObjects,findStockObject} from './inventoryTargets';
import {radiusBasisLabels} from './beamWidths';

const PAGE_SIZE=30;
const fmt=n=>Number.isFinite(n) ? String(Number(n.toPrecision(6))) : '待确认';
const locks={kind:'模型类型',length_mm:'长度',n:'折射率',focal_mm:'焦距',radius_mm:'曲率半径',aoi_deg:'入射角'};
const ownership={owned:'已有实物',proposed:'拟采购',unknown:'待核实'};
const quantity=item=>item.available_qty ?? item.available_quantity ?? item.quantity;

function stockDescription(item) {
  if(item.category==='lens')return Number.isFinite(item.focal_length_mm)?'f = '+fmt(item.focal_length_mm)+' mm':'焦距待确认';
  if(item.category==='mirror')return item.roc_is_flat?'平面':Number.isFinite(item.roc_mm)?'R = '+fmt(item.roc_mm)+' mm':'曲率待确认';
  return crystalDescription(item);
}

function prescription(target) {
  const first=target.entries[0].element;
  if(target.category==='lens')return 'f = '+fmt(first.focal_mm)+' mm';
  if(target.category==='mirror')return first.radius_mm==null?'平面':'R = '+fmt(first.radius_mm)+' mm';
  return target.entries.filter(e=>e.element.length_mm>0).map(({element:e},i)=>{
    const ref=e.index_reference;
    const indexSource=ref?.wavelength_nm!=null?'（'+fmt(ref.wavelength_nm)+' nm'+(ref.model?' · '+ref.model:'')+'）':'';
    const prefix=target.entries.length>1?'第'+(i+1)+'段 ':'';
    return prefix+fmt(e.length_mm)+' mm · n '+fmt(e.n)+indexSource;
  }).join(' → ');
}

function binding(target) {
  const owned=[...new Set(target.entries.map(e=>ownership[e.part?.ownership] || e.part?.ownership || '未登记归属'))].join('、');
  return owned+(target.inventoryIds.length?' · '+target.inventoryIds.map(id=>'库存 #'+id).join('、'):'');
}

function EndpointReadings({data,current}) {
  const cavity=data.project.system.mode!=='single_pass', uid=data.replacement.target_path_uid;
  const axes=['tangential','sagittal'];
  function stability(model,axis) {
    const p=model.analysis?.planes?.[axis];
    const state=p?.marginal?'临界':p?.stable===true?'稳定':p?.stable===false?'失稳':'待定';
    return Number.isFinite(p?.m)?state+' · '+fmt(p.m):state;
  }
  function radius(model,axis,branch) {
    const plane=model.analysis?.planes?.[axis];
    const path=branch && model.project.incident_paths?.find(p=>p.uid===uid);
    const ray=path && ((axis==='sagittal' && path.sagittal) || path.tangential);
    const trace=path && model.analysis?.sections?.paths?.find(p=>p.uid===uid);
    const mm=branch ? trace?.axes?.[axis]?.exit_radii_mm?.at(-1) : plane?.output?.radius_mm;
    if(!Number.isFinite(mm))return '待定';
    const basis=branch ? ray?.second_moment_radius?'second_moment':ray?.equivalent_radius || ray?.m2!==1 || ray?.fit_origin?'equivalent':'gaussian' : plane.width_basis;
    return fmt(mm*1000)+' μm · '+(radiusBasisLabels[basis] || '约定待核对');
  }
  const rows=[];
  if(cavity)rows.push(['冷腔稳定性 (A+D)/2',stability]);
  else rows.push(['主光路末端近轴半径',(model,axis)=>radius(model,axis,false)]);
  if(uid && uid!=='primary')rows.push(['支路接入前近轴半径',(model,axis)=>radius(model,axis,true)]);
  return <div className="component-prediction"><h5>{cavity?'谐振腔试算':'末端试算'}</h5>
    <table><caption>当前 → 替换后</caption><thead><tr><th>计算量</th><th>T</th><th>S</th></tr></thead>
      <tbody>{rows.map(([name,value])=><tr key={name}><th scope="row">{name}</th>{axes.map(axis=><td key={axis}>
        <span className="stock-optical-value">{value(current,axis)}</span> → <span className="stock-optical-value">{value(data,axis)}</span></td>)}</tr>)}</tbody></table></div>;
}

function PreviewReadings({data,current}) {
  const [choice,setChoice]=useState('');
  const stations=data.analysis?.sections?.planes || [];
  const station=stations.find(s=>s.uid===choice) || stations[0];
  const project=data.project,primary=project.system.mode==='single_pass'?'主光束':'腔模';
  if(!station)return <EndpointReadings data={data} current={current}/>;
  const before=current.analysis?.sections?.planes?.find(s=>s.uid===station.uid);
  const name=id=>project.observation_planes?.find(s=>s.uid===id)?.name || id;
  const label=id=>id==='primary'?primary:project.incident_paths?.find(p=>p.uid===id)?.name || id;
  const reading=(b,axis)=>Number.isFinite(b?.radii_mm?.[axis]) ? fmt(b.radii_mm[axis]*1000) : '待定';
  const basis=(b,axis)=>Number.isFinite(b?.radii_mm?.[axis]) ? radiusBasisLabels[b.width_basis?.[axis]] || '约定待核对' : '无半径读数';
  return <div className="component-prediction"><h5>观察面试算</h5>
    {stations.length>1 ? <label>比较截面 <select value={station.uid} onChange={e=>setChoice(e.target.value)}>
      {stations.map(s=><option key={s.uid} value={s.uid}>{name(s.uid)}</option>)}</select></label> : <strong>{name(station.uid)}</strong>}
    <span> · z = {Number.isFinite(before?.z_mm) && before.z_mm!==station.z_mm && fmt(before.z_mm)+' → '}{fmt(station.z_mm)} mm</span>
    {station.beams?.length>0 && <table><caption>半径 / μm · 当前 → 替换后</caption><thead><tr><th>光束</th><th>T</th><th>S</th></tr></thead>
      <tbody>{station.beams.map(b=>{
        const previous=before?.beams?.find(p=>p.uid===b.uid);
        return <tr key={b.uid}><th scope="row">{label(b.uid)}<small>{fmt(b.wavelength_nm)} nm</small></th>
          {['tangential','sagittal'].map(axis=><td key={axis}>{reading(previous,axis)} → {reading(b,axis)}
            <small>{basis(previous,axis)!==basis(b,axis) && basis(previous,axis)+' → '}{basis(b,axis)}</small></td>)}</tr>;
      })}</tbody></table>}
    {station.message && <p>{station.message}</p>}
  </div>;
}

function StockPreview({data,target,item,current,next}) {
  if(!next)return <p role="alert">试算结果缺少目标元件，无法核对库存绑定。</p>;
  const locked=[...new Set(next.entries.flatMap(e=>e.part?.locked_parameters || []))];
  const {old_path_length_mm:before,new_path_length_mm:after,compensating_gap:gap,reference_notes:referenceNotes=[],
    old_entry_z_mm:oldEntry,new_entry_z_mm:newEntry,old_exit_z_mm:oldExit,new_exit_z_mm:newExit}=data.replacement;
  const origins=next.sources;
  const missing=[...new Set((data.analysis?.input_issues || []).filter(issue=>issue.owner===next.pathUid &&
    next.entries.some(({element})=>element.uid===issue.element_uid)).map(issue=>issue.message).filter(Boolean))];
  return <><dl>
    <dt>归属与绑定</dt><dd>{binding(target)} → <strong>{binding(next)}</strong></dd>
    <dt>模型参数</dt><dd>{prescription(target)} → <strong>{prescription(next)}</strong></dd>
    {['medium','gain_crystal'].includes(target.category) && [oldEntry,newEntry,oldExit,newExit].every(Number.isFinite) &&
      <><dt>沿本光路的位置</dt><dd>入射面 {fmt(oldEntry)} → <strong>{fmt(newEntry)} mm</strong><br/>
        出射面 {fmt(oldExit)} → <strong>{fmt(newExit)} mm</strong><br/><small>均从{target.pathName}起点量起</small></dd></>}
    {gap && <><dt>补偿空气间距</dt><dd>{gap.side==='after'?'后方':'前方'} · {gap.name} · {fmt(gap.old_length_mm)} → <strong>{fmt(gap.new_length_mm)} mm</strong></dd></>}
    {Number.isFinite(before) && Number.isFinite(after) && before!==after && <><dt>路径长度</dt><dd>{fmt(before)} → <strong>{fmt(after)} mm</strong></dd></>}
    <dt>实物参数锁</dt><dd>{locked.length?locked.map(k=>locks[k] || k).join('、'):'未登记'}</dd>
    {missing.length>0 && <><dt>待补计算输入</dt><dd>{missing.map(message=><div key={message}>{message}</div>)}可先保存装配，在画布的元件属性中补全。</dd></>}
    </dl>
    {referenceNotes.length>0 && <ul className="component-scope-list" aria-label="换件后的位置与引用">{referenceNotes.map(note=><li key={note}>{note}</li>)}</ul>}
    <PreviewReadings data={data} current={current}/>
    {item.wavelength_note && <p>{item.wavelength_note}</p>}
    <details><summary>来源与模型条件</summary>
      {origins.map(note=><p key={note}>{note}</p>)}{item.raw_spec && <p>{item.raw_spec}</p>}
      {item.source_file && <p>资料来源 · {item.source_file}</p>}
      {Array.from(new Set([...(data.scope_warnings || []),...next.unknowns])).filter(note=>!referenceNotes.includes(note)).map(note=><p key={note}>{note}</p>)}
    </details></>;
}

export default function ComponentPicker({caseId,current,target,taskPending,onReplacing,onClose}) {
  const {pathUid,category}=target,elementUid=target.entries[0].element.uid,revision=current.revision_id;
  const [search,setSearch]=useState(''), [skip,setSkip]=useState(0), [refresh,reload]=useState(0);
  const [page,setPage]=useState(null), [choice,setSelected]=useState(null), [editing,setEditing]=useState(undefined);
  const [orientation,setOrientation]=useState('as_listed'),[note,setNote]=useState('');
  const [spacingPolicy,setSpacingPolicy]=useState('keep_gaps');
  const [checked,setChecked]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [mobileView,setMobileView]=useState('directory');
  const section=useRef(null), workspace=useRef(null), directory=useRef(null), previewHeading=useRef(null);
  const pageKey=JSON.stringify([caseId,revision,pathUid,elementUid,skip,search,refresh]);
  const list=page?.key===pageKey ? page : null;
  const selected=list?.items?.find(item=>item.id===choice?.id) || choice;
  const selectedId=selected?.id, eligible=selected?.eligible!==false;
  const hasThickness=category==='gain_crystal' || category==='medium';
  const previewKey=JSON.stringify([caseId,revision,pathUid,elementUid,selectedId,orientation,spacingPolicy,refresh]);
  const trial=current.model_kind==='draft';
  const data=checked?.key===previewKey ? checked : null;
  const preview=data?.result;
  useEffect(()=>{section.current.focus({preventScroll:true});section.current.scrollIntoView({block:'start'});},[]);
  useEffect(()=>{
    const controller=new AbortController();
    const timer=setTimeout(()=>{
      workbenchApi.replacementCandidates(caseId,{revision_id:revision,target_element_uid:elementUid,target_path_uid:pathUid,
        limit:PAGE_SIZE,skip,search:search.trim() || undefined},controller.signal)
        .then(result=>{if(!controller.signal.aborted)setPage({key:pageKey,...result});})
        .catch(cause=>{if(!controller.signal.aborted)setPage({key:pageKey,error:cause.message});});
    },search?180:0);
    return ()=>{clearTimeout(timer);controller.abort();};
  },[caseId,revision,elementUid,pathUid,skip,search,pageKey]);
  useEffect(()=>{
    if(selectedId==null || !eligible || editing!==undefined)return;
    const controller=new AbortController();
    workbenchApi.previewReplacement(caseId,{base_revision_id:revision,target_element_uid:elementUid,target_path_uid:pathUid,
      inventory_id:selectedId,orientation,spacing_policy:spacingPolicy},controller.signal)
      .then(result=>{if(!controller.signal.aborted)setChecked({key:previewKey,result});})
      .catch(cause=>{if(!controller.signal.aborted)setChecked({key:previewKey,error:cause.message});});
    return ()=>controller.abort();
  },[caseId,revision,pathUid,elementUid,selectedId,eligible,orientation,spacingPolicy,editing,previewKey]);
  const choose=item=>{setSelected(item);setOrientation('as_listed');setNote('');setError('');};
  const compareItem=(item)=>{
    if(item)choose(item);
    setMobileView('preview');
    requestAnimationFrame(()=>{
      if(!item || workspace.current && getComputedStyle(workspace.current).display!=='grid')
        previewHeading.current?.focus();
    });
  };
  const returnToStock=()=>{
    setMobileView('directory');
    requestAnimationFrame(()=>{
      const row=directory.current?.querySelector('[aria-pressed="true"]') || directory.current?.querySelector('input');
      row?.focus();
    });
  };
  const filter=value=>{setSearch(value);setSkip(0);choose(null);};
  const retry=()=>{setError('');reload(n=>n+1);};
  const finishEdit=item=>{
    setEditing(undefined);filter('');
    if((page?.categories || [category]).includes(item.category))compareItem(item);
    else {setMobileView('directory');setError('库存记录已保存，当前光路对象需选择相容类型。');}
    reload(n=>n+1);
  };
  const next=preview && findStockObject(engineeringObjects(preview.project),pathUid,preview.replacement.target_element_uid);
  const canApply=next && preview.preview_token && selected?.eligible!==false && !taskPending;
  const submit=async()=>{
    if(!canApply || busy)return;
    const finish=onReplacing();
    setBusy(true);setError('');
    try{
      const saved=await workbenchApi.replaceComponent(caseId,{base_revision_id:revision,target_path_uid:pathUid,target_element_uid:elementUid,
        inventory_id:selected.id,orientation,spacing_policy:spacingPolicy,preview_token:preview.preview_token,
        change_note:note.trim() || '从库存选用 #'+selected.id+' '+selected.name+'，替换'+target.pathName+'的'+target.title+'。'});
      await finish(saved,selected);
    }catch(cause){setError(cause.message);setChecked(null);reload(n=>n+1);}
    finally{setBusy(false);}
  };
  return <section ref={section} tabIndex={-1} className="component-picker" data-mobile-view={mobileView} aria-label="为工程对象选择库存元件">
    <div className="component-heading"><div><h3>为{target.title}选择库存元件</h3>
      <p>{target.pathName} · {trial?'独立方案':'主工程'} #{revision}。确认后{trial?'另存独立方案版本':'保存新的主工程版本'}。</p></div>
      <button type="button" className="btn btn-secondary" disabled={busy} onClick={onClose}>关闭</button></div>
    <p className="component-target-summary">{target.inventoryIds.length?'当前实物':'当前模型'} · {target.entries.map(e=>e.element.name).join(' → ')} · {binding(target)}</p>
    {editing!==undefined ? <ComponentEditor item={editing} defaultCategory={page?.default_category || category} onSaved={finishEdit} onCancel={()=>setEditing(undefined)}/> : <div className="component-stock-workspace" ref={workspace}>
      <section className="component-directory" ref={directory} aria-label="候选库存">
      <div className="component-op-toolbar"><input aria-label="搜索库存元件" placeholder="按名称、位置或标签搜索" value={search} disabled={busy} onChange={e=>filter(e.target.value)}/>
        <button type="button" className="btn btn-secondary" disabled={busy || !list || !!list.error} onClick={()=>{setEditing(null);setError('');}}>新建库存记录</button></div>
      {selected && <button className="stock-skip" type="button" onClick={()=>compareItem()}>查看所选试算 →</button>}
      {!list ? <p role="status">读取库存…</p> : list.error ? <p role="alert">{list.error} <button type="button" onClick={retry}>重试读取库存</button></p> : <>
        <div className="component-stock-list">{list.items.map(item=><button type="button" className={selected?.id===item.id?'selected':''}
          key={item.id} disabled={busy} aria-pressed={selected?.id===item.id} onClick={()=>compareItem(item)}><strong>#{item.id} {item.name}</strong>
          <small>{stockDescription(item)} · {item.location && item.location+' · '}
            {item.eligible===false?item.reason : '库存可用 '+fmt(quantity(item))+(item.occupied_quantity?' · 本工程其他位置已用 '+item.occupied_quantity:'')}</small></button>)}
          {!list.items.length && <p>{search?'没有符合搜索条件的库存记录。':'还没有此类库存元件，可新建库存记录。'}</p>}</div>
        {(skip>0 || skip+PAGE_SIZE<list.total) && <div className="component-actions"><button type="button" className="btn btn-secondary" disabled={busy || !skip} onClick={()=>setSkip(skip-PAGE_SIZE)}>上一页</button>
          <span>{skip+1}–{skip+list.items.length} / {list.total}</span><button type="button" className="btn btn-secondary" disabled={busy || skip+PAGE_SIZE>=list.total} onClick={()=>setSkip(skip+PAGE_SIZE)}>下一页</button></div>}
      </>}
      </section>
      <section className="component-result" aria-label="库存替换试算">
      {selected ? <article className="component-preview"><div className="component-heading"><h4 ref={previewHeading} tabIndex={-1}>替换预览 · #{selected.id} {selected.name}</h4>
        <button type="button" className="stock-skip" onClick={returnToStock} disabled={busy}>← 返回候选</button>
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={()=>setEditing(selected)}>补充 / 编辑这件元件</button></div>
        {(category==='gain_crystal' || (category==='medium' && selected.crystal_spec?.segments?.length>1)) && <><label>{category==='gain_crystal'?'新晶体朝向':'元件朝向'}<select aria-label={category==='gain_crystal'?'新晶体朝向':'元件朝向'} value={orientation} disabled={busy} onChange={e=>setOrientation(e.target.value)}>
          <option value="as_listed">按标签分段顺序进入</option><option value="reversed">从标签最后一段进入</option></select></label><p>{crystalDescription(selected)}</p></>}
        {hasThickness && selected.eligible!==false && <label>换件时的轴向定位<select aria-label="换件时的轴向定位" value={spacingPolicy} disabled={busy} onChange={e=>setSpacingPolicy(e.target.value)}>
          <option value="keep_gaps">保留外部间距，光路总长随厚度变化</option>
          <option value="fixed_entrance">固定入射面，调整后方空气间距</option>
          <option value="fixed_exit">固定出射面，调整前方空气间距</option></select></label>}
        {selected.eligible===false ? <p>{selected.reason}</p> : data?.error ? <p role="alert">{data.error} <button onClick={retry}>重新预览</button></p>
          : preview ? <StockPreview data={preview} target={target} item={selected} current={current} next={next}/> : <p role="status">计算替换后的光路…</p>}
        {preview && <label>本次变更备注<input aria-label="替换元件变更备注" value={note} disabled={busy} onChange={e=>setNote(e.target.value)} placeholder="可留空"/></label>}
      </article> : <p className="component-preview-empty">选择库存元件后，可比较模型参数与观察面读数。</p>}
      {selected && <div className="component-actions"><button className="btn btn-primary" type="button" disabled={busy || !canApply} onClick={submit}>{busy?'正在保存…':'确认选用并复算工程'}</button>
        {taskPending && <small role="status">可继续比较试算。Agent 任务结束后可确认更换库存实物。</small>}</div>}
      </section>
    </div>}
    {error && <p className="component-error" role="alert">{error}</p>}
  </section>;
}
