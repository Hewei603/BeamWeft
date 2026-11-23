const launchFields = ['source', 'input_radius_mm', 'input_curvature_mm', 'beam_quality_m2'];

export function unspecifiedSource() {
  return {tangential:{radius_mm:null, wavefront:'unknown', curvature_mm:null, m2:null}};
}

export function launchSettings(system) {
  return Object.fromEntries(launchFields.filter(key => Object.hasOwn(system,key)).map(key => [key,system[key]]));
}

function withoutLaunch(system) {
  const next = {...system};
  for (const key of launchFields) delete next[key];
  return next;
}

export function setMainSource(system, source) {
  return {...withoutLaunch(system), source};
}

export function sourceFromScalars(system) {
  const curvature = system.input_curvature_mm ?? null;
  return {tangential:{
    radius_mm: system.input_radius_mm ?? null,
    wavefront: curvature === null ? 'waist' : 'curved',
    curvature_mm: curvature,
    m2: Object.hasOwn(system,'beam_quality_m2') ? system.beam_quality_m2 : 1,
  }};
}

export function systemForMode(system, mode, retainedLaunch) {
  if (system.mode === mode) return system;
  const next = {...withoutLaunch(system), mode};
  return mode === 'single_pass' ? {...next, ...(retainedLaunch ?? {source:unspecifiedSource()})} : next;
}

export function mainBeamStatus(system, t, s) {
  if (system.wavelength_nm == null) return '几何传输 · 光斑待补波长';
  if (!system.source) return system.input_radius_mm == null ? '光斑待补入射半径' : '单程近轴传播';
  const calculated = plane => !plane?.message && plane?.radii_mm?.some(Number.isFinite);
  const tangential = calculated(t), sagittal = calculated(s);
  if (tangential && sagittal) return '单程近轴传播';
  if (tangential) return '已计算 T · S 待补入射参数';
  if (sagittal) return '已计算 S · T 待补入射参数';
  return '光斑待补入射参数';
}
