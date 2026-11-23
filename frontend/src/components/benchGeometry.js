import { localId } from '../api/localId.js';
import { unspecifiedSource } from './mainBeamSource.js';
import { profileSegments } from './opticalProfile.js';
// Longitudinal positions are derived from physical segment lengths, in mm.
export function benchPositions(project) {
  let z = 0;
  return project.system.elements.map((element, index) => {
    const start = z;
    z += Number(element.length_mm) || 0;
    return { element, index, start, end: z };
  });
}

// Missing n in older propagation spaces defaults to air. An explicit null is an unknown index.
export function materialSpace(element, part) {
  if (element?.kind !== 'space') return false;
  if (element.medium_role || part?.role === 'medium') return true;
  if (element.index_reference?.model === 'air') return element.n != null && element.n !== 1;
  return Boolean(element.index_reference) || (element.n !== undefined && element.n !== 1);
}

export function withElementParameter(element, key, value, wavelengthNm) {
  const ref=element.index_reference;
  if (key==='index_reference') {
    if (ref?.model===value.model) return element;
    return {...element,n:null,index_reference:value};
  }
  if (key==='n' && ref?.model==='air' && value!==1) {
    return {...element,n:value,index_reference:{model:null,wavelength_nm:wavelengthNm},medium_role:element.medium_role || 'medium'};
  }
  if (key==='n' && ref && ref.wavelength_nm!==wavelengthNm) {
    return {...element,n:value,index_reference:{...ref,wavelength_nm:wavelengthNm}};
  }
  if (key==='n' && ['space','thick_lens','grin'].includes(element.kind) && !ref && wavelengthNm!=null) {
    return {...element,n:value,index_reference:{model:null,wavelength_nm:wavelengthNm}};
  }
  return Object.is(element[key],value) ? element : {...element,[key]:value};
}

export function adjustableGap(project, index) {
  const element = project.system.elements[index];
  const part = project.parts?.find(p => p.element_index === index);
  return element?.kind === 'space' && !element.clear_aperture && !element.component_origin && !materialSpace(element,part) && !assemblyAt(project,index) &&
    part?.inventory_id == null && part?.ownership !== 'owned' && !['crystal','pump_input','laser_output'].includes(part?.role) &&
    !part?.locked_parameters?.includes('length_mm');
}

export function assemblyAt(project, index) {
  const uid = project.system.elements[index]?.uid;
  return uid && (project.assemblies || []).find(group =>
    group.path_uid === (project.pathUid || 'primary') && group.element_uids.includes(uid));
}

export function objectRange(project, index) {
  const elements = project.system.elements;
  const group = assemblyAt(project, index);
  if (!group) return { first: index, last: index };
  return {
    first: elements.findIndex(el => el.uid === group.element_uids[0]),
    last: elements.findIndex(el => el.uid === group.element_uids.at(-1)),
    group,
  };
}

// Exchange adjacent physical objects in path order, retaining member identities and internal order.
export function reorderedElements(project, index, step) {
  const elements = project.system.elements;
  if (!elements[index] || (step !== -1 && step !== 1)) return elements;
  const current = objectRange(project, index);
  const neighbor = step < 0 ? current.first - 1 : current.last + 1;
  if (!elements[neighbor]) return elements;
  const next = objectRange(project, neighbor);
  const [left, right] = step < 0 ? [next, current] : [current, next];
  return [...elements.slice(0, left.first), ...elements.slice(right.first, right.last + 1),
    ...elements.slice(left.first, left.last + 1), ...elements.slice(right.last + 1)];
}

// Membership and locks follow the physical element when a path is reordered or shortened.
export function withPathElements(path, elements) {
  if (!path.parts?.length) return {...path, elements};
  const positions = new Map(elements.map((element, index) => [element.uid || element, index]));
  const parts = path.parts.flatMap(part => {
    const old = path.elements[part.element_index];
    const index = positions.get(old?.uid || old);
    return index == null ? [] : [{...part, element_index:index}];
  });
  return {...path, elements, parts};
}

// An explicitly commanded displacement can open air between separate optics.
// Never split an instance or insert a second interval against a rigid spacer.
function canOpenAir(project, boundary) {
  const elements = project.system.elements;
  if (elements.length >= 80) return false;
  if (boundary === 0 && project.system.mode !== 'single_pass') return false;
  const left = elements[boundary - 1], right = elements[boundary];
  const rigidSpacer = index => {
    const el = elements[index];
    const part = project.parts?.find(p => p.element_index === index);
    return el?.kind === 'space' && !el.clear_aperture && !materialSpace(el, part) && !adjustableGap(project,index);
  };
  if (rigidSpacer(boundary - 1) || rigidSpacer(boundary)) return false;
  const instance = left?.component_origin?.instance_uid;
  if (instance && instance === right?.component_origin?.instance_uid) return false;
  // Older shared paths declare consecutive media by identity. Inserting an
  // unregistered segment inside that chain would disconnect its downstream path.
  return !(project.incident_paths || []).some(path => {
    if (path.through_uid) return false;
    const shared = [path.target_uid, ...(path.downstream || []).map(row => row.target_uid)];
    return shared.some((uid, i) => i > 0 && uid === right?.uid && shared[i-1] === left?.uid);
  });
}

