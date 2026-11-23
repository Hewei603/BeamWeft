const oldIssue = '旧扫描保留了无效采样位置，未记录具体原因。';

export function scanSamples(scan) {
  if (scan.maps) {
    const issues = new Map();
    for (const group of scan.invalid_groups || []) {
      for (const [y, x] of group.cells) issues.set(y * scan.x.length + x, group.message);
    }
    return scan.y.flatMap((y, iy) => scan.x.map((x, ix) => {
      const stable = scan.maps.stable_both[iy][ix];
      const valid = stable === 0 || stable === 1 || typeof stable === 'boolean';
      return {x, y, mt:scan.maps.tangential[iy][ix], ms:scan.maps.sagittal[iy][ix],
        stable:valid ? Number(stable) : null, status:valid ? 'completed' : 'invalid',
        reason:valid ? '' : issues.get(iy * scan.x.length + ix) || oldIssue};
    }));
  }
  return scan.points.map(point => {
    const partial = point.status === 'partial';
    const reason = partial ? ['tangential','sagittal'].flatMap((axis, i) => point[axis]?.message
      ? [(i ? 'S' : 'T')+' · '+point[axis].message] : []).join('；') : '';
    return {x:point.value, mt:point.tangential?.m, ms:point.sagittal?.m,
      rt:point.tangential?.output?.radius_mm, rs:point.sagittal?.output?.radius_mm,
      stable:typeof point.stable_both_planes === 'boolean' ? Number(point.stable_both_planes) : null,
      status:point.status, reason:point.message || reason || (point.status === 'invalid' ? oldIssue : '')};
  });
}

export function scanCsv(scan, rows) {
  const columns = scan.maps
    ? [['x_'+scan.x_axis.parameter,'x'],['y_'+scan.y_axis.parameter,'y'],['m_T','mt'],['m_S','ms'],['stable_both','stable']]
    : [[scan.parameter,'x'],['m_T','mt'],['m_S','ms'],['radius_T_mm','rt'],['radius_S_mm','rs'],['stable_both','stable']];
  columns.push(['status','status'], ['reason','reason']);
  function escape(value) {
    const cell = String(value ?? '');
    return /[",\r\n]/.test(cell) ? '"'+cell.replaceAll('"','""')+'"' : cell;
  }
  return [columns.map(([label])=>label), ...rows.map(row=>columns.map(([,key])=>row[key]))]
    .map(row=>row.map(escape).join(',')).join('\r\n');
}
