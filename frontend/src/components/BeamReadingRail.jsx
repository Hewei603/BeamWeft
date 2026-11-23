import React, {useId, useState} from 'react';
import {groupPlotLabels} from './plotLabels';
import {readingAxis, widthNames} from './beamWidths';
import {positionText} from './plotViewport';

const fmt = n => Number(n.toPrecision(6));

export default function BeamReadingRail({readings, width, x, left, right, beamName, onOpenReading}) {
  const [selected,setSelected] = useState(null);
  const id = useId();
  const rows = readings.filter(r=>Number.isFinite(r.z_mm)).map(reading=>({reading,start:reading.z_mm,end:reading.z_mm}))
    .sort((a,b)=>a.start-b.start || a.reading.id-b.reading.id);
  if (!rows.length) return null;
  const groups = groupPlotLabels(rows,x,width);
  const active = groups.find(g=>g.rows.some(row=>row.reading.id===selected));
  return <section className="bench-readings" aria-label="光路上的原始测量">
    <svg viewBox={'0 0 '+width+' 48'} aria-label="实测记录在登记版本中的轴向位置">
      <text x="5" y="15">实测</text>
      <line x1={left} x2={right} y1="11" y2="11"/>
      {groups.map(group=>{
        const key=group.rows[0].reading.id;
        const label=group.rows.length===1 ? positionText(group.rows[0].start)+' mm'
          : group.rows.length+' 条记录';
        const toggle=()=>setSelected(active===group?null:key);
        return <g key={key} role="button" tabIndex={0} aria-label={'查看实测 · '+label}
          aria-controls={active===group?id:undefined} aria-expanded={active===group}
          onClick={toggle} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}}}>
          {group.rows.map(({reading,start})=><line key={reading.id} x1={x(start)} x2={x(start)} y1="6" y2="16"/>)}
          <rect x={group.boxLeft} y="18" width={group.boxWidth} height="24" rx="4"/>
          <text textAnchor="middle" x={group.boxLeft+group.boxWidth/2} y="35">{label}</text>
        </g>;
      })}
    </svg>
    {active && <div id={id} className="bench-reading-records">
      {active.rows.map(({reading:r})=>{
        const unit=r.unit==='um'?'μm':'mm';
        const axisName=axis=>readingAxis(axis,r.axes_aligned);
        const values=['tangential','sagittal'].filter(axis=>r[axis]!=null).map(axis=>axisName(axis)+' '+r[axis]+' '+unit);
        return <article key={r.id}>
          <strong>{beamName(r.beam_uid)} · {widthNames[r.definition]} · {values.join(' / ')}</strong>
          <span>工程 #{r.revision_id} · z = {positionText(r.z_mm)} mm · {r.wavelength_nm==null?'波长待确认':r.wavelength_nm+' nm'} · {r.conditions}</span>
          {Object.entries(r.native_widths || {}).filter(([,v])=>Number.isFinite(v.predicted)).map(([axis,v])=>
            <span key={axis}>{axisName(axis)} · 登记版预测 {fmt(v.predicted)} {unit} · 实测减预测 {fmt(v.residual)} {unit}</span>)}
          {r.comparison_note && <span>{r.comparison_note}</span>}
          {Object.entries(r.axis_notes || {}).map(([axis,note])=><span key={axis}>{axisName(axis)} · {note}</span>)}
          <button type="button" className="bench-text-btn" onClick={()=>onOpenReading(r)}>打开原件 · {r.source_path} · {r.locator}</button>
        </article>;
      })}
    </div>}
  </section>;
}