export function positionControl(project, index, positions = benchPositions(project)) {
  const row = positions[index];
  if (!row) return null;
  if (adjustableGap(project, index)) {
    return { value: row.end, min: row.start, max: row.start + 1e6, before: index,
      label: '段末位置', hint: '改变这一空气段的长度，后续元件随段末一起平移。' };
  }
  const { first, last, group: assembly } = objectRange(project, index);
  const atEnd = last === positions.length - 1;
  const leftExists = adjustableGap(project, first - 1);
  const rightExists = !atEnd && adjustableGap(project, last + 1);
  if (!leftExists && !canOpenAir(project, first)) return null;
  if (!atEnd && !rightExists && !canOpenAir(project, last + 1)) return null;
  const leftLength = leftExists ? project.system.elements[first - 1].length_mm : 0;
  const rightLength = rightExists ? project.system.elements[last + 1].length_mm : 0;
  const min = row.start + Math.max(-leftLength, atEnd ? -Infinity : rightLength - 1e6);
  const max = row.start + Math.min(1e6 - leftLength, atEnd ? Infinity : rightLength);
  if (min === max && (!leftExists || (!atEnd && !rightExists))) return null;
  const openAt = !leftExists ? first : !atEnd && !rightExists ? last + 1 : null;
  const group = assembly ? '「' + assembly.name + '」的 ' + assembly.element_uids.length + ' 个元素整体移动。' : '';
  return { value: row.start, min, max, before: leftExists ? first - 1 : undefined,
    after: rightExists ? last + 1 : undefined, openAt, label: '入射面位置',
    hint: group + (openAt != null ? '移开时形成同等长度的空气间距（n = 1）。' : '') +
      (atEnd ? '移动末端元件会改变光路总长。' : '其余元件保持原位。') };
}

export function elementMarkerZ(project,row) {
  return adjustableGap(project,row.index) ? row.end : row.start;
}

export function moveAlongAxis(project, control, position, newGapUid) {
  if (!control || !Number.isFinite(position) || position < control.min || position > control.max) return project;
  const delta = position - control.value;
  if (delta === 0) return project;
  const elements = project.system.elements.map((element, index) => {
    if (index !== control.before && index !== control.after) return element;
    const length = Number(element.length_mm) + (index === control.before ? delta : -delta);
    return { ...element, length_mm: Math.max(0, length) };
  });
  if (control.openAt != null) {
    elements.splice(control.openAt, 0, freeSpace('空气间距', Math.abs(delta), newGapUid));
    const parts = (project.parts || []).map(part => part.element_index < control.openAt ? part :
      {...part, element_index:part.element_index + 1});
    return {...project, parts, system:{...project.system, elements}};
  }
  return { ...project, system: { ...project.system, elements } };
}

export function benchLayout(project) {
  const positions = benchPositions(project);
  const routes = (project.incident_paths || []).map(path => incidentGeometry(path, positions, project.assemblies || []));
  return { positions, routes };
}

function incidentGeometry(path, host, assemblies) {
  const arm = { parts: path.parts || [], pathUid: path.uid, assemblies, system: { mode:'single_pass', elements: path.elements || [] } };
  const rows = benchPositions(arm);
  const entrance = host.find(row => row.element.uid === path.target_uid && (path.through_uid || row.element.kind === 'space'));
  if (!entrance) return { path, arm, rows, shared:[], entrance: null, sourceZ: null, endZ: null, broken: true };
  const sourceZ = entrance.start - (rows.at(-1)?.end || 0);
  if (path.through_uid) {
    const exit = host.find(row => row.element.uid === path.through_uid && row.index >= entrance.index);
    const shared = exit ? host.slice(entrance.index, exit.index+1) : [];
    const broken = !exit || shared.some(row=>!row.element.uid);
    return {path, arm, rows, entrance, sourceZ, shared:broken?[]:shared, endZ:broken?entrance.start:exit.end, broken};
  }
  let last = entrance;
  for (const segment of path.downstream || []) {
    const next = host[last.index + 1];
    if (next?.element.kind !== 'space' || next.element.uid !== segment.target_uid) break;
    last = next;
  }
  return { path, arm, rows, entrance, sourceZ, shared:host.slice(entrance.index,last.index+1), endZ: last.end,
    broken: last.index - entrance.index !== (path.downstream || []).length };
}

