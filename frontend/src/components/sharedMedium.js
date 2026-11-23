// An old explicit path stores its geometry in downstream. An endpoint-bound
// path uses the same array only for independently supplied optical values.
export function withSharedIndex(path, uid, value, independent = true) {
  const indices = new Set(path.independent_index_uids || []);
  if (independent) indices.add(uid);
  else indices.delete(uid);
  const next = {...path, independent_index_uids:[...indices]};
  if (Number.isFinite(value)) next.shared_index_wavelength_nm = path.wavelength_nm ?? null;
  if (path.target_uid === uid) return {...next, target_n:value};
  const rows = path.downstream || [];
  if (!path.through_uid) return {...next, downstream:rows.map(row=>row.target_uid === uid ? {...row,n:value} : row)};
  const other = rows.filter(row=>row.target_uid !== uid);
  return {...next, downstream:value == null ? other : [...other,{target_uid:uid,n:value}]};
}

export function withIncidentWavelength(path, wavelength) {
  if (path.wavelength_nm === wavelength) return path;
  const independent = new Set(path.independent_index_uids || []);
  if (Number.isFinite(path.target_n)) independent.add(path.target_uid);
  for (const stage of path.downstream || []) {
    if (Number.isFinite(stage.n)) independent.add(stage.target_uid);
  }
  return {...path, wavelength_nm:wavelength, shared_index_wavelength_nm:wavelength,
    independent_index_uids:[...independent], target_n:null,
    downstream:path.through_uid ? [] : (path.downstream || []).map(row=>({...row,n:null}))};
}

const sources = {
  branch_value:'支路填写值',
  material_dispersion:'材料色散',
  host_same_wavelength:'同波长主路值',
  independent:'待独立标定',
  legacy_explicit:'此逐段路径需填写支路值',
  missing:'缺少该波长的折射率依据',
  out_of_range:'支路波长超出材料模型范围',
  directional_model:'需确认该支路的偏振和光轴方向',
  wavelength_missing:'请先填写支路波长',
};

export function sharedIndexReading(row) {
  if (!row) return '尚无此段计算记录';
  const value = Number.isFinite(row.n) ? 'n = '+Number(row.n.toPrecision(8)) : 'n 待定';
  const source = row.index_source==='material_dispersion' && row.index_form==='constant'
    ? '目录常数模型' : sources[row.index_source] || '原记录未注明来源';
  return [value, source, row.index_model].filter(Boolean).join(' · ');
}
