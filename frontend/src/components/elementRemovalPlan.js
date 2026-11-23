import {adjustableGap, freeSpace, hasEndMirrors, objectRange, withPathElements} from './benchGeometry.js';

export function hasLockedParameters(part) {
  return Boolean(part?.locked_parameters?.length);
}

// Host references always identify the primary train. Private arm identifiers are local to a path.
function referencesElement(ref, owner, pathUid, members) {
  if (!ref || !members.has(ref.element_uid)) return false;
  if (pathUid === 'primary') return ref.scope === 'host';
  return ref.scope === 'arm' && owner === pathUid;
}

export function portReferences(project, matches) {
  const names = new Map((project.network?.components || []).map(c => [c.uid, c.name]));
  return (project.network?.drives || []).filter(matches).map(({at}) =>
    '请先更改端口「' + (names.get(at.component) || at.component) + ' / ' + at.port + '」的输入引用。');
}

export function elementRemoval(project, index, pathUid = 'primary', unlocked = false) {
  const primary = pathUid === 'primary';
  const paths = project.incident_paths || [];
  const path = primary ? null : paths.find(p => p.uid === pathUid);
  const train = primary ? project : path && {pathUid,system:path,parts:path.parts,assemblies:project.assemblies};
  const element = train?.system.elements[index];
  if (!element) return null;
  const {first,last,group} = objectRange(train,index);
  const elements = train.system.elements;
  const pieces = elements.slice(first,last+1);
  const members = new Set(pieces.map(piece=>piece.uid).filter(Boolean));
  const blockers = [];
  if (group && (first < 0 || pieces.length !== group.element_uids.length ||
      pieces.some((piece,i) => piece.uid !== group.element_uids[i])))
    blockers.push('实物组合「' + group.name + '」的成员或顺序不完整，请核对分组。');
  const parts = (train.parts || []).filter(part=>part.element_index>=first && part.element_index<=last);
  const stockIds = [...new Set(parts.map(part=>part.inventory_id).filter(id=>id!=null))];
  if (!unlocked && parts.some(hasLockedParameters))
    blockers.push('请在工程参数中解锁已有元件，再移除这件实物。');
  if (primary) {
    for (const plane of project.observation_planes || []) {
      if (members.has(plane.target_uid) && plane.position_reference !== 'fixed_z')
        blockers.push('请先将观察面「' + plane.name + '」改为固定台面位置、移到另一段，或移除观察面。');
    }
    for (const arm of paths) {
      if (members.has(arm.target_uid) || members.has(arm.through_uid) || (!arm.through_uid && arm.downstream?.some(s => members.has(s.target_uid))))
        blockers.push('请先调整支路「' + arm.name + '」的共享区间或移除此支路。');
    }
  }
  blockers.push(...portReferences(project, drive =>
    referencesElement(drive.path_output, drive.path_output?.path_uid, pathUid, members) ||
    referencesElement(drive.profile?.element_exit, drive.profile?.beam_uid, pathUid, members)));
  const length = pieces.reduce((total,piece)=>total+(piece.length_mm || 0),0);
  const lastHost = primary && elements.length === pieces.length;
  const needsHost = lastHost && Boolean(paths.length || project.observation_planes?.length || project.single_pass_power);
  const endMirror = primary && hasEndMirrors(train.system) && (first === 0 || last === elements.length - 1);
  const air = adjustableGap(train, index);
  const keepAir = length > 0 && (!lastHost || needsHost);
  const canKeep = !endMirror && (!air || lastHost && !needsHost) && (!needsHost || keepAir);
  const canClose = !needsHost;
  if (!canKeep && !canClose) blockers.push('清空主光路前，请移除仍需主光路的观察面、支路及功率预算。');
  const ownedStage = (stage, owner) => referencesElement(stage, owner, pathUid, members);
  const budgets = [[project.single_pass_power, 'primary'], ...paths.map(p => [p.power_budget,p.uid])];
  const laws = budgets.reduce((n,[budget,owner]) => n + (budget?.stages || []).filter(s => ownedStage(s,owner)).length,0);
  const calibrated = primary ? paths.filter(p => p.through_uid &&
    (p.independent_index_uids?.some(uid=>members.has(uid)) || p.downstream?.some(s => members.has(s.target_uid)))).length : 0;
  return {primary,path,train,first,pieces,members,group,stockIds,length,keepAir,canKeep,canClose,endMirror,blockers,laws,calibrated,ownedStage};
}

export function removeOptic(project, index, {pathUid = 'primary', unlocked = false, geometry = 'keep', assemblyUid} = {}) {
  const plan = elementRemoval(project,index,pathUid,unlocked);
  if (!plan || plan.blockers.length || plan.group?.uid !== assemblyUid || (plan.group && !assemblyUid)) return project;
  const allowed = (geometry === 'keep' && plan.canKeep) || (geometry === 'close' && plan.canClose);
  if (!allowed) return project;
  const elements = [...plan.train.system.elements];
  let air = [];
  if (geometry === 'keep' && plan.keepAir) {
    const lengths = plan.length <= 1e6 ? [plan.length] : plan.pieces.map(p=>p.length_mm).filter(n=>n>0);
    air = lengths.map(length=>freeSpace('空气间距',length));
  }
  elements.splice(plan.first,plan.pieces.length,...air);
  function cleanBudget(budget, owner) {
    if (!budget?.stages?.some(s => plan.ownedStage(s,owner))) return budget;
    return {...budget,stages:budget.stages.filter(s => !plan.ownedStage(s,owner))};
  }
  const next = {...project};
  if (plan.group) next.assemblies = project.assemblies.filter(group=>group !== plan.group);
  if (plan.primary) {
    const moved = withPathElements({elements:project.system.elements,parts:project.parts},elements);
    next.system = {...project.system,elements};
    next.parts = moved.parts || [];
    if (project.single_pass_power) next.single_pass_power = cleanBudget(project.single_pass_power,'primary');
  }
  if (project.incident_paths) next.incident_paths = project.incident_paths.map(path => {
    let updated = path === plan.path ? withPathElements(path,elements) : path;
    const budget = cleanBudget(path.power_budget,path.uid);
    if (budget !== path.power_budget) updated = {...updated,power_budget:budget};
    if (plan.primary && path.through_uid) {
      if (path.downstream?.some(s => plan.members.has(s.target_uid)))
        updated = {...updated,downstream:path.downstream.filter(s => !plan.members.has(s.target_uid))};
      if (path.independent_index_uids?.some(uid=>plan.members.has(uid)))
        updated = {...updated,independent_index_uids:path.independent_index_uids.filter(uid => !plan.members.has(uid))};
    }
    return updated;
  });
  return next;
}