export function newIncidentPath(project, targetUid) {
  const paths=project.incident_paths || [];
  if(paths.length>=12 || !targetUid || !project.system.elements.some(el=>el.uid===targetUid)) return null;
  return {uid:localId(), name:'入射光束 '+(paths.length+1), target_uid:targetUid, through_uid:targetUid, wavelength_nm:null, target_n:null,
    tangential:{radius_mm:null, wavefront:'unknown', curvature_mm:null, m2:null}, elements:[], downstream:[]};
}

export function bindSharedRange(path, elements, entryUid, preferredExitUid) {
  const first=elements.findIndex(el=>el.uid===entryUid);
  if(first<0)return path;
  const last=Math.max(first, elements.findIndex(el=>el.uid===preferredExitUid));
  const indexAtWavelength=new Map((path.downstream || []).map(s=>[s.target_uid,s.n]));
  indexAtWavelength.set(path.target_uid,path.target_n);
  const downstream=elements.slice(first+1,last+1)
    .filter(el=>indexAtWavelength.get(el.uid)!=null)
    .map(el=>({target_uid:el.uid,n:indexAtWavelength.get(el.uid)}));
  return {...path,target_uid:entryUid,through_uid:elements[last].uid,target_n:indexAtWavelength.get(entryUid)??null,downstream};
}

export function incidentControl(route, index) {
  if (route?.sourceZ == null) return null;
  const local = positionControl(route.arm, index, route.rows);
  // Moving between two adjustable spaces keeps both the source and the join fixed.
  if (!local || (local.after == null && !(local.openAt != null && local.before != null))) return null;
  return { local, sourceZ: route.sourceZ, pathUid: route.path.uid,
    value: route.sourceZ + local.value, min: route.sourceZ + local.min, max: route.sourceZ + local.max,
    label: '元件入射面 z', hint: local.hint + '支路源面与接入位置保持固定。' };
}

export function moveIncidentElement(project, control, position, newGapUid) {
  const path = project.incident_paths?.find(p => p.uid === control?.pathUid);
  if (!path || !Number.isFinite(position) || position < control.min || position > control.max) return project;
  const arm = { parts: path.parts || [], system: { elements: path.elements } };
  const moved = moveAlongAxis(arm, control.local, Math.max(control.local.min, Math.min(control.local.max, position - control.sourceZ)), newGapUid);
  if (moved === arm) return project;
  return { ...project, incident_paths: project.incident_paths.map(p => p === path ? {...p, elements: moved.system.elements, ...(moved.parts.length ? {parts:moved.parts} : {})} : p) };
}

export function observationControl(plane, positions) {
  if (!plane) return null;
  const fixed = plane.position_reference === 'fixed_z';
  const segment = fixed ? null : positions.find(row => row.element.uid === plane.target_uid && row.element.kind === 'space');
  const coordinate = fixed ? plane.z_mm : plane.depth_mm;
  if ((!fixed && !segment) || !Number.isFinite(coordinate)) return null;
  const min = segment?.start ?? 0;
  const max = segment?.end ?? positions.at(-1)?.end ?? 0;
  return { uid: plane.uid, min, max, value: min + coordinate, fixed };
}

export function moveObservationPlane(project, control, position) {
  if (!control || !Number.isFinite(position) || position < control.min || position > control.max ||
      position === control.value) return project;
  const key = control.fixed ? 'z_mm' : 'depth_mm';
  const value = position - control.min;
  return { ...project, observation_planes: project.observation_planes.map(p => p.uid === control.uid ? {...p, [key]: value} : p) };
}

export function instantiateOptic(preset) {
  return { ...structuredClone(preset), uid: localId() };
}

export function hasEndMirrors(system) {
  const {elements} = system;
  return system.mode === 'standing_wave' && elements.length > 1 &&
    elements[0].kind === 'mirror' && elements.at(-1).kind === 'mirror';
}

export function appendOptic(project, preset) {
  const elements = [...project.system.elements];
  if (elements.length >= 80) return project;
  let index = elements.length;
  if (hasEndMirrors(project.system)) {
    index = objectRange(project, index - 1).first;
  }
  elements.splice(index, 0, instantiateOptic(preset));
  const parts = project.parts.map(p => p.element_index >= index ? {...p, element_index: p.element_index + 1} : p);
  return {...project, system: {...project.system, elements}, parts};
}

