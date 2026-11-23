import React from 'react';

// Paraxial power fixes the direction of the arrows, not the shapes of the glass surfaces.
export default function ThinLensSymbol({x, y, halfHeight, focal, className}) {
  const upper = y-halfHeight, lower = y+halfHeight;
  if (!Number.isFinite(focal) || focal === 0) {
    return <path className={className+' thin-lens-symbol'} strokeWidth={2} strokeDasharray="3 3"
      d={`M${x} ${upper}V${lower}`}><title>{focal===0?'薄透镜 · 焦距须为非零值':'薄透镜 · 焦距待确认'}</title></path>;
  }
  const head = Math.min(8, halfHeight*.45), wing = head*.72;
  const inset = focal < 0 ? head : 0;
  const start = upper+inset, end = lower-inset;
  const shoulder = focal < 0 ? -head : head;
  return <path className={className+' thin-lens-symbol'} strokeWidth={2}
    d={`M${x} ${start}V${end} M${x-wing} ${start+shoulder}L${x} ${start}l${wing} ${shoulder} M${x-wing} ${end-shoulder}L${x} ${end}l${wing} ${-shoulder}`}>
    <title>{focal < 0 ? '发散' : '会聚'}薄透镜 · f = {focal} mm</title>
  </path>;
}
