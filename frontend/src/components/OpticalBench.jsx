import {useMenuAction} from './menuCommands';
import React, { useEffect, useId, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react';
import ComponentLibrary from './ComponentLibrary';
import ElementRemoval from './ElementRemoval';
import ProjectVariants from './ProjectVariants';
import MeasuredLaunch from './MeasuredLaunch';
import AssemblyEditor from './AssemblyEditor';
import BeamBenchPlot from './BeamBenchPlot';
import ProbeReadings from './ProbeReadings';
import StationCenterComparison from './StationCenterComparison';
import {findStockObject,frozenIndexModel} from './inventoryTargets';
import {radiusBasisLabels} from './beamWidths';
import NetworkBench from './NetworkBench';
import {InspectorHeading,InspectorToggle,useBenchInspector} from './BenchInspector';
import LiveLayoutStudy from './LiveLayoutStudy';
import StudyUsage from './StudyUsage';
import {componentLabels,orderedSelections} from './layoutSelections';
import OpticalElementFields from './OpticalElementFields';
import SystemBeamFields from './SystemBeamFields';
import {mainBeamStatus} from './mainBeamSource';
import { useBeamReadings } from './useBeamReadings';
import { ElementPowerReading } from './PathPower';
import RegisteredSections from './RegisteredSections';
import {SharedMediumReadings} from './SharedMediumIndices';
import { adjustableGap, appendOptic, benchLayout, hasEndMirrors, incidentControl, insertOptic, moveAlongAxis, moveIncidentElement, newIncidentPath, observationControl, positionControl, savedPreview, starterProject, withElementParameter } from './benchGeometry';
import { useBenchPreview } from './useBenchPreview';
import { createBenchLayoutSelector } from './benchSolverLayout';
import { equalValues } from './projectDraft';
import {positionText} from './plotViewport';
import {applyPositionEntry, enteredPosition, resolvePositionEntry} from './positionIntent';
import {localId} from '../api/localId';
import {initialEngineeringView,offAxisDirection} from './beamProjection';
import {cavityStatus, halfTraceReading} from './cavityStability';
import './OpticalBench.css';
import './BenchAppearance.css';

const labels = { single_pass: '单程传播', standing_wave: '驻波腔', ring: '环形腔 · 展开一周' };
const fmt = n => Number.isFinite(n) ? Number(n.toPrecision(5)).toString() : '—';
const rowKey = row => row.element.uid || row.index;
const sameTrain = (a, b) => a.length === b.length && a.every((e,i)=>e.uid===b[i].uid && e.kind===b[i].kind);

function ProfileLimits({planes, arm, beamName, branchName}) {
  const rows = [[beamName,planes],[branchName,arm]].flatMap(([beam,axes])=>{
    const entries = new Map();
    for (const [axis,plane] of Object.entries(axes || {})) {
      for (const item of plane.profile_omissions || []) {
        const id=JSON.stringify(item);
        if (!entries.has(id)) entries.set(id,{...item,beam,directions:[]});
        entries.get(id).directions.push(axis==='tangential'?'T':'S');
      }
    }
    return [...entries.values()];
  });
  if (!rows.length) return null;
  return <details className="bench-input-issues"><summary>部分介质内的束宽振荡过密 · 图中仅绘两端，查看半径范围</summary>
    {rows.map((row,i)=><p key={i}>{row.beam} {row.directions.join('/')} · {row.name} · z = {fmt(row.start_mm)} 至 {fmt(row.end_mm)} mm
      · 段内半径 {fmt(row.radius_min_mm*1000)} 至 {fmt(row.radius_max_mm*1000)} μm。
      首个最小值在 z = {fmt(row.first_min_z_mm)} mm，其后每隔 {fmt(row.period_mm)} mm 重复。</p>)}</details>;
}

function CenterReadout({name,plane}) {
  const points=plane?.center_profile;
  if (!Array.isArray(points)) return <span>{name} · 本次结果没有记录束心轨迹</span>;
  if (!points.length) return <span>{name} · 此方向没有可定位的束心</span>;
  const [startZ,startY]=points[0], [endZ,endY]=points.at(-1);
  return <span>{name} · {plane.center_basis==='reference_axis' && '按理想轴上模型 · '}
    在 z = {fmt(startZ)} mm 处为 {fmt(startY*1000)} μm{points.length>1 && <>，在 z = {fmt(endZ)} mm 处为 {fmt(endY*1000)} μm</>}</span>;
}

function PositionEntry({ control, disabled, entry, pending, inputRef, onEntry, onApply }) {
  const hintId = useId();
  const [showProblem, setShowProblem] = useState(pending);
  let value, problem;
  if (pending) {
    try { value = enteredPosition(entry, control); }
    catch (cause) { problem = cause.message; }
  }
  const error = showProblem && problem;
  const commit = () => {
    if (!pending || disabled) return;
    if (problem) { setShowProblem(true); return; }
    onApply(value);
    setShowProblem(false);
  };
  return <form onSubmit={e => { e.preventDefault(); commit(); }} noValidate>
    <label>{control.label} / mm<div className="bench-entry"><input ref={inputRef} aria-label="元件轴向位置 / mm" type="text" inputMode="decimal" autoComplete="off" value={entry}
      aria-invalid={Boolean(error) || undefined} aria-describedby={hintId} disabled={disabled} onChange={e => { setShowProblem(false); onEntry(e.target.value); }}
      onBlur={e => { if (e.relatedTarget) commit(); }}
      onKeyDown={e => {
        if (e.key !== 'Escape' || !pending) return;
        e.preventDefault(); e.stopPropagation();
        setShowProblem(false); onEntry(null);
      }} /><button disabled={disabled} className="btn btn-secondary">应用</button></div></label>
    <p id={hintId} className="bench-hint" role={error ? 'alert' : undefined}>{error || control.hint}</p>
  </form>;
}

export default function OpticalBench({ fileActions, catalog, liveStudies, caseId, versions, onOpenStudy, savedStudyId, studyOrigin, onUseQuantity, draft, current, readWorkingDraft, files, refreshToken, dirty, disabled, error, onEdit, beginGesture, onUndo, canUndo, undoLabel, onRedo, canRedo, redoLabel, onSave, onParameters, benchChat: {host: setConversationHost, docked: conversationOpen, open: openConversation, close: closeConversation, ask: askFromCanvas}, openFiles, isLatest, onPlace, draftFeedback, saveBlocked, forkBlocked, unlock, onFork, onOpenVersion, onAdopting, onMeasuredLaunch, inventoryObjects, inventoryBlock, onPickStock, stockPicker, onModeChange, positionEntries, onPositionEntry, onMaterialReadings }) {
  useMenuAction('undo', !disabled && canUndo, onUndo);
  useMenuAction('redo', !disabled && canRedo, onRedo);
  const benchCatalog = catalog.presets;
  const panel = useRef(null), positionInput = useRef(null);
  const [selection, select] = useState(null), [overlayChoice, setCompare] = useState(null);
  const [openingView]=useState(()=>initialEngineeringView(current));
  const [projection,setProjection]=useState(openingView.projection);
  const engineering=projection!=='width';
  const [planeUid, setPlaneUid] = useState(null);
  const [plotTarget,setPlotTarget] = useState(null);
  const [pathUid,setPathUid]=useState(openingView.pathUid), [inspectArm,setInspectArm]=useState(false);
  const [armKey,setArmKey]=useState(null), [openRequest,setOpenRequest]=useState(null);
  const [full, setFull] = useState(false), [screenError, setScreenError] = useState('');
  const [representation,setRepresentation] = useState(null);
  const inspector=useBenchInspector();
  const [positionFocus, setPositionFocus] = useState(null);
  const focusedRequest = useRef(null);
  const [networkSelection, setNetworkSelection] = useState(null);
  const [networkInputStatus, setNetworkInputStatus] = useState('');
  const networkView = (representation ?? (draft.network ? 'network' : 'path')) === 'network';
  const [insertKind, setInsertKind] = useState('lens');
  const [launchFit,setLaunchFit] = useState(null);
  useEffect(() => {
    const change = () => setFull(document.fullscreenElement === panel.current);
    document.addEventListener('fullscreenchange', change);
    return () => document.removeEventListener('fullscreenchange', change);
  }, []);
  const geometry = useMemo(() => benchLayout(draft), [draft]);
  const {positions, routes} = geometry;
  const activeRoute = routes.find(r=>r.path.uid===pathUid) || routes.find(r=>r.entrance) || routes[0];
  const offsetInView=[draft.system.source?.launch_ray,activeRoute?.path.launch_ray].some(ray=>
    offAxisDirection(ray,'tangential') || offAxisDirection(ray,'sagittal'));
  const beamReadings = useBeamReadings(caseId, current?.project?.observation_planes?.length ? current.revision_id : null, refreshToken);
  const routeReadings = beamReadings.items.filter(r => r.beam_uid === 'primary' || r.beam_uid === activeRoute?.path.uid);
  const observed = routeReadings.filter(r => Object.keys(r.measured_minus_model_mm || {}).length);
  const compare = overlayChoice ?? routeReadings.length > 0;
  const viewingArm = inspectArm && activeRoute?.path.uid === pathUid;
  const armRow = viewingArm ? activeRoute.rows.find(r => rowKey(r) === armKey) : null;
  const selectArm=(uid,index,reveal=false)=>{
    const row=routes.find(r=>r.path.uid===uid)?.rows[index];
    setPathUid(uid); setInspectArm(true); setArmKey(row?rowKey(row):null); setOpenRequest(null);
    setPlotTarget({kind:'optic',reveal});
  };
  const selectMain=(i,reveal=false)=>{
    select(rowKey(positions[i]));setInspectArm(false);setOpenRequest(null);
    setPlotTarget({kind:'optic',reveal});
  };
  const selectProbe=uid=>{setPlaneUid(uid);setPlotTarget({kind:'probe',reveal:true});};
  const addPath=(targetUid,open)=>{
    const path=newIncidentPath(draft,targetUid);
    if(!path)return;
    onEdit({...draft,incident_paths:[...(draft.incident_paths||[]),path]});
    setPathUid(path.uid);setArmKey(null);
    setInspectArm(open);
    if (open) setPlotTarget({kind:'optic',reveal:true});
    setOpenRequest(open ? {uid:path.uid} : null);
  };
  const selected = positions.find(p => rowKey(p) === selection) || positions[0];
  const control = viewingArm ? (armRow && incidentControl(activeRoute,armRow.index)) : (selected && positionControl(draft, selected.index, positions));
  const selectedKey=viewingArm ? armKey : selected && rowKey(selected);
  const ownerUid=viewingArm ? activeRoute.path.uid : 'primary';
  const activeEntry = !networkView && control && positionEntries.find(e=>e.pathUid===ownerUid && e.elementUid===selectedKey);
  const entry = activeEntry ? activeEntry.text : control ? String(control.value) : '';
  const unapplied = positionEntries.length > 0;
  const edited = dirty || unapplied;
  const heldPositions = positionEntries.filter(item=>item!==activeEntry).map(item=>{
    try { return {item, ...resolvePositionEntry(draft,item,geometry)}; }
    catch (cause) { return {item, issue:cause.message}; }
  });
  useLayoutEffect(()=>{
    const input = positionInput.current;
    if (!positionFocus || focusedRequest.current===positionFocus || networkView || !input ||
      positionFocus.pathUid!==ownerUid || positionFocus.elementUid!==selectedKey) return;
    // The narrow inspector focuses its container when it opens. Give a requested
    // recovery target focus after that layout effect and after the selection renders.
    focusedRequest.current=positionFocus;
    input.focus({preventScroll:true});
    input.scrollIntoView({block:'nearest'});
  },[positionFocus, networkView, inspector.opened, ownerUid, selectedKey]);
  function revealPosition(item) {
    if (!item) return;
    let row;
    try { ({row} = resolvePositionEntry(draft,item,geometry)); }
    catch {
      requestAnimationFrame(()=>panel.current?.querySelector('.bench-held-positions button')?.focus());
      return;
    }
    setRepresentation('path');
    if (item.pathUid==='primary') selectMain(row.index,true);
    else selectArm(item.pathUid,row.index,true);
    inspector.open(document.activeElement);
    setPositionFocus({pathUid:item.pathUid,elementUid:item.elementUid});
  }
  const linkedRevision = edited || saveBlocked ? null : current?.revision_id;
  const selectLayout = useMemo(() => createBenchLayoutSelector(), []);
  const storedLayout = useMemo(() => selectLayout(current?.project), [selectLayout, current?.project]);
  const layout = useMemo(() => selectLayout(draft), [selectLayout, draft]);
  const fullLayout = useMemo(() => ({...layout, network:draft.network ?? null}), [layout, draft.network]);
  const modelChanged = useMemo(() => dirty && !equalValues(layout, storedLayout), [dirty, layout, storedLayout]);
  const baseline = useMemo(() => savedPreview(current?.analysis), [current?.analysis]);
  const previousGrid = Boolean(baseline && !(current.analysis.profile_sampling_version>=2) &&
    [current.project.system, ...(current.project.incident_paths || [])].some(path=>
      path.elements?.some(el=>el.kind==='grin' || el.kind==='thick_lens')));
  const trustedBaseline = previousGrid ? null : baseline;
  const legacyAxes=baseline?.sections?.paths?.find(p=>p.uid===activeRoute?.path.uid)?.axes;
  const missingTraces = routes.some(r=>!baseline?.sections?.paths?.some(trace=>trace.uid===r.path.uid));
  const missingExits = viewingArm && !!armRow && Object.values(legacyAxes || {}).some(axis => !Object.hasOwn(axis, 'exit_radii_mm'));
  const missingSections = draft.observation_planes?.length > 0 && !baseline?.sections?.planes;
  const legacyNetwork = baseline && draft.network?.components?.length > 0 && !baseline.network &&
    !baseline.input_issues?.some(issue=>issue.path?.[0]==='network');
  const replay = missingTraces || missingExits || missingSections || legacyNetwork || previousGrid;
  const needsPreview = modelChanged || !baseline || replay;
  useImperativeHandle(fileActions, () => ({
    inspectNetworkComponent(uid,port) {
      if (!draft.network?.components.some(component => component.uid === uid)) return;
      setRepresentation('network');
      setNetworkSelection({component:uid,port});
      inspector.open(document.activeElement);
      requestAnimationFrame(() => {
        const properties = panel.current?.querySelector('.network-inspector-pane');
        properties?.scrollIntoView({block:'nearest'});
        properties?.focus({preventScroll:true});
      });
    },
    inspectElement(uid,index,focus='inspector',project=draft) {
      const path=uid==='primary' ? project.system : project.incident_paths?.find(p=>p.uid===uid);
      const key=path?.elements[index]?.uid;
      if(!key)return;
      setPathUid(uid);setRepresentation('path');setInspectArm(uid!=='primary');
      if(uid==='primary')select(key);
      else setArmKey(key);
      setPlotTarget({kind:'optic',reveal:true});
      if(focus==='plot'){
        inspector.dismiss();
        requestAnimationFrame(()=>{
          const chart=panel.current?.querySelector('.bench-svg');
          chart?.scrollIntoView({block:'center'});chart?.focus({preventScroll:true});
        });
      } else if(focus){inspector.open(document.activeElement);panel.current?.scrollIntoView({block:'start'});}
    },
    useFit(id) { setLaunchFit({id}); },
    positionDraft(entries,project) {
      const [item] = entries;
      if (entries.length!==1 || networkView || !control || item.pathUid!==ownerUid || item.elementUid!==selectedKey)
        throw Error('还有未应用的元件位置。请回到光路核对输入，或舍弃该项后继续。');
      return applyPositionEntry(project,item,project===draft ? geometry : undefined);
    },
    focusPosition() {
      revealPosition(heldPositions[0]?.item || activeEntry);
    },
  }));
  const showModel = Boolean(current || dirty);
  const selections = useMemo(()=>orderedSelections(layout, ownerUid), [layout, ownerUid]);
  const live = useBenchPreview(caseId, layout, needsPreview && showModel, undefined, current?.revision_id ?? null,
    {model:layout, contextVersion:refreshToken});
  const result = needsPreview ? live.data : baseline;
  const materialReadings=result?.material_indices;
  useEffect(()=>{onMaterialReadings?.(materialReadings);},[materialReadings,onMaterialReadings]);
  const priorInput = live.previous?.system ?? current?.project;
  const priorSystem = priorInput?.system ?? priorInput;
  const priorPath = priorInput?.incident_paths?.find(p=>p.uid===activeRoute?.path.uid);
  const sameRoute = !activeRoute || (priorPath?.target_uid===activeRoute.path.target_uid && priorPath &&
    priorPath.through_uid===activeRoute.path.through_uid &&
    sameTrain(priorPath.elements || [], activeRoute.path.elements || []));
  const pendingProfile = !result && !live.error && priorSystem?.mode === draft.system.mode && sameRoute &&
    sameTrain(priorSystem.elements, draft.system.elements) ? live.previous?.data ?? trustedBaseline : null;
  const armTrace=result?.sections?.paths?.find(p=>p.uid===activeRoute?.path.uid);
  const pathNotes=[...new Set([armTrace?.message,...Object.values(armTrace?.axes || {}).map(a=>a.message)].filter(Boolean))];
  const planes = draft.observation_planes || [];
  const planeNames=useMemo(()=>componentLabels(draft.observation_planes || []),[draft.observation_planes]);
  const probe = planes.find(plane => plane.uid === planeUid) || planes[0];
  const probeControl = observationControl(probe, positions);
  const sample = result?.sections?.planes?.find(plane => plane.uid === probe?.uid);
  const sampleHost = sample && positions.find(row=>row.element.uid===sample.target_uid);
  const fullLayoutIncomplete=Boolean((result || live.previous?.data || baseline)?.input_issues?.length);
  const t = result?.planes.tangential, s = result?.planes.sagittal;
  const cavity = draft.system.mode !== 'single_pass';
  const status = !positions.length ? '尚无顺序元件' : !result ? live.error ? '预览未完成' : '正在计算' :
    cavity ? cavityStatus(result.planes) : t && s ? mainBeamStatus(draft.system,t,s) : '主光路待补全';
  const radii = viewingArm ? ['tangential','sagittal'].map(axis=>armRow ? armTrace?.axes?.[axis]?.exit_radii_mm?.[armRow.index]
    : (axis==='sagittal' && activeRoute.path.sagittal || activeRoute.path.tangential)?.radius_mm)
    : [t, s].map(p => selected ? p?.radii_mm[selected.index] : null);
  const radiusText = radii.map((radius, index) => {
    if (Number.isFinite(radius)) return fmt(radius * 1000);
    const plane = index === 0 ? t : s;
    if (cavity && !viewingArm && Number.isFinite(plane?.m)) {
      if (plane.marginal) return '临界附近，半径未定';
      if (plane.stable === false) return '无稳定近轴腔模';
      if (draft.system.wavelength_nm == null) return '待补波长';
    }
    return '—';
  });
  const spotMax = Math.max(...radii.map(r => r || 0));
  const focusElement = viewingArm ? armRow?.element : selected?.element;
  const stockObject=findStockObject(inventoryObjects,ownerUid,focusElement?.uid);
  const focusParts = viewingArm ? activeRoute.arm.parts : draft.parts;
  const focusPart = focusParts.find(p=>p.element_index===(viewingArm ? armRow?.index : selected?.index));
  const lockedFields = unlock ? [] : focusPart?.locked_parameters || [];
  function updateSelected(key, value) {
    if (!focusElement || lockedFields.includes(key)) return;
    const path = viewingArm ? activeRoute.path : draft.system;
    const element=withElementParameter(focusElement,key,value,path.wavelength_nm);
    if (element===focusElement) return;
    const index = viewingArm ? armRow.index : selected.index;
    const updated = {...path, elements: path.elements.map((e, i) => i === index ? element : e)};
    onEdit(viewingArm ? {...draft, incident_paths: draft.incident_paths.map(p => p.uid === path.uid ? updated : p)}
      : {...draft, system: updated});
  }
  function writePosition(text) {
    const raw = text === String(control.value) ? null : text;
    let uid = focusElement.uid;
    if (!uid) {
      if (raw == null) return;
      // Pre-ID files gain an identity only in the edited copy. Subsequent
      // reorders and reloads must never bind typed text to a different optic.
      uid = localId();
      updateSelected('uid',uid);
      if (viewingArm) setArmKey(uid); else select(uid);
      requestAnimationFrame(()=>positionInput.current?.focus({preventScroll:true}));
    }
    const row = viewingArm ? armRow : selected;
    onPositionEntry(ownerUid,uid,raw,{
      pathName:viewingArm ? activeRoute.path.name : '主光路', elementName:row.element.name,
      ordinal:row.index + 1, positionMm:control.value,
    });
  }
  const isSource = viewingArm && !armRow;
  const focusZ = viewingArm ? (activeRoute.sourceZ == null ? null : activeRoute.sourceZ + (armRow?.end ?? 0)) : selected?.end;
  const beamName = viewingArm ? activeRoute.path.name : (cavity ? '腔模' : '主光束');
  const wavelength = viewingArm ? activeRoute.path.wavelength_nm : draft.system.wavelength_nm;
  const hasPupil=draft.system.elements.some(el=>el.clear_aperture) || activeRoute?.path.elements?.some(el=>el.clear_aperture);
  const insert = () => {
    const next = insertOptic(draft, selected.index, benchCatalog[insertKind]);
    if (next !== draft) { onEdit(next); select(next.system.elements[selected.index + 1].uid); setPlotTarget({kind:'optic',reveal:true}); }
  };
  const fullscreen = async () => {
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await panel.current.requestFullscreen(); }
    catch { setScreenError('当前浏览器未进入全屏。可使用浏览器的全屏命令。'); }
  };
  const onAsk = (text,refs) => { inspector.dismiss(); askFromCanvas(text,refs); };
  const leaveCanvas = async action => {
    closeConversation();
    inspector.dismiss();
    if (document.fullscreenElement === panel.current) await document.exitFullscreen();
    action();
  };
  const modeControl = <select aria-label="顺序光路类型" disabled={disabled} value={draft.system.mode}
    onChange={event => onModeChange(event.target.value)}>
    {Object.entries(labels).map(([mode,label]) => <option key={mode} value={mode}>{label}</option>)}</select>;
  const issues = result?.input_issues || [];
  const visibleIssues = new Set(issues.map(issue => issue.summary));
  const repair = (issue, trigger) => {
    if (issue.path[0] === 'network') {
      setRepresentation('network');
      inspector.open(trigger);
      requestAnimationFrame(() => panel.current?.querySelector('.network-inspector input[aria-label="网络波长 / nm"]')?.focus());
      return;
    }
    if (issue.owner === 'primary' && issue.path[0] === 'wavelength_nm') {
      const field = panel.current?.querySelector('.bench-source');
      if (field) { field.open = true; field.querySelector('input')?.focus(); }
      return;
    }
    if (issue.element_uid) {
      if (issue.owner === 'primary') {
        const row = positions.find(p => p.element.uid === issue.element_uid);
        if (row) selectMain(row.index,true);
      } else selectArm(issue.owner, issue.path[1],true);
    } else setInspectArm(false);
    inspector.open(trigger);
    requestAnimationFrame(() => {
      const fields = panel.current?.querySelector('.bench-inspector');
      const input = issue.element_uid && Array.from(fields?.querySelectorAll('[data-element-field]') || [])
        .find(node => node.dataset.elementField === issue.path.at(-1));
      (input || fields?.querySelector(issue.element_uid ? '.bench-element-fields input' : '.bench-insert select'))?.focus();
    });
  };
  const addControl = <div className="bench-insert"><label>添加到光路<select aria-label="画布添加元件类型" value={insertKind} onChange={e=>setInsertKind(e.target.value)}>
        {Object.entries(benchCatalog).map(([key,preset])=><option key={key} value={key}>{preset.name}</option>)}</select></label>
        <button type="button" className="btn btn-primary" disabled={disabled || positions.length>=80} onClick={()=>{
          const next=appendOptic(draft,benchCatalog[insertKind]);
          const existing=new Set(draft.system.elements.map(e=>e.uid));
          select(next.system.elements.find(e=>!existing.has(e.uid))?.uid); setPlotTarget({kind:'optic',reveal:true}); onEdit(next);
        }}>{hasEndMirrors(draft.system)?'添加至末端镜前':'添加至光路末端'}</button></div>;
  const recentFile = !showModel && files?.reduce((recent,file)=>!recent || file.id>recent.id ? file : recent,null);
  const source = draft.system.source;
  const sourceRadius = source ? source.tangential.radius_mm : draft.system.input_radius_mm;
  const focusMissingSource = () => {
    const fields = panel.current?.querySelector('.bench-source');
    if (!fields || !source) return;
    fields.open = true;
    const independentS = !t?.message && s?.message && source.sagittal;
    const axis = independentS || source.tangential, name = independentS ? 'S' : 'T';
    const quantity = axis.radius_mm == null ? '输入半径 / mm' : axis.m2 == null ? 'M²'
      : axis.wavefront==='curved' ? '波前 R / mm' : '波前';
    fields.querySelector(`[aria-label="画布主光路 ${name} ${quantity}"]`)?.focus();
  };
  const sourceControls = <details className="bench-source"><summary>{cavity ? '腔模波长' : '主光束参数'} · {fmt(draft.system.wavelength_nm)} nm
    {!cavity && (source?.sagittal ? ` · T ${fmt(sourceRadius)} / S ${fmt(source.sagittal.radius_mm)} mm`
      : sourceRadius == null ? ' · 待补入射半径' : ` · 入射半径 ${fmt(sourceRadius)} mm`)}</summary>
    <fieldset className="bench-parameter-grid" disabled={disabled}>
      <SystemBeamFields system={draft.system} prefix="画布主光路 " change={system=>onEdit({...draft,system})}/>
    </fieldset>
    {!cavity && !source && draft.system.input_curvature_mm == null && <p className="bench-hint">入射面按束腰计算。</p>}
    {!cavity && source && [[t,'T'],[s,'S']].map(([axis,label])=>axis?.message &&
      <p key={label} className="bench-hint">{label} · {axis.message}</p>)}
    {positions.length>0 && <button type="button" className="btn btn-secondary" onClick={()=>setLaunchFit({id:null})}>从实测拟合接入光束</button>}
  </details>;
  return <section ref={panel} tabIndex={-1} className="optical-bench" aria-label="交互光路工作台"
    onFocusCapture={event=>{
      if (conversationOpen && event.target.closest('.bench-tool-body,[data-docked]')) closeConversation();
    }}>
    <div className="bench-toolbar"><header className="bench-head"><div className="bench-heading"><h2>光路工作台</h2><span className="bench-version">{current
      ? <button type="button" className="bench-quantity" title={'查看并引用已存版本 #' + current.revision_id + ' 的数据'} onClick={()=>leaveCanvas(()=>onUseQuantity({
          revision_id:current.revision_id, label:current.project.title
        }))}>{dirty ? '草稿基于' + (current.model_kind === 'draft' ? (current.variant_name || '方案') : '工程') + ' #' + current.revision_id
          : current.model_kind === 'draft' ? (current.variant_name || '试算方案') + ' · #' + current.revision_id : '使用已存工程 #' + current.revision_id + ' 的数据'}</button>
      : dirty ? '光路草稿' : '空白光路'}{dirty && ' · 有未保存修改'}{unapplied && ' · 输入的位置待应用'}</span>
      {studyOrigin?.run_id && <span className="bench-version">画布显示工程基础分析 · <button type="button" className="bench-quantity"
        title={studyOrigin.method_title || '查看原方法、输入和计算结果'} onClick={()=>leaveCanvas(()=>onOpenStudy(studyOrigin.run_id))}>原计算 #{studyOrigin.run_id}</button></span>}
      <button type="button" className="btn btn-secondary bench-agent" onClick={()=>{inspector.dismiss();openConversation();}}
        aria-expanded={conversationOpen} aria-controls="bench-conversation" aria-label="打开 Agent 对话">Agent 对话</button>
      </div><div className="bench-actions">
      <select aria-label="光路表示" id={networkView?'register-reading-'+caseId:undefined} value={networkView?'network':'path'} onChange={e=>{inspector.dismiss();setRepresentation(e.target.value);}}>
        <option value="path">顺序光路</option><option value="network">端口连接图</option></select>
      <button className="btn btn-secondary" onClick={onUndo} disabled={disabled || !canUndo}
        aria-keyshortcuts="Control+Z Meta+Z" title={['撤销',undoLabel,'(Ctrl / ⌘ Z)'].filter(Boolean).join(' ')}>撤销</button>
      <button className="btn btn-secondary" onClick={onRedo} disabled={disabled || !canRedo}
        aria-keyshortcuts="Control+Shift+Z Meta+Shift+Z Control+Y" title={['重做',redoLabel,'(Ctrl / ⌘ Shift Z)'].filter(Boolean).join(' ')}>重做</button>
      {document.fullscreenEnabled && <button className="btn btn-secondary" onClick={fullscreen}>{full ? '退出全屏' : '全屏'}</button>}
      <button className="btn btn-primary" disabled={disabled || saveBlocked || !edited} onClick={onSave}>{current?.model_kind === 'draft' ? '保存方案版本' : current ? '保存工程版本' : '保存首个版本'}</button>
    </div></header>
    <ProjectVariants caseId={caseId} current={current} headId={versions[0]?.id} dirty={edited} disabled={disabled || saveBlocked} forkDisabled={disabled || forkBlocked}
      draft={draft} readWorkingDraft={readWorkingDraft} pending={unapplied || saveBlocked}
      refreshToken={refreshToken} onFork={onFork} onOpen={onOpenVersion} onAsk={onAsk} onAdopting={onAdopting}/>
    <ComponentLibrary key={caseId} caseId={caseId} current={current} draft={draft} selectedUid={focusElement?.uid} selectedPathUid={ownerUid}
      networkView={networkView} networkSelection={networkSelection} dirty={edited} disabled={disabled || saveBlocked || unapplied} isLatest={isLatest}
      onPlace={async (args,network)=>{
        const result=await onPlace(args);
        const instance=result.instance_uid;
        setRepresentation(network?'network':'path');
        if(network)setNetworkSelection({instance});
        else {
          const pathUid=args.path_uid || 'primary';
          const path=pathUid==='primary' ? result.project.system : result.project.incident_paths.find(p=>p.uid===pathUid);
          const optic=path?.elements.find(el=>el.component_origin?.instance_uid===instance);
          setPathUid(pathUid);setInspectArm(pathUid!=='primary');setOpenRequest(null);
          if(pathUid==='primary')select(optic?.uid);else setArmKey(optic?.uid);
          setPlotTarget({kind:'optic',reveal:true});
        }
        return instance;
      }}/></div>
    <div className="bench-workarea" data-conversation={conversationOpen || undefined}><div className="bench-optics">
    {draftFeedback}
    {networkInputStatus && <p role="status" className="bench-hint" data-network-input-status>{networkInputStatus}
      {' '}<button type="button" className="bench-text-btn" onClick={()=>setNetworkInputStatus('')}>关闭提示</button></p>}
    {heldPositions.length>0 && <section className="bench-held-positions" aria-label="待核对的位置输入">
      <strong>待应用的位置</strong>
      <ul>{heldPositions.map(({item,name,control:heldControl,issue})=><li key={JSON.stringify([item.pathUid,item.elementUid])}>
        <span>{issue || name} · {item.text.trim() ? <>输入「<bdi>{item.text}</bdi>」</> : '输入为空'}
          {heldControl && ' · 已应用 '+positionText(heldControl.value)+' mm'}</span>
        {!issue && <button type="button" className="bench-text-btn" onClick={()=>revealPosition(item)}>回到此位置</button>}
        <button type="button" className="bench-text-btn" disabled={disabled} onClick={()=>onPositionEntry(item.pathUid,item.elementUid,null)}>舍弃此输入</button>
      </li>)}</ul>
    </section>}
    {launchFit && <MeasuredLaunch key={String(launchFit.id)+'/'+current?.revision_id} caseId={caseId} fitId={launchFit.id}
      current={current} dirty={dirty || saveBlocked || unapplied} disabled={disabled} onClose={()=>setLaunchFit(null)}
      onBind={async request=>{
        const saved=await onMeasuredLaunch(request);
        setPathUid(saved.launch_path_uid);setInspectArm(saved.launch_path_uid !== 'primary');setArmKey(null);
        setRepresentation('path');setLaunchFit(null);
      }}/>}
    {networkView ? <NetworkBench inspector={inspector} devices={catalog.devices} caseId={caseId} liveStudies={liveStudies} layout={fullLayout} opticalLayout={layout} project={draft} result={result} disabled={disabled} onEdit={onEdit} beginGesture={beginGesture} notifyInput={setNetworkInputStatus}
      selection={networkSelection} setSelection={setNetworkSelection}
      openReference={({pathUid,scope,elementUid,planeUid:sourcePlane})=>{
        inspector.dismiss();
        if(pathUid!=='primary')setPathUid(pathUid);
        if(scope==='arm'){
          const arm=routes.find(r=>r.path.uid===pathUid);
          if(arm)selectArm(pathUid,arm.rows.findIndex(r=>r.element.uid===elementUid),true);
        } else {
          const row=positions.find(r=>r.element.uid===elementUid);
          if(row)selectMain(row.index,true);
        }
        if(sourcePlane)selectProbe(sourcePlane);
        setRepresentation('path');
      }}
      revision={linkedRevision} previewRevision={current?.revision_id ?? null} onUseQuantity={item=>leaveCanvas(()=>onUseQuantity(item))}
      error={live.error} onRetry={live.retry} onAsk={(focus,port)=>onAsk('关于当前端口光路'+focus+'，',
        {project_revision_id:linkedRevision, project_objects:port ? [port] : [], followCanvas:!port && !linkedRevision})}/> : !positions.length ? <div className="bench-empty-path" id="current-optical-canvas" tabIndex={-1} role="group" aria-label="空白光路">
      <div className="bench-empty-axis" role="img" aria-label="空白光路的轴向坐标，起点为零，向右为正">
        <span>z = 0</span><i aria-hidden="true"/><span>+z</span></div>
      <div className="bench-first-optic"><h3>放置第一个顺序光学元件</h3>{modeControl}{addControl}{sourceControls}
      {!showModel && <><div className="bench-actions">
        <button type="button" className="bench-text-btn" disabled={disabled} onClick={() => onEdit(starterProject('single_pass'))}>建立单程光路示例</button>
        <button type="button" className="bench-text-btn" disabled={disabled} onClick={() => onEdit(starterProject('standing_wave'))}>从平凹腔示例开始</button>
      </div><button type="button" className="bench-text-btn" onClick={() => openFiles(recentFile?.id)}>{recentFile ? '查看最近原件 · '+(recentFile.relative_path || recentFile.filename) : '打开实验资料'}</button></>}
      </div></div> : <><div className="bench-content"><div className="bench-diagram">
      <div className="bench-caption"><strong>{modeControl} · {draft.system.wavelength_nm == null ? '波长待补' : fmt(draft.system.wavelength_nm)+' nm'}</strong>
        <label className="bench-projection">纵轴 <select aria-label="光路图纵轴" value={projection} onChange={e=>setProjection(e.target.value)}>
          <option value="width">束宽 · 相对各自束心</option><option value="tangential">工程光轴 · 切向 T</option><option value="sagittal">工程光轴 · 弧矢 S</option>
        </select></label>
        <span className="bench-state" role="status" data-state={activeEntry?'位置待应用':status}>{activeEntry && ('已应用位置 '+positionText(control.value)+' mm · ')}{pendingProfile ? '正在复算 · 淡线为上一次计算' :
          !cavity && source && (t?.message || s?.message) ? <button type="button" className="bench-quantity"
            onClick={focusMissingSource}>{status} ↗</button> : status}{hasPupil && ' · 无限孔径参考曲线'}{activeEntry && ' · 新位置待应用'}</span>
        <InspectorToggle inspector={inspector} label="所选元件属性"/></div>
      {!!issues.length && <details className="bench-input-issues" open={issues.length <= 3}>
        <summary>待补的模型信息 · {issues.length} 项</summary>
        {issues.map((issue,i)=><button type="button" key={issue.owner+':'+i} onClick={e=>repair(issue,e.currentTarget)}>{issue.summary || issue.message} ↗</button>)}
      </details>}
      {previousGrid && <small className="bench-hint">当前画布重新计算介质内部的束宽。版本 #{current.revision_id} 的原始采样保存在分析记录中。</small>}
      <ProfileLimits planes={result?.planes} arm={armTrace?.axes} beamName={cavity?'腔模':'主光束'} branchName={activeRoute?.path.name}/>
      <div className="bench-plot-scroll" id="current-optical-canvas" tabIndex={-1} role="group" aria-label="光路图"><BeamBenchPlot key={caseId} project={draft} positions={positions} activeIndex={viewingArm?null:selected?.index} onSelect={selectMain}
        onInspect={inspector.open} plotTarget={plotTarget} onSelectProbe={()=>setPlotTarget({kind:'probe',reveal:false})}
        result={result} previous={pendingProfile} projection={projection} saved={compare && modelChanged && trustedBaseline?.mode === draft.system.mode ? trustedBaseline : null} disabled={disabled} beginGesture={beginGesture}
        probe={probe} probeControl={probeControl} probeSample={sample} onEdit={onEdit} routes={routes} activeRoute={activeRoute}
        readings={compare && baseline?.mode === draft.system.mode ? routeReadings : []}
        onOpenReading={r=>leaveCanvas(()=>openFiles(r.attachment_id))}
        armSelection={viewingArm?{uid:ownerUid,index:armRow?.index}:null} onSelectArm={selectArm} /></div>
      <div className="bench-legend">{engineering ? <><span className="bench-key-t">{cavity?'腔模':'主光束'}</span>
        {activeRoute && <span className="bench-key-arm">{activeRoute.path.name}</span>}<span>粗线为束心，细线为束心两侧各一个半径</span>
        {compare && modelChanged && trustedBaseline?.mode===draft.system.mode && <span>点线为已存版本 #{current.revision_id}</span>}</>
      : activeRoute ? <><span className="bench-key-t">{cavity?'腔模':'主光束'}</span>
        <span className="bench-key-arm">{activeRoute.path.name}</span><span>实线 T · 虚线 S · ± 光束半径</span></> :
        <><span className="bench-key-t">切向 T</span><span className="bench-key-s">弧矢 S</span><span>± 光束半径</span></>}
        {!engineering && (t?.profile?.length>1 || armTrace?.axes?.tangential?.profile?.length>1) && <span>浅色填充 · T 向包络</span>}
        {!cavity && [t,s].some(axis=>axis?.width_basis && axis.width_basis!=='gaussian') &&
          <span>主光束宽度 · T {radiusBasisLabels[t?.width_basis] || '待定'} / S {radiusBasisLabels[s?.width_basis] || '待定'}</span>}
        {baseline?.mode === draft.system.mode && (routeReadings.length > 0 || trustedBaseline?.planes.tangential?.profile?.length > 0) && <label><input type="checkbox" checked={compare} onChange={e => setCompare(e.target.checked)} />
          {routeReadings.length ? `${previousGrid?'实测':'基线与实测'} · 工程 #${current.revision_id}（${routeReadings.length} 条）` : '叠加已存基线'}</label>}</div>
      {!engineering && offsetInView && <p className="bench-hint">当前束宽按各自束心对齐。选择工程光轴可查看已登记的偏移与指向。</p>}
      {engineering && result && <div className="bench-center-readout" aria-label="工程光轴束心读数">
        <CenterReadout name={cavity?'腔模':'主光束'} plane={result.planes?.[projection] ?? result.primary_center_axes?.[projection]}/>
        {activeRoute && <CenterReadout name={activeRoute.path.name} plane={armTrace?.axes?.[projection]}/>}
      </div>}
      {!engineering && observed.length > 0 && compare && baseline?.mode === draft.system.mode && <p className="bench-hint">○ 实测 T 半径 / □ 实测 S 半径。点选读数可打开原件。
        {dirty && ' 点位保留采集版本的坐标，虚线基线对应当时的光路。'}</p>}
      {engineering && compare && routeReadings.length>0 && <p className="bench-hint">实测宽度保留在图下的轴向位置。切换到束宽可比较已登记的半径。</p>}
      {!engineering && compare && routeReadings.length>observed.length && <p className="bench-hint">{routeReadings.length-observed.length} 条原始读数无法直接叠加为半径。展开观察面下的「实验读数」可核对波长、宽度约定与登记条件。</p>}
      {beamReadings.error && <p role="alert" className="wb-error">实测读数读取失败。{beamReadings.error} <button type="button" onClick={beamReadings.refresh}>重读实测</button></p>}
      {activeRoute && <p className="bench-hint">支路按展开距离对齐。三角标出光源，菱形标出接入主光路的位置。{pathNotes.map(n=><span key={n}> {activeRoute.path.name} · {n}</span>)}</p>}
      {cavity && (Number.isFinite(t?.m) || Number.isFinite(s?.m)) && <p className="bench-hint">往返矩阵 (A+D)/2 · T {halfTraceReading(t)} · S {halfTraceReading(s)}。
        {status==='存在失稳平面' ? '当前近轴模型没有双轴受限的高斯本征模。' : '绝对值小于 1 时该平面稳定。'}</p>}
      {!cavity && [t,s].some(axis=>Number.isFinite(axis?.collimated_focus_from_exit_mm)) &&
        <p className="bench-hint">平行入射的几何焦点，距末端面 · T {fmt(t?.collimated_focus_from_exit_mm)} mm · S {fmt(s?.collimated_focus_from_exit_mm)} mm。正值向下游。</p>}
      {probe && <div className="bench-probe" aria-label="光路图观察面">
        <label>观察面 <select aria-label="图中观察面" value={probe.uid} onChange={e=>selectProbe(e.target.value)}>
          {planes.map(p=><option value={p.uid} key={p.uid}>{planeNames.get(p.uid)}</option>)}</select></label>
        <button type="button" className="bench-text-btn" title={'在光路图中定位观察面 '+probe.name} onClick={()=>selectProbe(probe.uid)}>
          {probe.position_reference==='fixed_z'?'固定':'随元件'} · {Number.isFinite(sample?.z_mm)
          ? '取样位置 z = '+positionText(sample.z_mm)+' mm'
          : Number.isFinite(probeControl?.value) ? '登记位置 z = '+positionText(probeControl.value)+' mm' : '待定位'}
          {activeRoute && sampleHost && Number.isFinite(sample.depth_mm) && <small className="probe-depth">
            距{sampleHost.element.name}入口 {fmt(sample.depth_mm)} mm</small>}</button>
        <button type="button" className="bench-text-btn" onClick={()=>onAsk('关于观察面「'+planeNames.get(probe.uid)+'」：\n',
          {project_revision_id:linkedRevision,project_objects:[{plane_uid:probe.uid,
            ...(sample?.beams?.some(b=>b.uid===activeRoute?.path.uid) && {beam_uid:activeRoute.path.uid})}]})}>
          引用此观察面问 Agent ↗</button>
        <ProbeReadings beams={sample?.beams} paths={draft.incident_paths || []} branchUid={activeRoute?.path.uid} cavity={cavity}/>
        <StationCenterComparison key={[caseId,linkedRevision,probe.uid].join('/')} caseId={caseId} revision={linkedRevision}
          plane={probe} initialBeam={ownerUid} refreshToken={[refreshToken,savedStudyId].join('/')}
          files={files} onOpenFile={id=>leaveCanvas(()=>openFiles(id))}
          onOpenStudy={id=>leaveCanvas(()=>onOpenStudy(id))} onMethod={method=>leaveCanvas(()=>onUseQuantity({method}))}/>
        {!result && needsPreview && !live.error && <span>正在计算此面的光束半径…</span>}
        {sample?.message && <span role="status">{sample.message}</span>}
      </div>}
      <LiveLayoutStudy caseId={caseId} methods={liveStudies} layout={fullLayout} opticalLayout={layout} selections={selections}
        revision={linkedRevision} previewRevision={current?.revision_id ?? null} disabled={disabled} onOpenStudy={onOpenStudy && (id=>leaveCanvas(()=>onOpenStudy(id)))}
        selectedPlane={fullLayoutIncomplete ? probe?.uid : null} sections={result?.sections}/>
      {sourceControls}
      {needsPreview && live.error && <div role="alert" className="wb-error">无法计算当前参数。{live.error} <button className="btn btn-secondary" onClick={live.retry}>重算预览</button></div>}
    </div><aside className="bench-inspector" aria-label="光学元件属性" {...inspector.props}>
      <InspectorHeading inspector={inspector} title={focusElement?.name || '光源'}/>
      {routes.length>0 && <label>查看光路<select aria-label="查看光路" value={ownerUid} onChange={e=>e.target.value==='primary'?selectMain(selected.index,true):selectArm(e.target.value,null,true)}>
        <option value="primary">{cavity?'主光路 · 谐振腔':'主光路'}</option>{routes.map(r=><option key={r.path.uid} value={r.path.uid}>{r.path.name}</option>)}</select></label>}
      {viewingArm ? <label>观察元件<select aria-label="入射支路上的元件" value={armRow?.index??'source'} onChange={e=>selectArm(ownerUid,e.target.value==='source'?null:Number(e.target.value),true)}>
        <option value="source">光源</option>{activeRoute.rows.map(r=><option key={rowKey(r)} value={r.index}>{r.index+1} · {r.element.name}</option>)}</select></label> :
      <label>观察元件<select aria-label="光路上的元件" value={selected?.index ?? ''} onChange={e => selectMain(Number(e.target.value),true)}>
        {positions.map(p => <option key={p.element.uid || p.index} value={p.index}>{p.index + 1} · {p.element.name}</option>)}</select></label>}
      {viewingArm && !armRow && ['tangential','sagittal'].map(axis=>{
        const origin=activeRoute.path[axis]?.fit_origin;
        return origin && <p className="bench-hint" key={axis}>{axis==='tangential'?'T':'S'} 向源自拟合 #{origin.study_id}
          （原 {origin.measured_axis} 向，z = {origin.coordinate_mm} mm）。{origin.warnings.join(' ')}</p>;
      })}
      {selected && <><div className="bench-measure"><div><small>{isSource?'源面':'出射面'} z = {positionText(focusZ)} mm<br />
        {beamName} · {fmt(wavelength)} nm · {isSource?'输入半径':hasPupil?'无限孔径参考半径':'半径'} / μm</small><strong>T {radiusText[0]}</strong>
        <strong>S {radiusText[1]}</strong></div>
        <svg viewBox="-34 -34 68 68" width="62" height="62" aria-label="T 与 S 计算半径的相对比例"><path d="M-32 0H32 M0 -32V32" stroke="#53646e" fill="none" />
          {radii.every(r => r > 0) && <ellipse rx={27 * radii[0] / spotMax} ry={27 * radii[1] / spotMax} fill="#5eb7c738" stroke="#78d8d5" />}</svg></div>
      {viewingArm ? <button type="button" className="bench-text-btn bench-ask-object" onClick={()=>onAsk('关于支路「'+activeRoute.path.name+'」的'+(armRow?'元件「'+armRow.element.name+'」':'光源')+'：\n',
        {project_revision_id:linkedRevision, project_objects:[{path_uid:ownerUid, element_uid:armRow?.element.uid ?? null}]})}>引用所选支路问 Agent ↗</button> :
        <button type="button" className="bench-text-btn bench-ask-object"
          onClick={()=>onAsk('关于当前光路的「'+selected.element.name+'」：\n',
            {project_revision_id:linkedRevision, element_uids:selected.element.uid ? [selected.element.uid] : []})}>引用所选元件问 Agent ↗</button>}
        {focusElement?.component_origin && <p className="bench-hint">模型来源 · {focusElement.component_origin.name} v{focusElement.component_origin.version}，
          第 {focusElement.component_origin.ordinal} 个元素。参数可在本工程独立调整。{focusElement.component_origin.note}</p>}
        <ElementPowerReading revision={!dirty && current?.revision_id} onUseQuantity={item=>leaveCanvas(()=>onUseQuantity(item))} budgets={result?.sections?.power_budgets} uid={focusElement?.uid} pathUid={viewingArm?ownerUid:null}/>
        {!viewingArm && catalog.models[focusElement?.kind]?.parameters.includes('n') && <SharedMediumReadings
          routes={routes} uid={focusElement.uid} traces={result?.sections?.paths} open={(uid,sharedUid)=>{
            inspector.dismiss();setPathUid(uid);setOpenRequest({uid,sharedUid});
          }}/>}
        <AssemblyEditor key={'assembly:'+ownerUid+':'+selectedKey} project={draft} pathUid={ownerUid}
          rows={viewingArm?activeRoute.rows:positions} index={viewingArm?armRow?.index:selected?.index} disabled={disabled} onEdit={onEdit}/>
        {focusElement && <fieldset className="bench-element-fields" disabled={disabled}>
          <legend>所选元件参数</legend>
          <OpticalElementFields catalog={catalog} element={focusElement} wavelengthNm={wavelength} nameLabel="画布元件名称" prefix="画布元件 " matrixPrefix="画布元件 "
            stockBound={focusPart?.inventory_id!=null} frozenModel={focusPart?.inventory_id!=null && frozenIndexModel(draft,ownerUid,focusElement.uid)} materialReading={materialReadings?.find(row=>row.path_uid===ownerUid && row.element_uid===focusElement.uid)}
            className="bench-parameter-grid" locked={key=>lockedFields.includes(key)} change={updateSelected}/>
          {focusPart && <small>{focusPart.inventory_id!=null && '库存 #'+focusPart.inventory_id+' · '}{focusPart.source}{!!lockedFields.length && ' · 实物参数已锁定。'}</small>}
        </fieldset>}
        {stockObject?.category && <><button type="button" className="bench-text-btn" disabled={disabled || Boolean(inventoryBlock)}
          onClick={()=>{inspector.dismiss();onPickStock(stockObject);}}>更换库存实物</button>
          {inventoryBlock && <small>{inventoryBlock}</small>}</>}
        {control ? <PositionEntry key={'position:'+ownerUid+':'+selectedKey} control={control} entry={entry} pending={Boolean(activeEntry)} inputRef={positionInput} disabled={disabled}
          onEntry={writePosition}
          onApply={z => { writePosition(null); onEdit((viewingArm?moveIncidentElement:moveAlongAxis)(draft, control, z)); }} /> :
          viewingArm ? <p className="bench-hint">{activeRoute.entrance?'接入 '+activeRoute.entrance.element.name+' · z = '+positionText(activeRoute.entrance.start)+' mm':'入口待绑定，请在支路参数中选择均匀传播段。'}</p>
          : <p className="bench-hint">入射面 {positionText(selected.start)} mm。此处由相邻实物或参考面约束，可在工程参数中调整。</p>}
        {activeEntry && <p className="bench-hint">图和读数仍对应 {positionText(control.value)} mm。按 Enter 或 Tab 应用新位置，Esc 恢复。</p>}
        {!viewingArm && adjustableGap(draft, selected.index) && <details className="bench-insert"><summary>在空气段插入元件</summary>
          <label>模型<select value={insertKind} onChange={e => setInsertKind(e.target.value)}>{Object.entries(benchCatalog).map(([key, model]) =>
            <option key={key} value={key}>{model.name}</option>)}</select></label>
          <p className="bench-hint">新元件使用可编辑的示例参数。{benchCatalog[insertKind].length_mm ? '占用 ' + benchCatalog[insertKind].length_mm + ' mm 空气间距。' : '模型的轴向厚度为零。'}</p>
          <button className="btn btn-secondary" disabled={disabled || !(selected.element.length_mm > 0) || selected.element.length_mm < (benchCatalog[insertKind].length_mm || 0) || positions.length > 78} onClick={insert}>在段中点插入{benchCatalog[insertKind].name}</button>
        </details>}
        {focusElement && <ElementRemoval project={draft} pathUid={ownerUid} index={viewingArm ? armRow.index : selected.index} unlocked={unlock} disabled={disabled} onEdit={onEdit}/>}
        {!viewingArm && selected.element.uid && <button className="bench-text-btn" disabled={disabled || routes.length>=12} onClick={()=>addPath(selected.element.uid,true)}>在此接入光束</button>}
      </>}
      {viewingArm ? <button className="bench-text-btn" onClick={()=>{inspector.dismiss();setOpenRequest({uid:ownerUid,index:armRow?.index});}}>编辑这条支路 ↘</button> :
        <button className="bench-text-btn" onClick={() => leaveCanvas(onParameters)}>编辑工程与元件参数 ↘</button>}
      {!viewingArm && addControl}
    </aside></div>
    {stockPicker}
    {[...(result?.model_warnings || []), ...(result?.sections?.model_warnings || [])].filter(note=>networkView
      ? !issues.some(issue=>issue.path?.[0]==='network' && issue.summary===note)
      : !positions.length || !visibleIssues.has(note)).map(note =>
      <p className="bench-hint" role="note" key={note}>{note}</p>)}
    <RegisteredSections catalog={catalog} caseId={caseId} onAsk={onAsk} onOpenStudy={id=>leaveCanvas(()=>onOpenStudy(id))}
      onUseQuantity={item=>leaveCanvas(()=>onUseQuantity(item))} draft={draft} current={current} readWorkingDraft={readWorkingDraft} selectedElement={viewingArm?activeRoute.entrance?.element:selected?.element}
      positions={positions} routes={routes} meterSelection={{pathUid:ownerUid, elementUid:focusElement?.uid}} files={files} onUseFit={id=>setLaunchFit({id})}
      dirty={edited} disabled={disabled} unlock={unlock} result={result} onEdit={onEdit} beginGesture={beginGesture} refreshToken={refreshToken} beamReadings={beamReadings} chosen={planeUid} setChosen={selectProbe}
      chosenPath={activeRoute?.path.uid} setChosenPath={uid=>{setPathUid(uid);setArmKey(null);setOpenRequest(null);setPlotTarget({kind:'probe',reveal:true});}}
      onAddPath={addPath} openRequest={openRequest}/>
    <footer className="bench-footer"><span>{dirty || !current ? '草稿预览' : result?.input_issues?.length ? '已保存 · 模型待补全' : replay?'按已存工程复算':'已存计算'} · 近轴模型{draft.system.mode === 'ring' ? ' · 横轴为光路展开后的几何距离' : ''}</span>
      {!current && !dirty && <button onClick={() => onEdit(starterProject(cavity ? 'single_pass' : 'standing_wave'))}>从{cavity ? '单程' : '平凹腔'}示例开始</button>}

    </footer></>}
    {current && <StudyUsage key={current.revision_id} caseId={caseId} recordId={current.revision_id}
      versions={versions} parentId={current.parent_revision_id} onOpen={(...args)=>leaveCanvas(()=>onOpenStudy(...args))}/>}

    {(error || screenError) && <p role="alert" className="wb-error">{error || screenError}</p>}
    </div><div id="bench-conversation" className="bench-conversation-host" ref={setConversationHost}/></div>
  </section>;
}
