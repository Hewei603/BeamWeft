export const widthNames = {
  radius_1e2:'1/e² 半径', diameter_1e2:'1/e² 全直径', fwhm_diameter:'半高全宽 FWHM',
  d4sigma_diameter:'D4σ 全宽', unknown:'口径未知',
};

export const radiusBasisLabels = {gaussian:'1/e² 半径', second_moment:'二阶矩 2σ', equivalent:'等效半径', clipped:'受孔径限制'};

export function declaredRadiusBasis(axis) {
  if (axis.second_moment_radius) return 'second_moment';
  if (axis.equivalent_radius) return 'equivalent';
  return 'model';
}

export function setRadiusBasis(axis, basis) {
  const result = {...axis, second_moment_radius:basis === 'second_moment'};
  if (basis === 'equivalent') result.equivalent_radius = true;
  else delete result.equivalent_radius;
  return result;
}

export function stationRadiusMarks(beams, branchUid) {
  const marks = [];
  for (const beam of beams || []) {
    if (beam.uid !== 'primary' && beam.uid !== branchUid) continue;
    const {tangential, sagittal} = beam.radii_mm || {};
    if (Number.isFinite(tangential)) marks.push({uid:beam.uid, axis:tangential === sagittal ? 'T/S' : 'T', radius:tangential});
    if (Number.isFinite(sagittal) && sagittal !== tangential) marks.push({uid:beam.uid, axis:'S', radius:sagittal});
  }
  return marks;
}

export function readingAxis(axis, aligned) {
  if (aligned === false) return axis==='tangential'?'仪器轴 1':'仪器轴 2';
  return axis==='tangential'?'T':'S';
}
