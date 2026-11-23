import React from 'react';
import {radiusBasisLabels} from './beamWidths';
import './StationFootprint.css';

const format = value => Number(value.toPrecision(5)).toString();
const signed = value => value>0 ? '+'+format(value) : format(value);

function stationFootprint(beam) {
  const center = axis => beam.field_axes?.[axis]?.center_mm;
  const radius = axis => {
    const value=beam.radii_mm?.[axis];
    return Number.isFinite(value) && value>0 ? value : null;
  };
  const t=center('tangential'), s=center('sagittal');
  const rt=radius('tangential'), rs=radius('sagittal');
  const bt=beam.width_basis?.tangential, bs=beam.width_basis?.sagittal;
  const clipped=bt==='clipped' || bs==='clipped';
  const registered=beam.registration_available!==false;
  const supported=registered && !clipped && (rt!=null || rs!=null || beam.launch_basis==='specified');
  const located=supported && Number.isFinite(t) && Number.isFinite(s);
  let note;
  if (!registered) note='该支路尚未配准到此面';
  else if (clipped) note='此面受孔径影响，需要截面场或光线数据';
  else if (!Number.isFinite(t) || !Number.isFinite(s)) {
    note=['T','S'].filter((_,i)=>!Number.isFinite([t,s][i])).join('、')+' 向束心未确定';
  } else if (!located) note='此面缺少光束宽度或独立束心依据';
  else if (beam.launch_basis==='ideal_on_axis' || beam.launch_basis==='reference_axis') note='按模型参考轴定位';
  else if (beam.launch_basis==='specified') note='按登记的位置与指向定位';
  else note='采用此面已存的束心坐标';
  return {uid:beam.uid,t,s,rt,rs,supported,located,note,gaussian:bt==='gaussian' && bs==='gaussian',
    width:[bt,bs].map(b=>radiusBasisLabels[b] || '口径待核对')};
}

function FootprintPlot({shapes,name}) {
  const maximum=Math.max(0,...shapes.flatMap(p=>[Math.abs(p.t)+(p.rt??0), Math.abs(p.s)+(p.rs??0)]));
  if (!Number.isFinite(maximum)) return null;
  const step=10**Math.floor(Math.log10(maximum))/2;
  const rounded=step>0 ? Math.ceil(maximum/step)*step : maximum;
  const limit=Number.isFinite(rounded) ? rounded : maximum;
  const scale=limit>0 ? 81/limit : 1;
  // T and S use the same length scale. Keep zero in view for the optical axis.
  const x=v=>108+v*scale, y=v=>106-v*scale;
  return <svg className="station-footprint-plot" viewBox="0 0 225 218" role="img" aria-label="相对工程轴的光束横截面，T 向右，S 向上">
    <g className="footprint-frame">
      <path d="M19 106h179m-4 -3 4 3 -4 3 M108 195V17m-3 4 3-4 3 4"/>
      <text x="203" y="110">T</text><text x="115" y="18">S</text>
      {limit>0 && <><path d="M27 103v6m162 -6v6 M105 25h6m-6 162h6"/>
        <text x="27" y="207" textAnchor="middle">{format(-limit)}</text>
        <text x="108" y="207" textAnchor="middle">0</text>
        <text x="189" y="207" textAnchor="middle">{format(limit)}</text>
        <text x="101" y="29" textAnchor="end">{format(limit)}</text></>}
    </g>
    {shapes.map(p=><g key={p.uid} className={'footprint-shape '+(p.uid==='primary'?'primary':'incident')}
      data-footprint={p.uid} data-center-t={p.t} data-center-s={p.s}>
      <title>{name(p)} · 束心 T {signed(p.t)} / S {signed(p.s)} mm</title>
      {p.gaussian && p.rt && p.rs
        ? <ellipse cx={x(p.t)} cy={y(p.s)} rx={p.rt*scale} ry={p.rs*scale}/>
        : <>{p.rt && <line x1={x(p.t-p.rt)} y1={y(p.s)} x2={x(p.t+p.rt)} y2={y(p.s)}/>}
          {p.rs && <line x1={x(p.t)} y1={y(p.s-p.rs)} x2={x(p.t)} y2={y(p.s+p.rs)}/>}</>}
      {p.uid==='primary' ? <path d={'M'+(x(p.t)-3)+' '+y(p.s)+'h6m-3 -3v6'}/> : <circle cx={x(p.t)} cy={y(p.s)} r="2.4"/>}
    </g>)}
  </svg>;
}

export default function StationFootprint({beams,name,open,onToggle}) {
  const rows=beams.map(stationFootprint);
  const located=rows.filter(p=>p.located);
  const primary=rows.find(p=>p.uid==='primary'), branch=rows.find(p=>p.uid!=='primary');
  function separation(axis) {
    return primary?.supported && branch?.supported && Number.isFinite(primary[axis]) && Number.isFinite(branch[axis])
      ? branch[axis]-primary[axis] : null;
  }
  const deltaT=separation('t'), deltaS=separation('s');
  const separationText=value=>Number.isFinite(value) ? signed(value)+' mm' : '待定';
  return <details className="station-footprint" open={open} onToggle={e=>onToggle(e.currentTarget.open)}>
    <summary>查看观察面横截面</summary>
    {open && <div className="footprint-layout">
      {located.length>0 && <FootprintPlot shapes={located} name={name}/>}
      <div><strong>展开光路的 T/S 坐标 · mm</strong>
        {rows.map(p=><p key={p.uid} className={p.uid==='primary'?'primary':'incident'}><b>{name(p)}</b> · {p.note}
          {!p.located && p.supported && (Number.isFinite(p.t) || Number.isFinite(p.s)) &&
            <><br/>已知束心坐标 {Number.isFinite(p.t)?'T '+signed(p.t):'S '+signed(p.s)} mm。</>}
          {p.located && <><br/>束心 T {signed(p.t)} / S {signed(p.s)} mm。
            {p.gaussian && p.rt && p.rs ? '椭圆为 1/e² 强度边界。' :
              (p.rt || p.rs) ? '线段标出已知宽度（'+[p.rt && 'T '+p.width[0],p.rs && 'S '+p.width[1]].filter(Boolean).join(' / ')+'）。' : '仅显示束心。'}</>}
        </p>)}
        {(Number.isFinite(deltaT) || Number.isFinite(deltaS)) && <p className="footprint-offset">{name(branch)}相对{name(primary)}的束心偏移<br/>
          T {separationText(deltaT)} / S {separationText(deltaS)}</p>}
      </div>
    </div>}
  </details>;
}
