// Include the physical zero in both radiometric views. Corrected camera samples can be negative.
export function mapScales(grid) {
  const axes=[grid?.x_edges_mm,grid?.y_edges_mm];
  if(!axes.every(axis=>Array.isArray(axis) && axis.length>=2 &&
    axis.every((value,i)=>Number.isFinite(value) && (!i || value>axis[i-1]))))return null;
  const [columns,rows]=axes.map(axis=>axis.length-1);
  const {reference,observed,residual}=grid;
  if(![reference,observed,residual].every(matrix=>Array.isArray(matrix) && matrix.length===rows &&
    matrix.every(row=>Array.isArray(row) && row.length===columns)))return null;
  let low=0, high=0, magnitude=0, finite=false, gaps=false;
  for(let i=0;i<rows;i++) {
    for(let j=0;j<columns;j++) {
      const a=reference[i][j],b=observed[i][j],d=residual[i][j];
      if(a===null && b===null && d===null) {gaps=true;continue;}
      if(!Number.isFinite(a) || !Number.isFinite(b) || !Number.isFinite(d))return null;
      low=Math.min(low,a,b);high=Math.max(high,a,b);magnitude=Math.max(magnitude,Math.abs(d));finite=true;
    }
  }
  return finite ? {density:[low,high],difference:[magnitude===0?0:-magnitude,magnitude],gaps} : null;
}

export function cellAt(edges,coordinate) {
  if (!Number.isFinite(coordinate) || coordinate<edges[0] || coordinate>edges.at(-1)) return -1;
  let low=0, high=edges.length-1;
  while (high-low>1) {
    const mid=Math.floor((low+high)/2);
    if (coordinate<edges[mid]) high=mid;
    else low=mid;
  }
  return low;
}

// Keep a millimetre equally long on either axis without letting the plotting library alter the shared ranges.
export function mapDomains(x,y,width,height) {
  const ratio=Math.abs((x[1]-x[0])/(y[1]-y[0]));
  const widthFraction=Math.min(1,ratio*height/width), heightFraction=Math.min(1,width/(ratio*height));
  return {x:[(1-widthFraction)/2,(1+widthFraction)/2],y:[(1-heightFraction)/2,(1+heightFraction)/2]};
}
