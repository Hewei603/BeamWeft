export const basisLabels = {observed:'实测频次或占比', assumed:'假设的工况权重'};
const key = values => JSON.stringify(values);
const refs = originals => originals?.length
  ? {originals:originals.map(({attachment_id,sha256})=>({attachment_id,...(sha256 && {sha256})}))} : {};

export function requestDistribution(value) {
  if (!value) return null;
  return {basis:value.basis, source:value.source, ...refs(value.originals),
    scenarios:value.scenarios.map(({coordinates,weight}) => ({coordinates,weight}))};
}

export function distributionDraft(distribution) {
  return {basis:distribution?.basis || '', source:distribution?.source || '', ...refs(distribution?.originals),
    weights:Object.fromEntries((distribution?.scenarios || []).map(s => [key(s.coordinates),String(s.weight)]))};
}

export function sameDistribution(left,right) {
  if (!left || !right) return !left && !right;
  if (left.basis !== right.basis || left.source !== right.source || left.scenarios.length !== right.scenarios.length) return false;
  const leftRefs=left.originals || [],rightRefs=right.originals || [];
  if (leftRefs.length!==rightRefs.length || leftRefs.some(l=>!rightRefs.some(r=>l.attachment_id===r.attachment_id && l.sha256===r.sha256))) return false;
  const weights = new Map(right.scenarios.map(row => [key(row.coordinates),row.weight]));
  return left.scenarios.every(row => weights.get(key(row.coordinates)) === row.weight);
}

export function scenarioCoordinates(cells,groups,roles) {
  const columns = groups.flatMap((group,i) => group.map(() => ['observed','hidden'].includes(roles[i])));
  const unique = new Map();
  for (const cell of cells) {
    const coords = cell.coordinates.filter((_,i) => columns[i]);
    unique.set(key(coords),coords);
  }
  return [...unique.values()];
}

export function readDistribution(draft,points) {
  if (!Object.hasOwn(basisLabels,draft.basis)) throw Error('请选择实测频次或假设权重。');
  const source = draft.source.trim();
  if (!source) throw Error('请填写这些频次的测量来源或假设依据。');
  const scenarios = points.map((coordinates,i) => {
    const raw = draft.weights[key(coordinates)];
    const weight = Number(raw);
    if (raw == null || !raw.trim() || !Number.isFinite(weight) || weight < 0)
      throw Error('第 '+(i+1)+' 个联合工况需要填写有限的非负权重。没有出现的工况可填 0。');
    if (weight===0 && /[1-9]/.test(raw.split(/[eE]/)[0]))
      throw Error('第 '+(i+1)+' 个权重小于数字表示范围。请按相同比例放大全部权重后再试。');
    return {coordinates,weight};
  });
  if (!scenarios.some(s => s.weight > 0)) throw Error('至少一个工况的权重需要大于零。');
  return {basis:draft.basis,source,scenarios,...refs(draft.originals)};
}

export function percent(value,exact) {
  if (!Number.isFinite(value)) return '待核对';
  const rounded = Number((value*100).toPrecision(6));
  if (!rounded && exact && !/^0(?:\/|$)/.test(exact)) return '>0%';
  if (rounded===100 && (value<1 || (exact && !['1','1/1'].includes(exact)))) return '<100%';
  return rounded+'%';
}
