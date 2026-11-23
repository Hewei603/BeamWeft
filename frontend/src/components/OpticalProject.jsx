import React, { useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link, useSearchParams } from 'react-router-dom';
import { workbenchApi } from '../api/workbench';
import { inventoryApi } from '../api/inventory';
import ScientificPlot from './ScientificPlot';
import OpticalFieldView from './OpticalFieldView';
import OpticalScanView from './OpticalScanView';
import {ProjectRecheck} from './SolverReceipt';
import ComponentPicker from './ComponentPicker';
import OpticalBench from './OpticalBench';
import ElementRemoval from './ElementRemoval';
import OpticalElementFields from './OpticalElementFields';
import SystemBeamFields from './SystemBeamFields';
import {launchSettings, systemForMode} from './mainBeamSource';
import { initialScanAxes, scanAxis, scanFields, validScanPair } from './scanAxes';
import DraftReview from './DraftReview';
import { equalValues, planDraftMerge } from './projectDraft';
import { ProjectHistory } from './projectHistory';
import {setPositionEntry} from './positionIntent';
import { useTabDraftCache } from './useTabDraftCache';
import { appendOptic, emptyProject, withElementParameter } from './benchGeometry';
import { emptyFieldMessage, profileSegments, profileSeries, wavefrontAtFace } from './opticalProfile';
import {cavityStatus} from './cavityStability';
import './OpticalProject.css';
import {engineeringObjects,findStockObject,frozenIndexModel,objectReferences} from './inventoryTargets';
import {componentLabels,connectionAt} from './layoutSelections';
import {downloadJSON as download} from './downloadFile';

const layout = (x, y) => ({ height: 350, autosize: true, margin: { l: 68, r: 25, t: 35, b: 65 }, paper_bgcolor: '#131d2a', plot_bgcolor: '#131d2a', font: { color: '#becbda', size: 11 }, xaxis: { title: { text: x }, gridcolor: '#ffffff10' }, yaxis: { title: { text: y }, gridcolor: '#ffffff10' }, legend: { orientation: 'h', y: 1.15 }, hovermode: 'x unified' });

const getCanvasProject = (id, revision) => workbenchApi.getProject(id, revision, true);

function DesignOriginLink({ origin, caseId }) {
  const independentPump = origin.collection === 'pump_imaging';
  const pump = independentPump || origin.collection === 'candidates.pump';
  const coupled = ['cold_cavity+pump_imaging','candidates+pump'].includes(origin.collection);
  const query = new URLSearchParams({design_revision:origin.content_id});
  if (!independentPump) query.set('design_row', origin.selection);
  query.set('design_target', pump ? 'pump_path' : coupled ? 'coupled_layout' : 'cold_cavity');
  const label = {pump_imaging:'泵浦光路', 'candidates.pump':'泵浦光路 · 候选', 'candidates+pump':'泵浦与冷腔 · 候选',
    'cold_cavity+pump_imaging':'泵浦与冷腔 · 试算点', 'cold_cavity.rows':'冷腔试算', candidates:'探索候选'}[origin.collection] || '设计记录';
  return <p>几何来源 · <Link to={`/cases/${caseId}?${query}`}>
    设计记录 #{origin.content_id} · {label}{!independentPump && ` ${origin.selection}`}
  </Link></p>;
}

function preservedDraftNotice(caseId, requested) {
  return <>此标签页保留了尚未保存的编辑。 <Link target="_blank" rel="noopener" to={`/cases/${caseId}?model_revision=${requested}`}>
    在新标签页打开光路 #{requested}
  </Link></>;
}

function ProjectEvidence({project,caseId}) {
  const paths=[{uid:'primary',name:'主光路',elements:project.system.elements,parts:project.parts},...(project.incident_paths || [])];
  const declared=project.component_context?.unverified_installation_notes;
  const unresolved=new Set(Array.isArray(declared) ? declared : []);
  const current=project.assumptions.filter(line=>!unresolved.has(line));
  const historyToCheck=project.assumptions.filter(line=>unresolved.has(line));
  const unverified=paths.flatMap(path=>(path.parts || []).filter(part=>part.unknowns?.length).map(part=>({
    key:JSON.stringify([path.uid,part.element_index]),label:path.name+' · '+path.elements[part.element_index]?.name,unknowns:part.unknowns,
  })));
  return <details className="op-evidence"><summary>方案假设、待核验项与记录 · {current.length} 项假设{historyToCheck.length>0 && ' · 有装配记录待核验'}</summary>
    {project.component_context?.design_origin && <DesignOriginLink origin={project.component_context.design_origin} caseId={caseId} />}
    <section aria-label="当前建模假设"><h4>当前建模假设</h4>
      {current.length ? <ul>{current.map((item,i)=><li key={i}>{item}</li>)}</ul> : <p>未登记建模假设。</p>}
    </section>
    {historyToCheck.length>0 && <section aria-label="待核验的装配记录"><h4>待核验的装配记录</h4>
      <p>现有历史记录不足以确认这些文字描述的是当前装配。请对照本版工程的安装位和库存标识核实。</p>
      <ul>{historyToCheck.map((line,i)=><li key={i}>{line}</li>)}</ul></section>}
    {unverified.length>0 && <section aria-label="元件待核验项"><h4>元件待核验项</h4>{unverified.map(part=><div key={part.key}>
      <strong>{part.label}</strong><ul>{part.unknowns.map((item,i)=><li key={i}>{item}</li>)}</ul></div>)}</section>}
    {project.notes && <section aria-label="工程备注与装配记录"><h4>工程备注与装配记录</h4><p className="op-log">{project.notes}</p></section>}
  </details>;
}

function ObjectPorts({object, network, inspect}) {
  const names = componentLabels(network.components);
  return <ul className="op-object-ports" aria-label="端口连接">
    {object.ports.map(port => {
      const {peer, drive} = connectionAt(network,{component:object.component.uid,port});
      const label = peer ? (names.get(peer.component) || peer.component) + ' / ' + peer.port
        : drive?.path_output ? '工程光路输入' : drive ? '指定入射光源' : '匹配端接';
      return <li key={port}><button type="button" onClick={() => inspect(object.component.uid,port)}>
        <strong>端口 {port}</strong><span>{label}</span><span aria-hidden="true">↗</span>
      </button></li>;
    })}
  </ul>;
}

