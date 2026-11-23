import {differentAtSameZ} from './plotViewport.js';
import {cavityStatus} from './cavityStability.js';

// A profile can contain sampled propagation intervals and isolated reference planes.
// Old revisions can also contain only the solved input and element exit states.
export function profileSegments(plane, quantity = 'radius_mm') {
  const valid = p => Number.isFinite(p?.z_mm) && Number.isFinite(p[quantity]);
  if (!plane.samples?.length)
    return [plane.input, ...(plane.elements || [])].filter(valid).map(p => [p]);

  const segments = [];
  const breaks = new Set(plane.profile_breaks);
  let segment;
  for (const [index, point] of plane.samples.entries()) {
    if (!valid(point)) { segment = null; continue; }
    const prior = segment?.at(-1);
    const value = point[quantity], old = prior?.[quantity];
    const distinctFace = prior && differentAtSameZ(point.z_mm,value,prior.z_mm,old);
    // A finite Gaussian beam changes the sign of R through a pole, never through R=0.
    const pole = prior && quantity === 'curvature_mm' && Math.sign(old) !== Math.sign(value);
    if (!segment || breaks.has(index) || distinctFace || pole) {
      segment = [];
      segments.push(segment);
    }
    segment.push(point);
  }
  return segments;
}

export function profileSeries(segments, quantity, scale = 1) {
  const x = [], y = [];
  const isolated = segments.map(s => s[0].z_mm === s.at(-1).z_mm);
  const hasLines = isolated.includes(false), hasPoints = isolated.includes(true);
  const sizes = hasLines && hasPoints ? [] : null;
  for (const [i, segment] of segments.entries()) {
    if (i) { x.push(null); y.push(null); sizes?.push(0); }
    for (const point of segment) {
      x.push(point.z_mm);
      y.push(point[quantity] * scale);
      sizes?.push(isolated[i] ? 6 : 0);
    }
  }
  return {x, y, mode: hasLines ? (hasPoints ? 'lines+markers' : 'lines') : 'markers',
    ...(hasPoints && {marker:{size:sizes || 6}})};
}

export function wavefrontAtFace(point) {
  if (Number.isFinite(point.curvature_mm)) return point.curvature_mm.toFixed(4);
  if (point.wavefront === 'waist' || (point.curvature_mm === null && point.wavefront !== 'unknown'
    && point.q_real_mm === 0 && point.q_imag_mm > 0)) return '∞';
  return '未知';
}

export function emptyFieldMessage(system, analysis) {
  const cavity = system.mode !== 'single_pass';
  const axes = Object.values(analysis.planes || {}).filter(Boolean);
  const source = system.source || system.input_beam;
  const stability = cavity && cavityStatus(analysis.planes);
  if (stability === '存在失稳平面') return '谐振腔存在失稳平面。可调整间距或光焦度后重新检查稳定性。';
  if (stability === '临界附近' && axes.some(p => p.marginal && !p.stable))
    return '谐振腔位于稳定边界附近，当前模型无法确定受限基模的光斑。';
  if (source && axes.some(p => p.message)) {
    return [...new Set(axes.map(p => p.message).filter(Boolean))].join(' ');
  }
  const missing = [];
  if (system.wavelength_nm == null) missing.push('工作波长');
  if (!cavity && !source && system.input_radius_mm == null && !system.input_field) missing.push('入射光束半径');
  if (missing.length) {
    const geometry = stability === '双平面稳定' ? '谐振腔几何稳定。' : axes.length ? '已算出几何传输矩阵。' : '';
    return geometry + '补充' + missing.join('和') + '后可计算光斑。';
  }
  return '此结果未记录可绘制的光斑采样。可查看模型参数和计算记录。';
}
