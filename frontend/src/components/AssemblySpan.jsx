import React from 'react';
import { assemblyAt } from './benchGeometry';

export default function AssemblySpan({project, rows, index, x, y, visible}) {
  const group = assemblyAt(project,index);
  if (!group) return null;
  const first = rows.find(r=>r.element.uid===group.element_uids[0]);
  const last = rows.find(r=>r.element.uid===group.element_uids.at(-1));
  if (!first || !last || !visible(first.start,last.end)) return null;
  const left = x(first.start), right = Math.max(left+3,x(last.end));
  return <g className="bench-assembly-span" role="img" aria-label={group.name+' · '+group.element_uids.length+' 个元素'} pointerEvents="none">
    <title>{group.name} · {first.start}–{last.end} mm（沿本光路）</title>
    <path d={`M${left} ${visible(first.start,first.start)?y-4:y}V${y}H${right}V${visible(last.end,last.end)?y-4:y}`} fill="none" stroke="currentColor" strokeWidth="2"/>
  </g>;
}