export default function OpticalProject({ caseId, fileActions, onUseQuantity, onOpenStudy, savedStudyId, benchHost, files, showProject, openFiles, benchChat, onAsk, onProjectChanged, onModelView, pending = false, activeTask, refreshToken, reportedHead, defaultRevision, availableAlternatives }) {
  const [search, setSearch] = useSearchParams();
  const openingRevision = search.get('model_revision');
  const clearOpeningRevision = () => {
    showProject();
    setSearch(params => {
      const next = new URLSearchParams(params);
      next.delete('model_revision');
      return next;
    }, {replace:true, preventScrollReset:true});
  };
  const [state, setState] = useState(null);
  const [history] = useState(() => new ProjectHistory());
  const [snapshot, setSnapshot] = useState(history.getSnapshot);
  useLayoutEffect(() => history.subscribe(() => setSnapshot(history.getSnapshot())), [history]);
  const {project:draft, dirty, canUndo, canRedo, undoLabel, redoLabel} = snapshot;
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [loadAttempt, setLoadAttempt] = useState(0);
  const returnAfterRetry=useRef(false);
  useLayoutEffect(()=>{
    if(!returnAfterRetry.current || !benchHost) return;
    const next=benchHost.querySelector('.optical-bench') || (error && benchHost.querySelector('.bench-loading button'));
    if(!next) return;
    returnAfterRetry.current=false;
    if(document.activeElement===document.body) next.focus({preventScroll:true});
  },[benchHost,draft,error]);
  const missingHead = state && (reportedHead ?? 0) > (state.revisions[0]?.id ?? 0) ? reportedHead : null;
  const lastRead = useRef(null);
  const activeField = useRef(null), changingField = useRef(null);
  const editingFocus = useRef(null);
  useLayoutEffect(() => {
    const focus = editingFocus.current;
    editingFocus.current = null;
    if (!focus || document.activeElement !== document.body || !focus.surface.getClientRects().length) return;
    if (!focus.control.isConnected || focus.control.matches(':disabled') || !focus.control.getClientRects().length)
      focus.surface.focus({preventScroll:true});
  }, [draft]);
  // Retain the launch only with the unsaved cavity draft produced by a mode
  // switch. A saved cavity has its own eigenmode and carries no launch settings.
  const deferredLaunch = useRef(new WeakMap());
  const [positionEntries, setPositionEntries] = useState([]);
  const positionBuffer = useRef(positionEntries);
  const positionPending = positionEntries.length > 0;
  const editEpoch = useRef(0);
  const mounted = useRef(false);
  useLayoutEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; editEpoch.current++; };
  }, []);
  const [unlock, setUnlock] = useState(false), [newKind, setNewKind] = useState('gap');
  const [map, setMap] = useState(null), [plotType, setPlotType] = useState('radius'), [raw, setRaw] = useState(null);
  const [wavefrontOpen, setWavefrontOpen] = useState(false);
  const [elementsOpen, setElementsOpen] = useState(false);
  const [materialReadings,setMaterialReadings]=useState(null);
  const [rawOpen, setRawOpen] = useState(false);
  const [x, setX] = useState(null), [y, setY] = useState(null);
  const [pickerTarget, setPickerTarget] = useState(null), [changeRefs, setChangeRefs] = useState(null);
  const stockOrigin=useRef(null);
  const [objectKey, setObjectKey] = useState(null), [inventoryState, setInventoryState] = useState({ key: '', items: [], errors: [] });
  const cache = useTabDraftCache(caseId, 'optical');
  const head = useRef(null);
  const [incoming, setIncoming] = useState(null), [draftNotice, setDraftNotice] = useState('');
  const [openedStudy, setOpenedStudy] = useState(null);
  const plot = useRef(null);
  const benchFileActions = useRef(null);
  const settings = useRef(null), elementEditor = useRef(null), elementNodes = useRef({}), addControls = useRef(null);
  const accept = (data, keepHistory = false) => {
    if (!data.element_catalog) throw Error('光学模型目录缺失，请确认前后端来自同一版本后重试。');
    editEpoch.current++;
    const project = data.current?.project || emptyProject();
    if (!keepHistory) deferredLaunch.current = new WeakMap();
    deferredLaunch.current.set(project,null);
    head.current = data.revisions[0]?.id ?? null;
    setOpenedStudy(previous => {
      const selected = data.current;
      if (!previous || !selected) return null;
      if (previous.revision === selected.revision_id) return previous;
      return selected.source_revision_id === previous.revision ? {...previous,revision:selected.revision_id} : null;
    });
    setIncoming(null); setDraftNotice('');
    activeField.current = null;
    positionBuffer.current = [];
    setPositionEntries(positionBuffer.current);
    if (keepHistory) history.saved(project);
    else history.reset(project);
    setState(data); setUnlock(false); setMap(data.scan || null); setRaw(null); setPickerTarget(null);
    if (data.scan) { setX(data.scan.x_axis); setY(data.scan.y_axis); }
    else {
      const [first, second] = initialScanAxes(data.element_catalog, project);
      setX(first); setY(second);
    }
  };
  useEffect(() => {
    const previous=lastRead.current;
    if (openingRevision == null && !missingHead && previous?.caseId===caseId && previous.refreshToken===refreshToken &&
        previous.loadAttempt===loadAttempt && previous.defaultRevision===defaultRevision) return;
    let active = true;
    let retryTimer;
    const epoch = editEpoch.current;
    const load = async () => {
      if (openingRevision != null && (!/^[1-9]\d*$/.test(openingRevision) || !Number.isSafeInteger(Number(openingRevision))))
        throw Error('起始光路版本编号无效。');
      const requested = openingRevision == null ? undefined : Number(openingRevision);
      const initial = await getCanvasProject(caseId, requested);
      const latestId = initial.revisions[0]?.id ?? null;
      const superseded = () => {
        if (!active) return true;
        if (epoch === editEpoch.current) return false;
        // A head announcement can arrive on the final Agent poll. Reconcile
        // after a concurrent edit even if there will be no further polls.
        if (requested == null && latestId > (head.current ?? 0) && latestId !== incoming?.current?.revision_id)
          retryTimer = setTimeout(() => setLoadAttempt(n => n + 1), 200);
        return true;
      };
      if (!active) return;
      if (superseded()) {
        if (requested != null) { setDraftNotice(preservedDraftNotice(caseId, requested)); clearOpeningRevision(); }
        return;
      }
      lastRead.current={caseId,refreshToken,loadAttempt,defaultRevision};
      const readVersion = id => {
        if ((initial.current?.revision_id ?? null) === id) return initial;
        if (id == null) return {...initial, current:null, scan:null, scans:[]};
        return getCanvasProject(caseId, id);
      };
      if (dirty || raw != null || positionPending) {
        if (state?.current?.model_kind === 'draft') {
          head.current = latestId;
          setState(data => ({...data, revisions:initial.revisions}));
        } else if (head.current !== latestId) {
          const latest = await readVersion(latestId);
          if (superseded()) return;
          if (raw == null && !positionPending && equalValues(draft, latest.current?.project)) {
            cache.clear();
            if (requested != null) {
              if (requested !== latestId) cache.store({selectionOnly:true, source:requested});
              accept(initial, requested === latestId);
              clearOpeningRevision();
            } else accept(latest, true);
            setDraftNotice('草稿已采用到当前工程。');
            return;
          } else setIncoming({...latest, draftBase:history.base});
        }
        if (requested != null) {
          if (requested !== state?.current?.revision_id) setDraftNotice(preservedDraftNotice(caseId, requested));
          clearOpeningRevision();
        }
        return;
      }
      // A trial can be the only geometry in a case. Open that persisted revision
      // as a trial without adopting it or displacing a user's selected model.
      const canOpenDefault = defaultRevision && requested == null && latestId == null &&
        !state?.current && !initial.current;
      const recovered = canOpenDefault ? cache.read(initial.draft_scope) : undefined;
      if (canOpenDefault && !recovered) {
        const candidate = await readVersion(defaultRevision);
        if (superseded()) return;
        if (candidate.current?.revision_id === defaultRevision && !candidate.revisions.length) {
          cache.store({selectionOnly:true, source:defaultRevision});
          accept(candidate);
          setError('');
          return;
        }
      }
      if (requested == null && state?.draft_scope === initial.draft_scope && head.current === latestId) {
        // Background readings may refresh the unchanged file while undo or redo is available.
        // A historic or trial selection is also independent of the current main revision.
        if (state.current?.revision_id === initial.current?.revision_id) {
          setState(initial);
        } else {
          setState(data => ({...data, revisions:initial.revisions}));
        }
        return;
      }
      const record = canOpenDefault ? recovered : cache.read(initial.draft_scope);
      if (requested != null && (!record || record.selectionOnly)) {
        cache.clear();
        if (requested !== latestId) cache.store({selectionOnly:true, source:requested});
        accept(initial); setError(''); clearOpeningRevision(); return;
      }
      if (!record) { accept(initial); setError(''); return; }
      // Restore an interrupted edit against its source. Only a concurrent
      // change to the main model requires the contents of the newer head.
      const viewed = await readVersion(record.source);
      if (superseded()) return;
      const independent = viewed.current?.model_kind === 'draft';
      const changedHead = !independent && !record.selectionOnly && record.head !== latestId;
      const latest = changedHead ? (record.source === latestId ? viewed : await readVersion(latestId)) : null;
      if (superseded()) return;
      if (latest && record.raw == null && !record.positionEntries?.length && equalValues(latest.current?.project, record.project)) {
        cache.clear();
        if (requested != null && requested !== latestId) cache.store({selectionOnly:true, source:requested});
        accept(requested == null ? latest : initial); setDraftNotice('已确认上次编辑的内容保存到了当前版本。');
        if (requested != null) clearOpeningRevision();
        return;
      }
      accept(viewed);
      if (record.selectionOnly) { setError(''); return; }
      head.current = independent ? latestId : record.head;
      history.reset(record.base);
      if (record.project.system.mode !== 'single_pass' && record.inactiveSinglePassLaunch)
        deferredLaunch.current.set(record.project,record.inactiveSinglePassLaunch);
      history.edit(record.project, '恢复的草稿');
      setRaw(record.raw);
      positionBuffer.current = record.positionEntries || [];
      setPositionEntries(positionBuffer.current);
      setDraftNotice(requested == null ? '已恢复这个标签页上次编辑的草稿。' : preservedDraftNotice(caseId, requested));
      setError(''); setMap(null);
      if (latest) setIncoming({...latest, draftBase:record.base});
      if (requested != null) clearOpeningRevision();
    };
    load().catch(e => {
      if (!active) return;
      setError(e.message);
      if (openingRevision != null) showProject();
    });
    return () => { active = false; clearTimeout(retryTimer); };
  }, [caseId, refreshToken, loadAttempt, missingHead, openingRevision, defaultRevision]);
  useEffect(() => {
    if (!dirty && raw == null && !positionPending) return;
    const warn = event => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, raw, positionPending]);
  const remember = (project, text) => {
    const source = state?.current?.revision_id ?? null;
    const entries = positionBuffer.current;
    if (project === history.base && text == null && !entries.length) {
      if (source != null && source !== head.current) cache.store({selectionOnly:true, source});
      else cache.clear();
      return;
    }
    const inactiveLaunch = deferredLaunch.current.get(project);
    cache.store({ head: head.current, source, base: history.base, project, raw: text,
      ...(entries.length && {positionEntries:entries}),
      ...(inactiveLaunch && {inactiveSinglePassLaunch:inactiveLaunch}) });
  };
  const updatePositionEntry = (pathUid, elementUid, text, origin) => {
    const entries = setPositionEntry(positionBuffer.current, pathUid, elementUid, text, origin);
    if (entries === positionBuffer.current) return;
    positionBuffer.current = entries;
    setPositionEntries(entries);
    editEpoch.current++;
    remember(history.project, raw);
  };
  const changed = (previous, text = raw) => {
    const next = history.project;
    if (previous === next) return;
    // A model edit can remove its own inspector control. Keep keyboard commands
    // with the editor if the browser drops focus during that render.
    const control = document.activeElement;
    const surface = control?.closest('.optical-bench, .op-workspace');
    editingFocus.current = surface ? {control, surface} : null;
    editEpoch.current++;
    if (previous && next.system.mode !== 'single_pass' && !deferredLaunch.current.has(next))
      deferredLaunch.current.set(next,deferredLaunch.current.get(previous) ?? null);
    remember(next, text);
    setNotice(''); setError(''); setDraftNotice('');
    setMap(next === history.base ? state?.scan || null : null);
  };
  const editRaw = value => { editEpoch.current++; setRaw(value); remember(draft, value); };
  const beginGesture = (base, label) => {
    const token = history.begin(base, label);
    return {
      update(next) {
        const previous = history.project;
        const accepted = history.move(token, next);
        changed(previous);
        return accepted;
      },
      finish() { history.finishGesture(token); },
      cancel() {
        const previous = history.project;
        history.cancelGesture(token);
        changed(previous);
      },
    };
  };
  const edit = (next, label) => {
    const field = changingField.current;
    if (field) {
      let session = activeField.current;
      if (session?.field === field && session.gesture.update(next)) return;
      const gesture = beginGesture(history.project, label || field.getAttribute('aria-label') || '编辑参数');
      activeField.current = {field, gesture};
      gesture.update(next);
    } else {
      const previous = history.project;
      history.edit(next, label);
      changed(previous);
    }
  };
  const captureField = event => {
    const field = event.target;
    if (!field.matches('input:not([type]), input[type=text], input[type=number], textarea')) return;
    changingField.current = field;
    queueMicrotask(() => { if (changingField.current === field) changingField.current = null; });
  };
  const leaveField = event => {
    if (activeField.current?.field !== event.target) return;
    activeField.current.gesture.finish();
    activeField.current = null;
  };
  const travel = direction => {
    if (busy || raw != null) return;
    const previous = history.project;
    history.step(direction);
    changed(previous);
  };
  const historyShortcut = event => {
    if (event.defaultPrevented || event.nativeEvent.isComposing || event.altKey || !(event.ctrlKey || event.metaKey)) return;
    const target = event.target;
    if (target.closest('textarea, select, input, [role=textbox]') || target.isContentEditable) return;
    const key = event.key.toLowerCase();
    const undo = key === 'z' && !event.shiftKey;
    const redo = (key === 'z' && event.shiftKey) || (key === 'y' && event.ctrlKey && !event.shiftKey);
    if (busy || raw != null || !(undo && canUndo || redo && canRedo)) return;
    event.preventDefault();
    travel(undo ? 'undo' : 'redo');
  };
  const applyRaw = async () => {
    const epoch=editEpoch.current;
    setBusy(true);
    try {
      const next = await workbenchApi.normalizeProject(caseId,JSON.parse(raw ?? JSON.stringify(draft)),state?.current?.revision_id);
      if(epoch!==editEpoch.current)return;
      deferredLaunch.current.set(next,null);
      const previous = history.project;
      history.edit(next, '应用工程模型');
      changed(previous, null);
      if (history.project === previous) remember(previous, null);
      setRaw(null); setError('');
    } catch(cause) { if(epoch===editEpoch.current)setError(cause instanceof SyntaxError ? '工程 JSON 语法错误。'+cause.message : cause.message); }
    finally {setBusy(false);}
  };
  const changeMode = mode => {
    if (mode === draft.system.mode) return;
    const next = {...draft, system:systemForMode(draft.system,mode,deferredLaunch.current.get(draft))};
    const leavingSource = draft.system.mode === 'single_pass';
    if (leavingSource) deferredLaunch.current.set(next,launchSettings(draft.system));
    edit(next);
    if (leavingSource) setDraftNotice('腔内光束由本征模决定。保存前切回单程，可恢复刚才的入射光设置。');
  };
  const changeElement = (index, key, value) => edit({ ...draft, system: { ...draft.system, elements: draft.system.elements.map((e, i) => i === index ? withElementParameter(e,key,value,draft.system.wavelength_nm) : e) } });
  const current = state?.current;
  const sourceId = current?.revision_id ?? null;
  const trial = current?.model_kind === 'draft';
  const baseId = trial ? current.base_revision_id : sourceId;
  const rawPending = raw != null;
  const studyOrigin = openedStudy?.revision === sourceId ? openedStudy.origin : current?.study_input;
  useLayoutEffect(() => {
    if (!onModelView || !draft) return;
    onModelView({sourceId, trial, variantName:current?.variant_name, studyOrigin, dirty, rawPending, positionPending, project:draft});
  }, [onModelView, draft, sourceId, trial, current, studyOrigin, dirty, rawPending, positionPending, positionEntries]);
  const analysis = current?.analysis?.mode !== 'port_network' && Object.keys(current?.analysis?.planes || {}).length ? current.analysis : null;
  const savedElements = current?.project.system.elements || [];
  const objects = useMemo(() => engineeringObjects(draft), [draft]);
  const objectNames = useMemo(() => componentLabels(objects.map(o => ({uid:o.key,name:o.title}))), [objects]);
  const selectedObject = objects.find(object => object.key === objectKey) || objects[0];
  const inventoryIdsKey = (selectedObject?.inventoryIds || []).join(',');
  const inventoryItems = inventoryState.key === inventoryIdsKey ? inventoryState.items : [];
  useEffect(() => {
    let alive = true;
    Promise.allSettled((inventoryIdsKey ? inventoryIdsKey.split(',') : []).map(id => inventoryApi.getItem(Number(id))))
      .then(results => { if (alive) setInventoryState({ key: inventoryIdsKey,
        items: results.filter(item => item.status === 'fulfilled').map(item => item.value),
        errors: results.filter(item => item.status === 'rejected').map(item => item.reason?.message || '读取失败') }); });
    return () => { alive = false; };
  }, [inventoryIdsKey]);
  const hasEditorChanges = dirty || rawPending || positionPending;
  const modelLabel = !sourceId ? '未保存的画布' : dirty ? '画布草稿 · 基于 #' + sourceId : '已存版本 #' + sourceId;
  const objectRefs = selectedObject && objectReferences(selectedObject, hasEditorChanges ? null : sourceId);
  const latestRevision = current?.revision_id === state?.revisions[0]?.id;
  const stockBlock = !sourceId || dirty || rawPending || positionPending ? '请先保存当前编辑，再关联库存实物。'
    : !trial && !latestRevision ? '历史主工程需先另存独立方案，或打开最新工程。'
    : incoming ? '请先核对两边的改动。' : '';
  const objectActionsDisabled = busy || Boolean(stockBlock);
  const stockSupported = Boolean(selectedObject?.category);
  const pickStock = (object,area) => {
    if (!object?.category || objectActionsDisabled) return;
    stockOrigin.current=document.activeElement;
    setObjectKey(object.key);setPickerTarget({key:object.key,area});setChangeRefs(null);
  };
  const closeStock = () => {
    setPickerTarget(null);
    requestAnimationFrame(()=>{
      if(stockOrigin.current?.isConnected && stockOrigin.current.getClientRects().length)stockOrigin.current.focus();
      else if(pickerTarget?.area==='bench' && selectedObject?.entries)
        benchFileActions.current?.inspectElement(selectedObject.pathUid,selectedObject.entries[0].index);
    });
  };
  const editObject = (adding = false) => {
    if (!adding && selectedObject?.component) {
      benchFileActions.current?.inspectNetworkComponent(selectedObject.component.uid);
      return;
    }
    if (!adding && selectedObject?.pathUid!=='primary'){
      benchFileActions.current?.inspectElement(selectedObject.pathUid,selectedObject.entries[0].index);
      return;
    }
    if (settings.current) settings.current.open = true;
    if (elementEditor.current) { elementEditor.current.open = true; setElementsOpen(true); }
    requestAnimationFrame(() => (adding ? addControls.current : elementNodes.current[selectedObject?.entries[0]?.element.uid])?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
  };
  const profile = useMemo(() => {
    const quantity = plotType === 'radius' ? 'radius_mm' : 'curvature_mm';
    return Object.entries(wavefrontOpen ? analysis?.planes || {} : {}).map(([axis, data]) => ({
      type:'scatter', ...profileSeries(profileSegments(data, quantity), quantity, plotType === 'radius' ? 1000 : 1),
      name: axis === 'tangential' ? '切向 T' : '弧矢 S',
      line:axis === 'tangential' ? {color:'#80d6c5'} : {color:'#e8b879',dash:'dash'},
    }));
  }, [analysis, plotType, wavefrontOpen]);
  const hasProfile = profile.some(trace => trace.x.length);
  const acceptSaved = (saved, keepHistory = false) => accept({ ...state, current: saved, scan: null, scans: [], revisions: [
    { id: saved.revision_id, title: saved.project.title, change_note: saved.change_note },
    ...(state?.revisions || []).filter(revision => revision.id !== saved.revision_id),
  ] }, keepHistory);
  const previewPlacement = async args => {
    const epoch = editEpoch.current;
    const result = await workbenchApi.previewComponent(caseId, args);
    if (epoch !== editEpoch.current) throw new Error('等待预览时工程已经发生变化，请在当前版本重新插入。');
    edit(result.project);
    return result;
  };
  const mergePlan = useMemo(() => incoming && draft ? planDraftMerge(incoming.draftBase, draft, incoming.current.project) : null,
    [incoming, draft]);
  const applyMerge = project => {
    const target = incoming;
    accept(target);
    if (equalValues(project, target.current.project)) {
      cache.clear(); setDraftNotice('已采用当前已存版本。'); return;
    }
    editEpoch.current++;
    history.edit(project, '合并本地修改'); setMap(null);
    cache.store({ head: head.current, source: head.current, base: history.base, project, raw: null });
    setDraftNotice('已把核对后的改动放入草稿。请查看光路后保存工程版本。');
  };
  const downloadDraft = () => download(`case-${caseId}-draft.json`, {case_id: caseId, source_revision_id: current?.revision_id ?? null,
    base_revision_id: baseId, project: draft, ...(raw != null ? {unapplied_json:raw} : {}),
    ...(positionBuffer.current.length && {unapplied_positions:positionBuffer.current})});
  const inspectEditor = () => {
    const project = history.project;
    if (!project) throw Error(error || '光路仍在读取，请稍后重试。');
    if (raw != null) throw Error('完整模型有尚未应用的文本。请先应用模型编辑，或舍弃文本修改。');
    if (!positionBuffer.current.length) return project;
    if (!benchFileActions.current) throw Error('画布仍在读取，请稍后核对待应用的位置。');
    return benchFileActions.current.positionDraft(positionBuffer.current,project);
  };
  const readEditor = () => {
    const project = inspectEditor();
    if (positionBuffer.current.length) {
      // positionDraft validates the sole active input; other outstanding inputs
      // require an explicit review on their own optics.
      const [{pathUid,elementUid}] = positionBuffer.current;
      updatePositionEntry(pathUid,elementUid,null);
      if (project !== history.project) edit(project);
    }
    return project;
  };
  const checkFileInputs = () => {
    if (!draft) throw Error(error || '光路仍在读取，请稍后保存。');
    if (incoming) throw Error('请先在光路编辑器核对其他窗口提交的版本，再保存文件。');
    if (pending || busy) throw Error('有任务正在更新工程，请在完成后保存文件。');
    try { return inspectEditor(); }
    catch (cause) {
      cause.code = 'unapplied_input';
      throw cause;
    }
  };
  const readWorkingDraft = () => ({project:readEditor(), source_revision_id:sourceId, base_revision_id:baseId});
  const focusEditor = async () => {
    if (raw == null && positionPending) {
      benchFileActions.current?.focusPosition();
      return;
    }
    if (document.fullscreenElement && !document.fullscreenElement.contains(settings.current)) {
      try { await document.exitFullscreen(); }
      catch { setError('请退出光路全屏，再打开工程参数处理未应用的编辑。'); return; }
    }
    showProject();
    if (settings.current) {
      settings.current.open = true;
      if (raw != null) {
        const editor = settings.current.querySelector('.op-raw');
        editor.open = true;
        setRawOpen(true);
        requestAnimationFrame(() => {
          const field=editor.querySelector('textarea');
          field?.focus({preventScroll:true});
          field?.scrollIntoView({block:'center'});
        });
      } else requestAnimationFrame(() => settings.current.scrollIntoView({block:'start'}));
    }
  };
  const save = async (forFile = false, forkName) => {
    const saveTrial = trial || forkName !== undefined;
    if (busy || (incoming && !saveTrial) || raw != null) return;
    let project;
    try { project = readEditor(); }
    catch (cause) {
      if (forFile === true) throw cause;
      setError(cause.message); focusEditor(); return;
    }
    const token = cache.token.current;
    history.finishGesture();
    const epoch = editEpoch.current;
    setBusy(true); setError('');
    try {
      const saved = saveTrial
        ? await workbenchApi.saveVariant(caseId, {project, source_revision_id:sourceId, base_revision_id:baseId,
          variant_name:forkName || current?.variant_name || null})
        : await workbenchApi.saveProject(caseId, { base_revision_id: head.current, project, change_note: current && current.revision_id !== state.revisions[0]?.id ? `从历史版本 #${current.revision_id} 恢复并复算` : '用户修改光学工程并复算', unlock_quote: unlock ? '用户在工程编辑器明确解锁' : '' });
      // The server revision can still satisfy a file save after navigating away.
      // Only the mounted editor owns this tab's current selection and draft.
      if (!mounted.current) return saved;
      if (editEpoch.current !== epoch) {
        if (saveTrial) throw Error(`方案 #${saved.revision_id} 已保存。提交期间的新改动仍在画布上，请核对后保存。`);
        const latest = await getCanvasProject(caseId);
        if (!mounted.current) return saved;
        setIncoming({...latest, draftBase:history.base});
        throw Error('刚提交的版本已保存。提交期间还有新的编辑，请核对画布草稿后再保存文件。');
      }
      cache.clear(token);
      if (saveTrial) {
        cache.store({selectionOnly:true, source:saved.revision_id});
        accept({...state, revisions:incoming?.revisions ?? state.revisions, current:saved, scan:null, scans:[]}, trial && forkName === undefined);
      } else acceptSaved(saved, true);
      setNotice(`${saveTrial ? '方案' : '工程'} #${saved.revision_id} 已保存。` + (saved.analysis?.model_warnings || []).join(' '));
      try { await onProjectChanged?.(); }
      catch (cause) { setError(`工程 #${saved.revision_id} 已保存，附属资料刷新失败，无需重复保存。${cause.message}`); }
      return saved;
    } catch (cause) {
      setError(cause.message);
      if (cause.status === 409) {
        try { const latest = await getCanvasProject(caseId); if (latest.current) setIncoming({...latest, draftBase:history.base}); }
        catch (readError) { setError('草稿已保留。读取新版本失败，重新保存可再次核对。' + readError.message); }
      }
      if (forFile === true) throw cause;
    } finally { setBusy(false); }
  };
  useImperativeHandle(fileActions, () => ({
    startHeadUpdate,
    checkFileInputs,
    async openStudyInput(runId, inputKey, stillSelected) {
      if (!draft || busy || pending) throw Error('工程正在读取或更新，请稍后再打开方案。');
      if (hasEditorChanges || history.getSnapshot().dirty || positionBuffer.current.length)
        throw Error('请先在画布保存或放弃当前修改，再打开另一份方案。');
      const epoch = editEpoch.current;
      setBusy(true);
      try {
        const saved = await workbenchApi.openStudyInput(caseId, runId, inputKey);
        const rid = saved.model_basis.revision_id;
        if (!mounted.current || !stillSelected()) return;
        if (!await loadRevision(rid, epoch, stillSelected))
          throw Error(`方案 #${rid} 已存入“方案与版本”。请重新打开。`);
        if (stillSelected()) {
          setOpenedStudy({revision:rid,origin:saved.study_input});
          showProject();
          benchHost?.focus({preventScroll:true});
          benchHost?.scrollIntoView({block:'start'});
        }
      } finally { if (mounted.current) setBusy(false); }
    },
    useFit(id) { benchFileActions.current?.useFit(id); },
    focusEditor,
    agentModel() {
      if (busy || pending) throw Error('工程正在更新，请在完成后发送。');
      const input = readWorkingDraft();
      if (dirty || input.project !== draft) return {working_draft:input};
      return sourceId ? {revision:sourceId} : {viewed_empty_project:true};
    },
    async prepare() {
      const project = checkFileInputs();
      if (!history.getSnapshot().dirty && project === history.project && positionBuffer.current.length) {
        readEditor();
        return sourceId;
      }
      if (history.getSnapshot().dirty || positionBuffer.current.length) return (await save(true)).revision_id;
      return sourceId;
    },
  }));
  const installMeasuredLaunch = async request => {
    if (!current || dirty || busy || incoming || raw != null || request.revision_id !== sourceId)
      throw Error('请先保存当前光路，确认要接入的工程版本。');
    const epoch=editEpoch.current;
    setBusy(true);
    try {
      const saved=await workbenchApi.measuredLaunch(caseId,request);
      if (!mounted.current) return saved;
      if (editEpoch.current !== epoch) throw Error(`试算 #${saved.revision_id} 已保存，可从“方案与版本”打开。当前画布有后续修改。`);
      cache.store({selectionOnly:true,source:saved.revision_id});
      accept({...state,current:saved,scan:null,scans:[]});
      try { await onProjectChanged?.(); } catch(e) {setError('试算已保存，附属资料刷新失败。'+e.message);}
      return saved;
    } finally {setBusy(false);}
  };
  const loadRevision = async (id, epoch = editEpoch.current, stillSelected = () => true) => {
    if (!mounted.current || epoch !== editEpoch.current) return null;
    const token = cache.token.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const loaded = await getCanvasProject(caseId, id);
      if (!mounted.current || epoch !== editEpoch.current || !stillSelected()) return null;
      cache.clear(token);
      if (loaded.current && loaded.current.revision_id !== loaded.revisions[0]?.id)
        cache.store({selectionOnly:true, source:loaded.current.revision_id});
      accept(loaded);
      return loaded;
    } catch (err) { if (mounted.current) setError(err.message); } finally { if (mounted.current) setBusy(false); }
  };
  function startHeadUpdate(follow = true, independent = false) {
    // Capture before dispatch: a receipt may arrive after further local edits, or even
    // after the user has opened a different document. Never clear that draft on return.
    const epoch = editEpoch.current;
    return async revision => {
      if (!mounted.current) return null;
      const loaded = follow ? await loadRevision(independent ? revision : undefined, epoch) : null;
      if (!mounted.current) return null;
      if (!loaded) {
        setLoadAttempt(attempt => attempt + 1);
        if (follow && epoch !== editEpoch.current)
          setDraftNotice((independent?'独立方案 #':'主工程 #') + revision + ' 已保存。画布保留了提交期间的修改。');
      }
      try { await onProjectChanged?.(); }
      catch (cause) { if (mounted.current) setError('工程 #' + revision + ' 已保存，刷新附属资料失败。' + cause.message); }
      return loaded;
    };
  }
  const scan = async () => {
    const epoch = editEpoch.current;
    setBusy(true); setError('');
    try {
      const result = await workbenchApi.scanProject(caseId, sourceId, {system:current.project.system, x, y});
      if (mounted.current && epoch === editEpoch.current) setMap(result);
    } catch (cause) {
      if (mounted.current && epoch === editEpoch.current) setError(cause.message);
    } finally { if (mounted.current) setBusy(false); }
  };
  const replacing = () => {
    const complete = startHeadUpdate(true,trial);
    return async (saved, item) => {
      const loaded = await complete(saved.revision_id);
      if (loaded?.current?.revision_id !== saved.revision_id) return;
      const message='已将库存 #'+item.id+' '+item.name+' 保存到'+(trial?'独立方案 #':'主工程 #')+saved.revision_id+'。';
      setNotice(message);setDraftNotice(message);
      const owner=saved.replacement.target_path_uid || 'primary';
      const changedObject=findStockObject(engineeringObjects(saved.project),owner,saved.replacement.target_element_uid);
      setObjectKey(changedObject?.key || null);
      if(changedObject)benchFileActions.current?.inspectElement(owner,changedObject.entries[0].index,false,saved.project);
      setChangeRefs({ project_revision_id:saved.revision_id, inventory_ids:[item.id],
        project_objects:changedObject?.entries.map(({element})=>({path_uid:owner,element_uid:element.uid})) || [],
        ...(owner==='primary' && {element_uids:changedObject?.entries.map(e=>e.element.uid) || []}) });
    };
  };
  const catalog = state?.element_catalog;
  const stockObject=objects.find(o=>o.key===pickerTarget?.key);
  const stockPicker=stockObject && !objectActionsDisabled && <ComponentPicker key={sourceId+'-'+stockObject.key}
    caseId={caseId} current={current} target={stockObject} taskPending={pending} onReplacing={replacing} onClose={closeStock}/>;
  const axisEditor = (axis, setAxis, name) => <fieldset><legend>{name} 扫描轴</legend>
    <select aria-label={`${name} 元件`} value={axis.element_index} onChange={event => {
      const index = Number(event.target.value);
      setAxis(scanAxis(catalog, savedElements, index, Object.keys(scanFields(catalog,savedElements[index]))[0], axis.count));
    }}>{savedElements.map((el, i) => Object.keys(scanFields(catalog,el)).length > 0 &&
      <option value={i} key={el.uid || i}>{el.name}</option>)}</select>
    <select aria-label={`${name} 参数`} value={axis.parameter} onChange={event =>
      setAxis(scanAxis(catalog, savedElements, axis.element_index, event.target.value, axis.count))}>
      {Object.entries(scanFields(catalog,savedElements[axis.element_index])).map(([key,field]) =>
        <option value={key} key={key}>{field.label}</option>)}</select>
    {[['start', '起点'], ['stop', '终点'], ['count', '采样数']].map(([key, label]) => <label key={key}>{label}
      <input aria-label={`${name} ${label}`} type="number" step={key === 'count' ? '1' : 'any'} value={axis[key]}
        onChange={e => setAxis({...axis, [key]: e.target.value === '' ? '' : Number(e.target.value)})}/></label>)}</fieldset>;
  if (!state || !draft) {
    const notice=<section className="bench-loading" aria-label="工程画布">
      <h2>光路工作台</h2>
      {error ? <><p role="alert">读取光学工程失败。{error}</p><div className="action-row">
        <button className="btn btn-secondary" onClick={()=>{returnAfterRetry.current=true;setError('');setLoadAttempt(value=>value+1);}}>重试读取工程</button>
        {openingRevision!=null && <button className="btn btn-secondary" onClick={()=>{returnAfterRetry.current=true;clearOpeningRevision();setError('');setLoadAttempt(value=>value+1);}}>返回工程工作台</button>}
      </div></> : <p role="status">读取光学工程…</p>}
    </section>;
    return benchHost ? createPortal(notice,benchHost) : notice;
  }
  const feedback = <>
    {availableAlternatives}
    {pending && <p role="status" className="bench-hint" data-agent-model={activeTask?.model_basis?.revision_id ?? ''}>
      {activeTask ? <>Agent 任务 #{activeTask.id}{activeTask.model_basis?.revision_id
        ? ' 从'+(activeTask.model_basis.kind==='draft'?'快照':'工程')+' #'+activeTask.model_basis.revision_id+' 开始'
        : activeTask.model_basis?.kind==='empty' ? ' 从空白光路开始' : ''}。{activeTask.execution_owner_state==='unverified' && '任务状态待核对。'}</> : '正在提交 Agent 任务。'}
      可以继续编辑、预览和保存。若主工程出现新版本，画布会保留草稿供核对。</p>}
    {draftNotice && <p role="status" className="bench-hint">{draftNotice}</p>}
    {cache.warning && <p role="alert">{cache.warning} <button onClick={downloadDraft}>导出草稿</button></p>}
    {raw != null && <p role="status">完整模型有尚未应用的文本。请先应用模型编辑，或<button onClick={() => editRaw(null)}>舍弃文本修改</button>。</p>}
    {mergePlan && <DraftReview key={incoming.current.revision_id} plan={mergePlan} latestId={incoming.current.revision_id}
      disabled={busy || raw != null || positionPending} onDownload={downloadDraft} onApply={applyMerge} />}
  </>;
  return <section className="op-workspace" tabIndex={-1} aria-label="工程参数编辑器" onKeyDown={historyShortcut} onChangeCapture={captureField} onBlurCapture={leaveField}>
    {benchHost && createPortal(<OpticalBench key={caseId} fileActions={benchFileActions} catalog={catalog} liveStudies={state.live_studies} caseId={caseId} versions={state.revisions} savedStudyId={savedStudyId} onOpenStudy={onOpenStudy} onUseQuantity={onUseQuantity} files={files} refreshToken={refreshToken} draft={draft} current={current} dirty={dirty} disabled={busy}
      studyOrigin={studyOrigin}
      onMaterialReadings={setMaterialReadings} readWorkingDraft={dirty || positionPending || rawPending ? readWorkingDraft : null}
      positionEntries={positionEntries} onPositionEntry={updatePositionEntry}
      onFork={name => save(false, name)} onOpenVersion={loadRevision} onAdopting={startHeadUpdate}
      onMeasuredLaunch={installMeasuredLaunch} onModeChange={changeMode}
      error={error} onEdit={edit} beginGesture={beginGesture} canUndo={canUndo && !rawPending} canRedo={canRedo && !rawPending}
      onUndo={() => travel('undo')} onRedo={() => travel('redo')} undoLabel={undoLabel} redoLabel={redoLabel} onSave={save} unlock={unlock}
      onParameters={() => { showProject(); if (settings.current) { settings.current.open = true; setTimeout(() => settings.current?.scrollIntoView({ block: 'start' }), 0); } }}
      inventoryObjects={objects} inventoryBlock={stockBlock} onPickStock={object=>pickStock(object,'bench')}
      stockPicker={pickerTarget?.area==='bench'?stockPicker:null}
      draftFeedback={feedback} saveBlocked={!!incoming || raw != null} forkBlocked={raw != null} benchChat={benchChat} openFiles={openFiles} isLatest={latestRevision} onPlace={previewPlacement} />, benchHost)}
    <div className="wb-row"><div><span className="wb-eyebrow">OPTICAL ENGINEERING</span><h3>{draft.title || '光学工程'} · {draft.network && !draft.system.elements.length ? '端口光路' : ({ single_pass: '单程光路', standing_wave: '驻波腔', ring: '环形腔' })[draft.system.mode]}</h3></div><select aria-label="工程版本" disabled={busy || hasEditorChanges} value={sourceId || ''} onChange={e => loadRevision(Number(e.target.value))}>
      {!current && <option value="" disabled>尚未保存光路</option>}{trial && <option value={sourceId}>已保存的独立方案 #{sourceId}{current.variant_name && ' · '+current.variant_name}{!state.revisions.length && ' · 尚无主工程版本'}</option>}
      {state?.revisions.map(r => <option value={r.id} key={r.id}>版本 #{r.id} · {r.change_note}</option>)}</select></div>
    {objects.length>0 && <section className="op-object-browser" aria-label="光学工程对象">
      <div className="op-object-heading"><div><h4>光路中的对象</h4><p>{modelLabel}{(rawPending || positionPending) && ' · 有尚未应用的输入'}</p></div>
        {draft.system.elements.length>0 && <button className="btn btn-secondary" disabled={busy} onClick={() => editObject(true)}>＋ 添加顺序元件</button>}</div>
      <div className="op-object-columns"><nav className="op-object-list" aria-label="选择光学对象">{objects.map(object => <button key={object.key} className={object.key === selectedObject?.key ? 'selected' : ''} aria-pressed={object.key === selectedObject?.key} onClick={() => { setObjectKey(object.key); setPickerTarget(null); }}><strong>{objectNames.get(object.key)}</strong><small>{object.component ? `端口光路 · ${object.ports.length} 个端口` : <>{object.pathName} · {catalog.models[object.entries[0].element.kind]?.label}{object.inventoryIds.map(id => ` · 库存 #${id}`).join('')}</>}</small></button>)}</nav>
        {selectedObject && <article className="op-object-inspector"><div className="wb-row"><h4>{objectNames.get(selectedObject.key)}</h4><span>{selectedObject.component ? '端口光路' : selectedObject.entries.some(entry => entry.part?.ownership === 'owned') ? '已有实物' : '计算模型 / 待选实物'}</span></div>
          {selectedObject.component ? <ObjectPorts object={selectedObject} network={draft.network}
            inspect={(uid,port)=>benchFileActions.current?.inspectNetworkComponent(uid,port)}/> : <>
          <p className="op-object-names">{selectedObject.entries.filter(entry => selectedObject.role !== 'crystal' || entry.element.length_mm > 0).map(entry => entry.element.name).join(' → ')}</p>
          {benchHost && <button type="button" className="bench-text-btn"
            onClick={()=>benchFileActions.current?.inspectElement(selectedObject.pathUid,selectedObject.entries[0].index,'plot')}>在光路图中定位</button>}
          <dl><dt>工程身份</dt><dd>{selectedObject.pathName} · {selectedObject.entries.length} 个模型元素</dd><dt>库存关联</dt><dd>{selectedObject.inventoryIds.length ? selectedObject.inventoryIds.map(id => `#${id}`).join('、') : '尚未绑定'} · <Link to="/inventory">查阅元件库 ↗</Link></dd></dl>
          {selectedObject.role === 'crystal' && <details><summary>展开晶体内部结构</summary><ol>{selectedObject.entries.filter(entry => entry.element.length_mm > 0).map(entry => <li key={entry.element.uid}>{entry.element.name} · {entry.element.length_mm} mm · n={entry.element.n ?? '待补'}</li>)}</ol></details>}
          <details><summary>来源与待核实项 · {selectedObject.unknowns.length} 项</summary>{selectedObject.sources.length ? selectedObject.sources.map((source, index) => <p key={index}>{source}</p>) : <p>当前对象没有单独记录参数来源，请补充资料或让 Agent 核对。</p>}{selectedObject.unknowns.length ? <ul>{selectedObject.unknowns.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p>未记录待核实项；这不等于参数已全部测量确认。</p>}</details>
          {inventoryItems.map(item => <details key={item.id}><summary>库存 #{item.id} · {item.name}</summary><p>{item.material || '材料未注明'} · 数量 {item.quantity ?? '未知'} · {item.location || '位置未注明'}</p><p>{item.raw_spec || '没有原始标签文字'}</p>{item.crystal_spec?.coating_note && <p>端面：{item.crystal_spec.coating_note}</p>}</details>)}
          {inventoryIdsKey && inventoryState.key !== inventoryIdsKey && <p className="op-object-help" role="status">读取关联库存资料…</p>}{inventoryState.key === inventoryIdsKey && inventoryState.errors.length > 0 && <p className="wb-error" role="alert">部分库存资料未能读取：{inventoryState.errors.join('；')}</p>}
          {!stockSupported && <p className="op-object-help">此类对象可直接编辑模型参数。库存参数的自动映射尚未覆盖时，请把元件资料和目标交给 Agent 核对建模。</p>}
          </>}
          <div className="op-object-actions"><button className="btn btn-primary" onClick={() => onAsk(`关于「${selectedObject.title}」：\n`, objectRefs)}>向 Agent 提问</button><button className="btn btn-secondary" disabled={busy} onClick={() => editObject()}>编辑参数</button>{stockSupported && <button className="btn btn-secondary" disabled={objectActionsDisabled} onClick={() => pickStock(selectedObject,'objects')}>选择库存元件</button>}</div>
          {stockSupported && stockBlock && <p className="op-object-help">{stockBlock}</p>}
          {current && (dirty || (!trial && !latestRevision)) && <button className="btn btn-secondary" disabled={busy} onClick={() => loadRevision(state.revisions[0]?.id)}>{dirty ? '放弃草稿并读取最新工程' : '读取最新工程'}</button>}
        </article>}
      </div>
      {pickerTarget?.area==='objects' && stockPicker}
    </section>}
    {!current && !dirty && <p className="wb-muted">尚未保存光路版本。<button className="btn btn-secondary" onClick={() => onAsk('请结合本案例的目标和已有信息，建立适用的光学工程；说明已知条件与待核实参数。')}>让 Agent 整理工程</button></p>}
    {error && <p role="alert" className="wb-error">{error}</p>}{notice && <p role="status" className="op-notice">{notice}</p>}
    {changeRefs && <div className="component-success"><p>工程变更已保存。可基于这一版本提出下一步目标，Agent 将收到准确的工程和元件引用。</p><button className="btn btn-secondary" onClick={() => onAsk('基于这次工程变更，我希望：\n', changeRefs)}>围绕这次变更提问 →</button><small>只准备输入内容，由你补充并发送。</small></div>}
    {analysis && <OpticalFieldView analysis={analysis} system={current.project.system} parts={current.project.parts} componentContext={current.project.component_context} scopeWarnings={current.project.component_context?.scope_warnings} revisionLabel={`已存版本 #${sourceId}`} />}
    {(current || dirty) && <ProjectEvidence project={draft} caseId={caseId}/>}
    <details ref={settings} className="op-settings" open={!current || undefined}><summary>编辑工程参数与已有元件约束</summary>
    <fieldset className="op-edit-fields" disabled={busy}>
    <div className="op-controls"><label>工程标题<input aria-label="工程标题" value={draft.title} onChange={e => edit({ ...draft, title: e.target.value })} /></label><label>系统类型<select aria-label="系统类型" value={draft.system.mode} onChange={e => changeMode(e.target.value)}><option value="standing_wave">驻波腔</option><option value="ring">共面环形腔</option><option value="single_pass">单程传播</option></select></label><SystemBeamFields system={draft.system} change={system=>edit({...draft,system})}/></div>
    <details ref={elementEditor} className="op-editor" onToggle={e => { if (e.currentTarget===e.target) setElementsOpen(e.currentTarget.open); }} open={!current || undefined}><summary>元件参数 · {draft.system.elements.length} 个元件（展开编辑）</summary>
    <label className="op-unlock"><input type="checkbox" checked={unlock} onChange={e => setUnlock(e.target.checked)} />本次明确解锁已有元件参数（历史版本保留）</label>
    <div className="op-elements">{elementsOpen && draft.system.elements.map((e, i) => { const part = draft.parts.find(p => p.element_index === i); const locked = key => !unlock && part?.locked_parameters.includes(key); return <article key={e.uid || i} ref={node => { elementNodes.current[e.uid] = node; }}><div className="wb-row"><strong>{i + 1}. {e.name}</strong><small>{catalog.models[e.kind]?.label} {part?.ownership === 'owned' ? '· 已有实物' : ''} {part?.locked_parameters.length ? '· 已锁定' : ''}</small></div>
      <OpticalElementFields catalog={catalog} element={e} wavelengthNm={draft.system.wavelength_nm} nameLabel={`元件 ${i + 1} 名称`} prefix={e.name + ' '} className="op-controls"
        locked={locked} stockBound={part?.inventory_id!=null} frozenModel={part?.inventory_id!=null && frozenIndexModel(draft,'primary',e.uid)}
        materialReading={materialReadings?.find(row=>row.path_uid==='primary' && row.element_uid===e.uid)}
        change={(key, value) => changeElement(i, key, value)} />
      {part && <details><summary>元件依据与未知项</summary><p>{part.source}</p>{part.unknowns.map((u, j) => <p key={j}>{u}</p>)}</details>}<ElementRemoval project={draft} index={i} unlocked={unlock} onEdit={edit}/></article>; })}</div>
    <div ref={addControls} className="wb-row op-actions"><select aria-label="新增元件类型" value={newKind} onChange={e => setNewKind(e.target.value)}>
      {Object.entries(catalog.presets).map(([key, preset]) => <option value={key} key={key}>{preset.name}</option>)}</select>
      <button className="btn btn-secondary" disabled={draft.system.elements.length >= 80}
        onClick={() => edit(appendOptic(draft, catalog.presets[newKind]))}>添加元件</button>
      <small>新元件使用可编辑的示例参数。</small></div>
    </details>
    <button className="btn btn-primary" disabled={busy || !!incoming || raw != null} onClick={save}>{busy ? '计算中…' : trial ? '保存方案版本并复算' : current && !latestRevision ? '从此版本恢复主工程并复算' : '保存新版本并复算'}</button>
    <p className="wb-engine">曲面透射的 R 正号指曲率中心沿传播方向。反射镜正 R 指凹面。</p>
    <details className="op-raw" onToggle={e => { if (e.currentTarget===e.target) setRawOpen(e.currentTarget.open); }}><summary>完整工程模型 / 参数来源 / 假设</summary>{rawOpen && <><textarea aria-label="工程 JSON" disabled={busy} rows={12} value={raw ?? JSON.stringify(draft, null, 2)} onChange={e => editRaw(e.target.value)} /><button className="btn btn-secondary" onClick={applyRaw}>应用模型编辑</button></>}</details>
    </fieldset></details>
    {current && <div className="wb-row"><div><small className="wb-engine">已存版本 #{sourceId}{hasEditorChanges && ' · 画布有后续编辑'}</small>
      {analysis && <h3>{current.project.system.mode === 'single_pass' ? '单程传播' : cavityStatus(analysis.planes)}</h3>}</div><button className="btn btn-secondary" onClick={() => download(`optical-project-${sourceId}.json`, current)}>导出此光路版本 JSON</button></div>}
    {current && <ProjectRecheck key={sourceId} caseId={caseId} revision={sourceId} analysis={current.analysis} edited={hasEditorChanges}/>}
    {analysis && <>
      <details className="op-wavefront" onToggle={e => { if (e.currentTarget===e.target) setWavefrontOpen(e.currentTarget.open); }}><summary>高级分析 · 波前曲率与精确截面数据</summary>
      <div className="wb-row"><select aria-label="工程图表类型" value={plotType} onChange={e => setPlotType(e.target.value)}><option value="radius">光束半径</option><option value="curvature">波前曲率</option></select><button className="btn btn-secondary" disabled={!hasProfile} onClick={() => plot.current?.exportImage('svg').catch(e => setError(e.message))}>导出工程 SVG</button></div>
      {wavefrontOpen && (hasProfile ? <ScientificPlot ref={plot} data={profile} layout={layout('物理位置 z / mm', plotType === 'radius' ? '光束半径 w / μm' : '波前 R / mm')} filename={`project-${current.revision_id}`} />
        : <p>{plotType === 'curvature' && Object.values(analysis.planes).some(p => p.elements?.length)
          ? '记录的截面没有有限的波前半径。平面波前在下表记为 ∞。'
          : emptyFieldMessage(current.project.system, analysis)}</p>)}
      {wavefrontOpen && <details className="op-table"><summary>精确元件位置与模场数据</summary><p>表中波前 R 为元件后的空气等效值。曲线中的分段波前 R 按所在介质显示。</p><div className="wb-table-wrap"><table><thead><tr><th>平面 / 元件</th><th>z / mm</th><th>w / μm</th><th>空气等效 R / mm</th></tr></thead><tbody>{Object.entries(analysis.planes).flatMap(([plane, result]) => result.elements.map((p, i) => <tr key={`${plane}${i}`}><td>{plane === 'tangential' ? 'T' : 'S'} · {p.name}</td><td>{p.z_mm.toFixed(4)}</td>
        <td>{Number.isFinite(p.radius_mm) ? (p.radius_mm * 1000).toFixed(4) : '未知'}</td><td>{wavefrontAtFace(p)}</td></tr>))}</tbody></table></div></details>}
      </details>
      {current.project.system.mode !== 'single_pass' && <section className="op-map">
        <h3>二维稳定图</h3>
        <p>扫描基于已存版本 #{sourceId}。{hasEditorChanges ? '请先应用并保存画布上的编辑，再扫描新参数。' : '选择两个参数，查看 T/S 同时稳定的范围。'}</p>
        {x && y ? <details className="op-scan-config" open={!map || undefined}>
          <summary>扫描设置 · X {x.start}—{x.stop} / Y {y.start}—{y.stop}</summary>
          <div className="op-axes">{axisEditor(x, setX, 'X')}{axisEditor(y, setY, 'Y')}</div>
          <small>首次扫描以当前非零参数值上下 10% 建议探索范围，并按模型边界收窄。可按实物允差或实验目的修改。</small>
          <button className="btn btn-primary" disabled={busy || hasEditorChanges || !validScanPair(x,y)}
            onClick={scan}>计算并保存二维扫描</button>
          {!validScanPair(x,y) && <p role="status">请选择两个不同的元件参数，并为每个参数填写递增的范围和 2 至 101 个采样点。</p>}
        </details> : <p>这个版本可扫描的元件参数不足两个。</p>}
        {map && <><OpticalScanView scan={map} system={current.project.system} recordLabel={'记录 #' + map.artifact_id} />
          <button className="btn btn-secondary" onClick={() => download(`stability-map-${map.artifact_id}.json`, map)}>导出完整扫描数据</button></>}
      </section>}
    </>}
  </section>;
}
