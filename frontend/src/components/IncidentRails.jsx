import React from 'react';
import { adjustableGap, elementMarkerZ, incidentControl, moveIncidentElement } from './benchGeometry';
import AssemblySpan from './AssemblySpan';
import ThinLensSymbol from './ThinLensSymbol';
import {positionText} from './plotViewport';
import {svgButton} from './usePointerGesture';

const format = n => Number(n.toPrecision(5));
const short = (name, limit=32) => name.length > limit ? name.slice(0, limit-1)+'…' : name;

export default function IncidentRails({project, routes, activeUid, selected, x, inView, overlaps, left, right, disabled, onSelect, onSelectHost, onInspect, startHostMove, start}) {
  return routes.map((route, i) => {
    const {path, entrance, sourceZ, endZ, rows} = route;
    const y = 340 + 46*i, active = path.uid === activeUid;
    const selectedRow = selected?.uid === path.uid ? rows[selected.index] : null;
    const selectedZ = selectedRow && sourceZ != null ? sourceZ + elementMarkerZ(route.arm,selectedRow) : null;
    const choose = index => onSelect(path.uid, index);
    const activate = index => trigger => {choose(index);onInspect(trigger);};
    const name = path.name + ' · ' + (path.wavelength_nm == null ? '波长待定' : format(path.wavelength_nm)+' nm')
      + ' → ' + (entrance ? entrance.element.name : '入口待重新绑定');
    return <g key={path.uid} className={'bench-rail'+(active?' active':'')} data-route={path.uid}>
      <g {...svgButton(activate(null))} aria-label={'查看支路 '+path.name} aria-pressed={active}>
        <rect x={left-15} y={y-33} width={right-left+21} height="22" rx="4" className="rail-band"/>
        <text x={left} y={y-17}>{short(name,right-left<480?17:32)}{route.broken && entrance?' · 后续未连通':''}</text><title>{name}</title>
        {selectedRow && right-left>480 && <text className="rail-selection" x={right-8} y={y-17} textAnchor="end">
          {short(selectedRow.element.name,16)}{selectedZ!=null?' · '+positionText(selectedZ)+' mm':''}
        </text>}
      </g>
      {entrance && <>
        <AssemblySpan project={route.arm} rows={rows} index={selectedRow?.index} x={z=>x(sourceZ+z)} visible={(a,b)=>overlaps(a+sourceZ,b+sourceZ)} y={y+9}/>
        {overlaps(sourceZ,entrance.start) && <line x1={x(sourceZ)} x2={x(entrance.start)} y1={y} y2={y} className="rail-input"/>}
        {overlaps(entrance.start,endZ) && <line x1={x(entrance.start)} x2={x(endZ)} y1={y} y2={y} className="rail-joined"/>}
        {selectedRow && adjustableGap(route.arm,selectedRow.index) && inView(selectedZ) &&
          <path d={`M${x(selectedZ)-4} ${y+9}h8m-4 0v-9`} stroke="currentColor" strokeWidth="2"
            fill="none" data-gap-end={selectedRow.element.uid}>
            <title>{selectedRow.element.name} · 段末 {positionText(selectedZ)} mm</title>
          </path>}
        {route.shared.filter(row=>overlaps(row.start,row.end) && !adjustableGap(project,row.index)).map(row=>
          <g key={row.element.uid} {...svgButton(el=>{onSelectHost(row.index);onInspect(el);},true)}
            className="rail-optic" aria-label={'查看共享元件 '+path.name+' / '+row.element.name} onPointerDown={e=>startHostMove(e,row.index)}>
            <title>{row.element.name} · 共享实物 · {positionText(row.start)} mm</title>
            <rect className="rail-glyph" x={x(row.start)-2} y={y-2} width={Math.max(4,x(row.end)-x(row.start))} height="11"/>
            <rect x={x(row.start)-5} y={y-3} width={Math.max(14,x(row.end)-x(row.start)+10)} height="15" fill="transparent"/>
          </g>)}
        {inView(endZ) && <circle cx={x(endZ)} cy={y} r="2.7" className="rail-stop"><title>已接入段终点 {positionText(endZ)} mm</title></circle>}
        {inView(sourceZ) && <g {...svgButton(activate(null))} aria-label={'选择光源 '+path.name} data-source-z={sourceZ}>
          <title>{path.name} 源面 · {positionText(sourceZ)} mm</title>
          <path className="rail-source" d={`M${x(sourceZ)-8} ${y-6}l9 6 -9 6z`}/>
          <rect x={x(sourceZ)-14} y={y-10} width="14" height="20" fill="transparent"/>
        </g>}
        {inView(entrance.start) && <path data-entrance-z={entrance.start} className="rail-join" pointerEvents="none" d={`M${x(entrance.start)} ${y-7}l5 7 -5 7 -5 -7z`}>
          <title>接入 {entrance.element.name} · {positionText(entrance.start)} mm</title></path>}
        {rows.filter(r=>overlaps(sourceZ+r.start,sourceZ+r.end) && !adjustableGap(route.arm,r.index)).map(r=>{
          const z=sourceZ+r.start, px=x(z), control=incidentControl(route,r.index);
          const chosen=selected?.uid===path.uid && selected.index===r.index;
          return <g key={r.element.uid || r.index} {...svgButton(activate(r.index),true)} aria-pressed={chosen}
            aria-label={'选择 '+path.name+' / '+r.element.name+'（'+(r.index+1)+'）'} data-element-z={z}
            className={'rail-optic'+(control && !disabled?' movable':'')+(chosen?' selected':'')}
            onPointerDown={e=>{
              if (e.button!==0 || e.isPrimary===false) return;
              choose(r.index); start(e,control,moveIncidentElement,onInspect);
            }}>
            <title>{r.element.name} · {positionText(z)} mm</title>
            {r.element.length_mm>0 ? <rect className="rail-glyph" x={px} y={y-6} width={Math.max(2,x(sourceZ+r.end)-px)} height="12" /> :
              r.element.kind==='lens' ? <ThinLensSymbol className="rail-glyph" x={px} y={y} halfHeight={9} focal={r.element.focal_mm}/> :
              <path className="rail-glyph" d={`M${px} ${y-8}v16`}/>}
            <rect className="rail-target" x={px-9} y={y-12} width="18" height="24" fill="transparent"/>
          </g>;
        })}
      </>}
    </g>;
  });
}
