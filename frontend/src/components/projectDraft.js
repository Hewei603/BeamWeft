// Drafts belong to this browser tab, case and authorized server identity.
// Keep drag updates in memory and serialize the last edit when the pointer settles.
const buffered = new Map();

export function draftStorageKey(server, caseId, scope, kind = 'optical') {
  return scope ? ['laserclaw-' + kind + '-draft-v1', encodeURIComponent(server), caseId, scope].join(':') : null;
}

export function bufferDraft(key, record) {
  if (key) buffered.set(key, record);
}

export function flushDraft(key) {
  if (!key || !buffered.has(key)) return;
  window.sessionStorage.setItem(key, JSON.stringify(buffered.get(key)));
  buffered.delete(key);
}

export function readDraft(key) {
  if (!key) return null;
  if (buffered.has(key)) return buffered.get(key);
  const text = window.sessionStorage.getItem(key);
  if (text == null) return null;
  return JSON.parse(text);
}

export function clearDraft(key) {
  if (!key) return;
  buffered.set(key, null);
  window.sessionStorage.removeItem(key);
  buffered.delete(key);
}

export function equalValues(a, b) {
  if (a === b) return true;
  if (a == null || b == null || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(key => Object.hasOwn(b, key) && equalValues(a[key], b[key]));
}

const opticsFields = ['system', 'incident_paths', 'observation_planes', 'single_pass_power', 'network', 'parts', 'assemblies', 'component_context'];
const titles = { title: '工程标题', notes: '工程备注', assumptions: '建模假设', single_pass_power: '主光路功率预算',
  observation_planes: '观察面', incident_paths: '入射支路', network:'端口网络' };
const without = (obj, keys) => Object.fromEntries(Object.entries(obj).filter(([k]) => !keys.includes(k)));
const optics = project => Object.fromEntries(opticsFields.filter(key => Object.hasOwn(project, key)).map(key => [key, project[key]]));
const ids = elements => (elements || []).map(e => e.uid);
function bindings(project) {
  return {
    mode: project.system.mode, elements: ids(project.system.elements), parts: project.parts || [],
    inventory: project.component_context || {}, assemblies: project.assemblies || [],
    paths: (project.incident_paths || []).map(p => [p.uid, p.target_uid, p.through_uid, ids(p.elements), p.parts || [], (p.downstream || []).map(s => s.target_uid)]),
    planes: (project.observation_planes || []).map(p => [p.uid, p.target_uid]),
  };
}

export function planDraftMerge(base, mine, latest) {
  const groups = [];
  const add = (key, label, get, put) => {
    const before = get(base), local = get(mine), incoming = get(latest);
    const localChanged = !equalValues(before, local), remoteChanged = !equalValues(before, incoming);
    if (!localChanged && !remoteChanged) return;
    const kind = !localChanged ? 'remote' : !remoteChanged ? 'local' : equalValues(local, incoming) ? 'same' : 'conflict';
    groups.push({key, label, before, local, incoming, kind, put});
  };
  const set = (obj, key, value) => { if (value === undefined) delete obj[key]; else obj[key] = structuredClone(value); };
  const field = key => add(key, titles[key] || key, p => p[key], (p, v) => set(p, key, v));
  const meta = new Set([base, mine, latest].flatMap(p => Object.keys(p).filter(k => !opticsFields.includes(k))));
  for (const key of meta) field(key);
  // Parts use positional indices and branch links refer to element identities.
  // If bindings or order changed, choosing a whole optical model keeps them coherent.
  const oldBindings = bindings(base);
  if (![mine, latest].every(p => equalValues(bindings(p), oldBindings)) || oldBindings.elements.some(uid => !uid)) {
    add('optics', '光路及其元件、库存和观察面关联', optics, (p, model) => {
      for (const key of opticsFields) set(p, key, model[key]);
    });
  } else {
    add('source', '系统波长与入射设置', p => without(p.system, ['elements']), (p, values) => {
      p.system = { ...structuredClone(values), elements: p.system.elements };
    });
    base.system.elements.forEach((e, i) => add('element:' + e.uid, e.name, p => p.system.elements[i],
      (p, value) => { p.system.elements[i] = structuredClone(value); }));
    for (const [i, arm] of (base.incident_paths || []).entries()) {
      add('arm:' + arm.uid, arm.name, p => p.incident_paths[i], (p, value) => { p.incident_paths[i] = structuredClone(value); });
    }
    for (const key of ['observation_planes', 'single_pass_power', 'network']) field(key);
  }
  return {groups, build(choices = {}) {
    const result = structuredClone(latest);
    for (const g of groups) {
      if (g.kind === 'conflict' && !['local','remote'].includes(choices[g.key])) throw new Error('请选择「' + g.label + '」要采用的版本。');
      if (g.kind === 'local' || g.kind === 'conflict' && choices[g.key] === 'local') g.put(result, g.local);
    }
    return result;
  }};
}
