import React, { useRef, useState } from 'react';
import { adjustableGap, incidentControl, moveIncidentElement } from './benchGeometry';
import { axialPixel, displayProfile, positionText, profileInView } from './plotViewport';
import {useAxialGesture} from './usePointerGesture';

const X0 = 69, X1 = 898, axisY = 106;
const colors = { primary: '#71d5ce', incident: '#f3ad72' };
const fmt = v => Number(v.toPrecision(4));

export default function SectionPlot({ draft, result, host, route, section, readings, disabled, beginGesture }) {
  const svg = useRef(null);
  const gesture = useAxialGesture(disabled, draft, beginGesture);
  const [fixed, setFixed] = useState(null);
  const path = route?.path;
  const anchor = route?.entrance, components=route?.rows || [], sourceZ=route?.sourceZ ?? 0;
  const trace = result?.sections?.paths.find(p => p.uid === path?.uid);
  const lines = [];
  for (const axis of ['tangential', 'sagittal']) {
    for (const [type, plane] of [['primary', result?.planes[axis]], ['incident', trace?.axes?.[axis]]]) {
      if (plane?.profile?.length) lines.push({type, axis, plane});
    }
  }
  const begin = Math.min(0, sourceZ), end = host.at(-1)?.end || 0;
  const [lo, hi] = fixed || [begin, end];
  const view = {start:lo,end:hi};
  const x = z => axialPixel(z,view,X0,X1);
  const predictions = (section?.beams || []).filter(b=>b.uid==='primary' || b.uid===path?.uid)
    .flatMap(b=>Object.entries(b.radii_mm).filter(([axis])=>b.radius_origin?.[axis]!=='source_input')
      .map(([axis,radius])=>({beam:b.uid,axis,radius})));
  const comparableRadii = readings.flatMap(r=>Object.keys(r.measured_minus_model_mm).map(axis=>r.radii_mm[axis]));
  const largest = Math.max(0,...predictions.map(p=>p.radius),...comparableRadii,
    ...lines.map(line=>line.plane.profile.reduce((m,p)=>Math.max(m,p[1]),0)));
  const y = r => axisY-(largest ? r/largest*65 : 0);
  const start = (event, comp) => {
    const control = incidentControl(route, comp.index);
    if (gesture.beginAxis(event, svg.current, control, (hi-lo)/(X1-X0), moveIncidentElement, () => setFixed(null))) {
      setFixed([lo,hi]);
    }
  };
  return <svg ref={svg} className="section-plot" tabIndex={-1} viewBox="0 0 945 219" aria-label="主光束与入射光束在同一物理轴上的半径">
    <text x={X0} y="18">光束半径 / μm</text>
    {[0,0.5,1].map(f=><g key={f}><line x1={X0} x2={X1} y1={axisY-f*65} y2={axisY-f*65} className="bench-grid"/>
      <text x={X0-8} y={axisY-f*65+4} textAnchor="end">{largest || f===0 ? fmt(largest*1000*f) : '—'}</text></g>)}
    {Number.isFinite(section?.z_mm) && <line x1={x(section.z_mm)} x2={x(section.z_mm)} y1="29" y2="164" stroke="#dce9f3" strokeDasharray="3 3"/>}
    {lines.map(({type,axis,plane})=>{
      const parts=profileInView(plane.profile,plane.profile_breaks,view);
      const d=parts.filter(part=>part.length>1).map(part=>displayProfile(part,.25*largest/65)
        .map(([z,r],i)=>(i?'L':'M')+x(z).toFixed(2)+' '+y(r).toFixed(2)).join(' ')).join(' ');
      return <g key={type+axis} data-beam={type} data-axis={axis}>
        {d && <path d={d} fill="none" stroke={colors[type]} strokeWidth={axis==='sagittal'?1.8:2.7} strokeDasharray={axis==='sagittal'?'5 4':undefined}/>}
        {parts.filter(part=>part.length===1).map(([[z,r]],i)=><circle key={i} cx={x(z)} cy={y(r)} r={axis==='sagittal'?2:3.5} fill={colors[type]}>
          <title>z = {positionText(z)} mm · {axis==='sagittal'?'S':'T'} 半径 {fmt(r*1000)} μm</title></circle>)}
      </g>;
    })}
    {predictions.map(p=>{
      const cx=x(section.z_mm), cy=y(p.radius);
      return <path key={p.beam+p.axis} data-prediction={p.beam} d={`M${cx-4} ${cy}h8M${cx} ${cy-4}v8`}
        stroke={p.beam==='primary'?colors.primary:colors.incident} strokeWidth="2"><title>{p.axis==='tangential'?'T':'S'} · {fmt(p.radius*1000)} μm</title></path>;
    })}
    {readings.map(r=><g key={r.id} data-reading={r.id} stroke={r.beam_uid==='primary'?colors.primary:colors.incident} strokeWidth="2">
      <title>{r.source_path} · {r.locator} · {r.conditions}</title>
      {r.measured_minus_model_mm.tangential!=null && <circle cx={x(r.z_mm)} cy={y(r.radii_mm.tangential)} r="5" fill="#101e29"/>}
      {r.measured_minus_model_mm.sagittal!=null && <rect x={x(r.z_mm)-5} y={y(r.radii_mm.sagittal)-5} width="10" height="10" fill="none"/>}
    </g>)}
    {Array.from({length:hi===lo?1:5},(_,i)=>{const z=lo+(hi-lo)*i/4;return <text key={i} x={x(z)} y="197" textAnchor="middle">{positionText(z)}</text>;})}
    <text x={X1} y="216" textAnchor="end">{hi===lo?'重合参考面 · z / mm':'展开位置 z / mm'}</text>
    {anchor && path && <><line x1={x(sourceZ)} x2={x(anchor.start)} y1="149" y2="149" stroke={colors.incident}/>
      <text x={x(sourceZ)} y="175">源</text>
      {components.map(row=>{const c=incidentControl(route,row.index);
        if(adjustableGap(route.arm,row.index))return null;
        const px=x(sourceZ+row.start);return <g key={row.element.uid || row.index}
          className={c?'section-movable':''} aria-label={'入射元件 '+row.element.name} onPointerDown={e=>start(e,row)}>
          <title>{row.element.name} · {fmt(sourceZ+row.start)} mm</title><path d={'M'+px+' 133v32'} stroke={colors.incident} strokeWidth="4"/>
          <rect x={px-9} y="128" width="18" height="43" fill="transparent"/></g>;})}</>}
    {Number.isFinite(section?.z_mm) && <text x={Math.min(X1-85,Math.max(X0,x(section.z_mm)+4))} y="126">面 {fmt(section.z_mm)} mm</text>}
  </svg>;
}
