export function cavityAxisState(plane) {
  if (plane?.marginal) return '临界附近';
  if (plane?.stable === false) return '失稳';
  if (plane?.stable === true) return '稳定';
  return '待定';
}

export function cavityStatus(planes) {
  const axes = [cavityAxisState(planes?.tangential), cavityAxisState(planes?.sagittal)];
  if (axes.includes('失稳')) return '存在失稳平面';
  if (axes.includes('临界附近')) return '临界附近';
  if (axes.every(axis => axis === '稳定')) return '双平面稳定';
  return '主光路待补全';
}

export function halfTraceReading(plane) {
  const value = plane?.m;
  if (!Number.isFinite(value)) return '—（' + cavityAxisState(plane) + '）';
  const rounded = Number(value.toPrecision(5));
  let text = String(rounded);
  if (Math.abs(rounded) === 1 && Math.abs(value) !== 1) {
    text = plane.marginal ? '≈' + rounded : String(value);
  }
  return text + '（' + cavityAxisState(plane) + '）';
}