export function insertOptic(project, index, model) {
  if (!model || !adjustableGap(project, index) || project.system.elements.length > 78) return project;
  const gap = project.system.elements[index];
  const footprint = model.length_mm || 0;
  if (gap.length_mm <= 0 || gap.length_mm < footprint) return project;
  const half = (gap.length_mm - footprint) / 2;
  const elements = [...project.system.elements];
  const rightGap = { ...gap, uid: localId(), length_mm: gap.length_mm - footprint - half };
  elements.splice(index, 1, { ...gap, length_mm: half },
    instantiateOptic(model),rightGap);
  const parts = project.parts.flatMap(part => {
    if (part.element_index === index) {
      const source = '由原空气段（' + gap.length_mm + ' mm）拆分，本段长度由布局计算。原记录：' + part.source;
      return [{ ...part, source }, { ...part, source, element_index: index + 2 }];
    }
    return [part.element_index > index ? { ...part, element_index: part.element_index + 2 } : part];
  });
  // The old gap's entrance stays on its first half and its exit moves to the second half.
  const paths=project.incident_paths?.map(path=>{
    if(!path.through_uid)return path;
    const entry=project.system.elements.findIndex(el=>el.uid===path.target_uid);
    const exit=project.system.elements.findIndex(el=>el.uid===path.through_uid);
    if(entry<0 || index<entry || index>exit)return path;
    const n=gap.uid===path.target_uid?path.target_n:path.downstream?.find(s=>s.target_uid===gap.uid)?.n;
    return {...path,through_uid:path.through_uid===gap.uid?rightGap.uid:path.through_uid,
      ...(path.independent_index_uids?.includes(gap.uid) && {independent_index_uids:[...path.independent_index_uids,rightGap.uid]}),
      downstream:n==null?(path.downstream||[]):[...(path.downstream||[]),{target_uid:rightGap.uid,n}]};
  });
  const planes=project.observation_planes?.map(plane=>{
    if(plane.position_reference==='fixed_z' || !Number.isFinite(plane.depth_mm) ||
       plane.target_uid!==gap.uid || plane.depth_mm<=half || plane.depth_mm<half+footprint ||
       plane.depth_mm>gap.length_mm)return plane;
    return {...plane,target_uid:rightGap.uid,depth_mm:plane.depth_mm-half-footprint};
  });
  return { ...project, parts, system: { ...project.system, elements },
    ...(paths && {incident_paths:paths}), ...(planes && {observation_planes:planes}) };
}

export function emptyProject() {
  return {title:'光学工程',system:{mode:'single_pass',wavelength_nm:null,source:unspecifiedSource(),elements:[]},
    parts:[],assemblies:[],assumptions:[],notes:''};
}

export function freeSpace(name, length_mm, uid = localId()) {
  return {uid,name,kind:'space',length_mm,n:1,index_reference:{model:'air'}};
}

export function starterProject(mode) {
  const gap = freeSpace('自由传播', 100);
  const cavity = mode === 'standing_wave';
  const mirror = (name, radius_mm) => ({ uid: localId(), name, kind: 'mirror', radius_mm });
  return { title: cavity ? '平凹腔起始模型' : '单程起始模型',
    system: { mode, wavelength_nm: 1064, beam_quality_m2: 1, samples_per_segment: 100,
      ...(cavity ? {} : { input_radius_mm: 0.3, input_curvature_mm: null }),
      elements: cavity ? [mirror('平面镜', null), gap, mirror('凹面镜', 200)] : [gap] },
    parts: [], assemblies: [], assumptions: ['工程从近轴示例创建。初始参数未经实测，请核对当前光源、元件和间距。'], notes: '' };
}

export function savedPreview(analysis) {
  if (!analysis?.planes) return null;
  const planes = Object.fromEntries(Object.entries(analysis.planes).map(([name, data]) => {
    const profile = [], breaks = [];
    for (const segment of profileSegments(data)) {
      if (profile.length) breaks.push(profile.length);
      for (const p of segment) profile.push([p.z_mm, p.radius_mm]);
    }
    return [name, {
      stable: data.stable, marginal: data.marginal, m: data.m, message:data.message,
      beam_quality_m2:data.beam_quality_m2, width_basis:data.width_basis, effective_wavelength_nm:data.effective_wavelength_nm,
      collimated_focus_from_exit_mm:data.collimated_focus_from_exit_mm, collimated_focus_note:data.collimated_focus_note,
      center_profile:data.center_profile, center_profile_breaks:data.center_profile_breaks, center_basis:data.center_basis,
      profile, profile_breaks:breaks, profile_omissions:data.profile_omissions, radii_mm:(data.elements || []).map(p => p.radius_mm),
    }];
  }));
  return { mode: analysis.mode, planes, primary_center_axes:analysis.primary_center_axes, sections: analysis.sections, material_indices:analysis.material_indices, network: analysis.network, status:analysis.status,
    input_issues:analysis.input_issues, model_warnings: analysis.model_warnings || [] };
}
