import React, {useState} from 'react';
import {radiusBasisLabels} from './beamWidths';
import StationFootprint from './StationFootprint';
import {componentLabels} from './layoutSelections';

const fmt = n => Number.isFinite(n) ? String(Number(n.toPrecision(5))) : '—';
const microns = mm => Number.isFinite(mm) ? fmt(mm * 1000) : '—';

function RadiusBars({beam, scale}) {
  const t=beam.radii_mm?.tangential, s=beam.radii_mm?.sagittal;
  const axes = t===s ? [['T/S',t]] : [['T',t],['S',s]];
  return <div className="probe-bars" aria-hidden="true">
    {axes.filter(([,r])=>Number.isFinite(r)).map(([label,r])=><span key={label}>
      <small>{label}</small><i><i style={{width:100*r/scale+'%'}}/></i></span>)}
  </div>;
}

function BeamReading({beam, name, showSymbol, compact, scale}) {
  const [t,s] = ['tangential','sagittal'].map(axis=>radiusBasisLabels[beam.width_basis?.[axis]]);
  const basis = t===s ? t : 'T '+(t || '待定')+' / S '+(s || '待定');
  const centers = beam.field_axes || {};
  const notes = [...new Set(Object.values(beam.notes || {}).filter(Boolean))];
  return <div className={'probe-reading'+(compact?' compact':'')} data-beam-uid={beam.uid}>
    <span className="probe-reading-value">
      {showSymbol && <i aria-hidden="true" className={beam.uid==='primary'?'probe-symbol primary':'probe-symbol incident'}/>}
      <b>{name}</b> · T {microns(beam.radii_mm?.tangential)} / S {microns(beam.radii_mm?.sagittal)} μm
    </span>
    {scale>0 && <RadiusBars beam={beam} scale={scale}/>}
    <small>{Number.isFinite(beam.wavelength_nm) ? fmt(beam.wavelength_nm)+' nm' : '波长待定'} · {basis || '半径约定待核对'}
      {beam.launch_basis==='specified' && (Number.isFinite(centers.tangential?.center_mm) || Number.isFinite(centers.sagittal?.center_mm)
        ? ' · 束心偏移 T '+fmt(centers.tangential?.center_mm)+' / S '+fmt(centers.sagittal?.center_mm)+' mm' : ' · 束心偏移待测')}</small>
    {notes.length>0 && <details><summary>查看截面读数依据</summary>{notes.map(note=><p key={note}>{note}</p>)}</details>}
  </div>;
}

export default function ProbeReadings({beams, paths, branchUid, cavity}) {
  const [footprintOpen,setFootprintOpen]=useState(false);
  if (!beams?.length) return null;
  const visible = beams.filter(b=>b.uid==='primary' || b.uid===branchUid);
  const others = beams.filter(b=>b.uid!=='primary' && b.uid!==branchUid);
  const comparable = visible.filter(b=>Object.values(b.radii_mm || {}).some(Number.isFinite));
  const scale = comparable.length>1 ? Math.max(...comparable.flatMap(b=>Object.values(b.radii_mm).filter(Number.isFinite))) : null;
  const pathNames=componentLabels(paths);
  const name = b=>b.uid==='primary' ? (cavity?'腔模':'主光束') : pathNames.get(b.uid) || b.uid;
  return <div className="probe-readings">
    {scale>0 && <small className="probe-scale">半径共用刻度 0 – {microns(scale)} μm</small>}
    {visible.map(beam=><BeamReading key={beam.uid} beam={beam} name={name(beam)} showSymbol={!!branchUid} compact={!branchUid} scale={scale}/>)}
    <StationFootprint beams={visible} name={name} open={footprintOpen} onToggle={setFootprintOpen}/>
    {others.length>0 && <details className="probe-other"><summary>此面的其他光束（{others.length}）</summary>
      {others.map(beam=><BeamReading key={beam.uid} beam={beam} name={name(beam)}/>)}</details>}
  </div>;
}
