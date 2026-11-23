import React, { useEffect, useState } from 'react';
import { workbenchApi } from '../api/workbench';
import BenchToolPanel from './BenchToolPanel';

export default function ComponentLibrary({ caseId, current, draft, selectedUid, selectedPathUid='primary', networkView, networkSelection, dirty, disabled, isLatest, onPlace }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState('place');
  const [records, setRecords] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [modelId, setModelId] = useState('');
  const [pathId, setPathId] = useState(null);
  const [first, setFirst] = useState(''), [last, setLast] = useState(''), [before, setBefore] = useState('');
  const [checked,setChecked] = useState(null);
  const [domains,setDomains] = useState(null), [planeChoices,setPlaneChoices] = useState(()=>new Map());
  const [name, setName] = useState(''), [note, setNote] = useState('');
  const [base, setBase] = useState(''), [power, setPower] = useState(false), [geometryOnly, setGeometryOnly] = useState(false);
  const [busy, setBusy] = useState(false), [settled, setSettled] = useState(''), [reload, setReload] = useState(0);
  const [placed, setPlaced] = useState(null);
  const revision = current?.revision_id ?? null;
  const queryKey = [caseId, revision, reload].join('/');
  const loading = open && settled !== queryKey;
  const [error, setError] = useState(''), [message, setMessage] = useState('');
  useEffect(() => {
    if (!open) return;
    let active = true;
    workbenchApi.componentModels(caseId).then(data => {
      if (active) { setRecords(data.models); setCursor(data.next_before_id); setError(''); }
    }).catch(e => { if (active) setError(e.message); }).finally(() => { if (active) setSettled(queryKey); });
    return () => { active = false; };
  }, [caseId, open, queryKey]);
  const placementExists = project => placed && [project.system.elements,...(project.incident_paths || []).map(p=>p.elements),
    project.network?.components || []].some(items=>items.some(el=>el.component_origin?.instance_uid===placed));
  const placementSaved = current && placementExists(current.project);
  const placementInDraft = placementExists(draft);
  const project = tab==='capture' ? current?.project || draft : draft;
  const arm = project.incident_paths?.find(path => path.uid === (pathId ?? selectedPathUid));
  const activePath = arm?.uid || 'primary';
  const path = arm || project.system;
  const elems = path.elements;
  const cavity = activePath === 'primary' && path.mode === 'standing_wave';
  const boundaries = cavity ? elems.slice(1) : elems;
  const focus = activePath === selectedPathUid ? elems.find(el => el.uid === selectedUid) : null;
  const defaultUid = focus?.uid || elems[0]?.uid || '';
  const firstUid = elems.find(el => el.uid === first)?.uid || defaultUid;
  const endChoices = elems.slice(elems.findIndex(el => el.uid === firstUid));
  const lastUid = endChoices.find(el => el.uid === last)?.uid || firstUid;
  const beforeUid = before === 'end' && !cavity ? null : (boundaries.find(el => el.uid === before)?.uid ||
    boundaries.find(el => el.uid === defaultUid)?.uid || boundaries[0]?.uid || null);
  const model = records.find(r => String(r.model_revision_id) === modelId) || records[0];
  const networkModel = !!model?.boundary_ports, orderedModel = model?.length_mm != null;
  const network = project.network;
  const nodes = network?.components || [];
  const focused = nodes.find(c=>c.uid===networkSelection?.component || (networkSelection?.instance && c.component_origin?.instance_uid===networkSelection.instance)) || nodes[0];
  const picked = checked?.revision===revision ? checked.ids : (focused ? [focused.uid] : []);
  const usePath = domains?.path ?? !networkView, useNetwork = domains?.network ?? networkView;
  const rangeIds = new Set(endChoices.slice(0,endChoices.findIndex(el=>el.uid===lastUid)+1).map(el=>el.uid));
  const registered = new Map((current?.analysis?.sections?.planes || []).map(p=>[p.uid,p.target_uid]));
  const planes = usePath && activePath==='primary' ? (project.observation_planes || []).filter(p=>
    rangeIds.has(p.target_uid ?? registered.get(p.uid))) : [];
  const selectedPlanes = planes.filter(p=>planeChoices.get(p.uid) ?? p.position_reference!=='fixed_z');
  const networkWave = model?.network_wavelength_nm ?? model?.wavelength_nm;
  const targetNetworkWave = network ? network.wavelength_nm ?? project.system.wavelength_nm : networkWave;
  const sameWave = (a,b)=>a!=null && b!=null && Math.abs(a-b)<=1e-12*Math.max(a,b);
  const sourceWaveMatches = !orderedModel || sameWave(model.wavelength_nm,path.wavelength_nm);
  const portableGeometry = model?.cross_wavelength_geometry && (!model.power_stages || geometryOnly);
  const pathMatches = sourceWaveMatches || portableGeometry;
  const networkMatches = !networkModel || sameWave(networkWave,targetNetworkWave);
  const planePlacement = !model?.reference_planes?.length || activePath==='primary';
  const validSelection = (usePath || useNetwork) && (!usePath || (firstUid && path.wavelength_nm)) && (!useNetwork || picked.length>0);
  const blocked = disabled || busy || loading;
  const run = async fn => {
    setBusy(true); setError(''); setMessage(''); setPlaced(null);
    try { await fn(); } catch (e) { setError(e.message); } finally { setBusy(false); }
  };
  const capture = () => run(async () => {
    const entry = await workbenchApi.captureComponent(caseId, { project_revision_id: revision,
      ...(useNetwork && {network_component_uids:picked}),
      ...(usePath && {path_uid:activePath,first_uid:firstUid,last_uid:lastUid,include_power:power,
        reference_plane_uids:selectedPlanes.map(p=>p.uid)}),
      name:name.trim(),note,previous_model_revision_id:base ? Number(base) : null });
    setRecords(items => [entry, ...items.filter(item => item.model_revision_id !== entry.model_revision_id)]);
    setModelId(String(entry.model_revision_id)); setBase(''); setTab('place');
    setMessage('已将「' + entry.name + '」v' + entry.version + ' 存入个人模型库，可在其他案例中选用。');
  });
  const place = () => run(async () => {
    const instance = await onPlace({ model_revision_id: model.model_revision_id, project_revision_id: revision,
      ...((dirty || !revision) && {draft}),
      ...(orderedModel && {path_uid:activePath,before_uid:beforeUid,geometry_only:geometryOnly}) },networkModel && (!orderedModel || networkView));
    setPlaced(instance);
  });
  const more = () => run(async () => {
    const data = await workbenchApi.componentModels(caseId, { before_id: cursor });
    setRecords(items => [...items, ...data.models]); setCursor(data.next_before_id);
  });
  const options = elements => elements.map(el => <option key={el.uid} value={el.uid}>{elems.indexOf(el) + 1}. {el.name}</option>);
  const modelOptions = records.map(r => <option key={r.model_revision_id} value={r.model_revision_id}>
    {r.name} · v{r.version} · {r.wavelength_nm} nm</option>);
  return <BenchToolPanel title="我的元件与组件模型" className="bench-library" caseId={caseId} open={open}
    onOpenChange={next=>{setOpen(next); if(next)setReload(n=>n+1);}}>
    {open && <div className="library-body">
      <div className="library-tabs">{[['place', '从库中插入'], ['capture', '将光路存入库']].map(([value, label]) =>
        <button type="button" key={value} aria-pressed={value === tab} onClick={() => { setTab(value); setDomains(null); setError(''); setMessage(''); }}>{label}</button>)}</div>
      {tab==='capture' && !!nodes.length && !!project.system.elements.length && <fieldset><legend>组件范围</legend>
        <label className="library-check"><input type="checkbox" checked={usePath} onChange={e=>setDomains({path:e.target.checked,network:useNetwork})}/>顺序光路区段</label>
        <label className="library-check"><input type="checkbox" checked={useNetwork} onChange={e=>setDomains({path:usePath,network:e.target.checked})}/>端口网络元件</label>
      </fieldset>}
      {(tab==='capture' ? usePath : orderedModel) && <label>工程光路<select value={activePath} onChange={e => { setPathId(e.target.value); setFirst(''); setLast(''); setBefore(''); }}>
        <option value="primary">主光路</option>{project.incident_paths?.map(p => <option key={p.uid} value={p.uid}>{p.name}</option>)}</select></label>}
      {tab === 'capture' ? <>
        {useNetwork && <fieldset><legend>组件包含的端口元件</legend>{nodes.map((c,i)=><label key={c.uid} className="library-check">
          <input type="checkbox" checked={picked.includes(c.uid)} onChange={e=>setChecked({revision,
            ids:e.target.checked?[...picked,c.uid]:picked.filter(id=>id!==c.uid)})}/>{i+1}. {c.name}</label>)}
          {!nodes.length && <p>请先在端口连接图添加元件并保存工程版本。</p>}</fieldset>}
        {usePath && <div className="library-fields"><label>起始元件<select value={firstUid} onChange={e => { setFirst(e.target.value); setLast(''); }}>{options(elems)}</select></label>
          <label>结束元件<select value={lastUid} onChange={e => setLast(e.target.value)}>{options(endChoices)}</select></label></div>}
        {!!planes.length && <fieldset><legend>随组件移动的参考面</legend>{planes.map(p=><label className="library-check" key={p.uid}>
          <input type="checkbox" checked={selectedPlanes.includes(p)} onChange={e=>setPlaneChoices(new Map(planeChoices).set(p.uid,e.target.checked))}/>
          {p.name}{p.position_reference==='fixed_z' && '（将实验台坐标转换为元件内深度）'}</label>)}</fieldset>}
        <label>保存方式<select value={base} onChange={e => {
          setBase(e.target.value); const chosen = records.find(item => String(item.model_revision_id) === e.target.value);
          if (chosen) { setName(chosen.name); setNote(chosen.note); }
        }}><option value="">新建模型</option>{modelOptions}</select></label>
        {base && <small>将从所选版本发布下一版。已装入工程的模型保留当时的参数。</small>}
        <label>模型名称<input value={name} maxLength={100} onChange={e => setName(e.target.value)} /></label>
        <label>参数依据与适用条件<textarea value={note} maxLength={1200} placeholder="例如标定波长、朝向、测量方法。这段说明将随模型进入目标案例。" onChange={e => setNote(e.target.value)} /></label>
        {useNetwork && <p>保留选区内部连线、功率引用和光场引用。外部光源可在新工程中接入。</p>}
        {usePath && <>
          <label className="library-check"><input type="checkbox" checked={power} onChange={e => setPower(e.target.checked)} />同时保存逐件传输参数及其来源文字</label>
          <p>按光传播顺序保存所选范围的近轴参数，建模波长 {path.wavelength_nm ?? '未知'} nm。元件模型可在其他案例中独立使用。</p></>}
        <button type="button" className="btn btn-secondary" disabled={blocked || !revision || dirty || !name.trim() || !validSelection} onClick={capture}>保存到个人模型库</button>
      </> : model ? <>
        <label>模型版本<select value={model.model_revision_id} onChange={e => { setModelId(e.target.value); setGeometryOnly(false); }}>{modelOptions}</select></label>
        <p><strong>{model.element_names.join(networkModel?'、':' → ')}</strong><br/>{model.note || '此模型没有附加来源说明。'}</p>
        {networkModel && <><p>放入端口连接图 · {model.internal_links} 条内部连接 · {model.boundary_ports.length} 个可接线端口
          {orderedModel && <> · {model.internal_feeds} 个端口引用所选光路</>}</p>
          <details><summary>查看可接线端口</summary><ul>{model.boundary_ports.map(p=><li key={JSON.stringify([p.component,p.port])}>{p.component}. {p.name} / {p.port}</li>)}</ul></details></>}
        {!!model.reference_planes?.length && <p>组件参考面 · {model.reference_planes.join('、')}</p>}
        {!!model.input_references?.length && <details><summary>查看组件内的取光位置</summary><ul>
          {model.input_references.map(r=><li key={JSON.stringify([r.component,r.port])}>{r.component}. {r.name} / {r.port}
            {' · '+(r.power_reference?'功率取自 '+r.power_reference:'接收工程需指定功率')+' · '+
              (r.field_reference?'光场取自 '+r.field_reference:'空间场计算前需指定光场')}</li>)}</ul></details>}
        {!planePlacement && <p role="status">这些观察面登记在主光路的元件上。请选择主光路放入组件。</p>}
        {orderedModel && <><label>插入位置<select value={beforeUid || 'end'} onChange={e => setBefore(e.target.value)}>
          {boundaries.map(el => <option key={el.uid} value={el.uid}>{elems.indexOf(el) + 1}. {el.name} 之前</option>)}
          {!cavity && <option value="end">光路末端</option>}</select></label>
        <p>光路总长将增加 {Number(model.length_mm.toPrecision(8))} mm。{arm ? '支路入口固定，源端向上游移动。' : '原有元件间距参数保持原值，插入点下游的位置随之移动。'}</p>
        {model.power_stages > 0 && <label className="library-check"><input type="checkbox" checked={geometryOnly} onChange={e => setGeometryOnly(e.target.checked)} />仅插入近轴参数，略过 {model.power_stages} 条传输参数</label>}</>}
        {!sourceWaveMatches && (model.cross_wavelength_geometry ?
          <p role="status">{portableGeometry ? '将按目标光路的波长重新计算材料折射率。仅有原波长标定值的介质需补充数据。' :
            '逐件传输参数仅适用于源波长。勾选仅插入近轴参数，可在此处使用介质几何。'}</p> :
          <p role="status">模型适用 {model.wavelength_nm} nm，所选光路为 {path.wavelength_nm ?? '未知'} nm，请选择匹配的版本。</p>)}
        {!networkMatches && <p role="status">端口标定波长为 {networkWave} nm，当前网络为 {targetNetworkWave} nm。</p>}
        {revision && !isLatest && <p>正在查看历史工程。请先回到最新版本再插入。</p>}
        <button type="button" className="btn btn-secondary" disabled={blocked || (revision && !isLatest) || !pathMatches || !networkMatches || !planePlacement} onClick={place}>插入草稿并预览</button>
      </> : !loading && !error && <p>库中还没有模型。选择“将光路存入库”，可从当前画布保存单个元件或一组元件。</p>}
      {tab==='capture' && (dirty || !revision) && <p>请先保存当前工程版本，模型库会记录其来源。</p>}
      {loading && <p role="status">正在读取个人模型库…</p>}
      {cursor && <button type="button" disabled={busy} onClick={more}>加载更早的模型版本</button>}
      {error && <div role="alert" className="wb-error">{error} <button type="button" disabled={busy} onClick={() => setReload(n => n + 1)}>刷新模型库</button></div>}
      {message && <p role="status">{message}</p>}
      {placed && <p role="status">{placementSaved ? '组件已保存到工程 #' + current.revision_id + '。' :
        placementInDraft ? '已插入草稿。可在光路图查看变化，点击工作台的保存按钮保留这个方案。' : '当前草稿中已移除这次插入的组件。'}</p>}
    </div>}
  </BenchToolPanel>;
}
