// Clustering affects the names above the plot only. Optical coordinates and
// solver inputs retain their original precision, including coincident faces.
export function groupPlotLabels(positions, x, width, anchor = row => (row.start + row.end) / 2) {
  const groups = [];
  for (const row of positions) {
    const at = x(anchor(row));
    const previous = groups.at(-1);
    if (previous && at - previous.first < 74) {
      previous.rows.push(row);
      previous.at = (previous.first + at) / 2;
    } else {
      groups.push({first:at, at, rows:[row]});
    }
  }
  return groups.map((group,i)=>{
    const left=i ? (groups[i-1].at + group.at)/2+2 : 3;
    const right=i+1<groups.length ? (groups[i+1].at + group.at)/2-2 : width-3;
    const boxWidth=Math.min(150,right-left);
    const boxLeft=Math.max(left,Math.min(right-boxWidth,group.at-boxWidth/2));
    return {...group,boxLeft,boxWidth};
  });
}
