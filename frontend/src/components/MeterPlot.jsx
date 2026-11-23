import React, { useMemo, useState } from 'react';

const fmt=v=>String(Number(v.toPrecision(5)));
const left=68, right=733, top=21, bottom=155;
const band=(a,scale)=>a?(a[0]===a[1]?fmt(a[0]*scale):fmt(a[0]*scale)+' 至 '+fmt(a[1]*scale)):'—';

export default function MeterPlot({record}){
  const [index,setIndex]=useState(0);
  const points=record.points;
  const fixed=record.absolute.compared?record.location.model?.watts:null;
  const plot=useMemo(()=>{
    let lo=Infinity,hi=-Infinity;
    const include=v=>{lo=Math.min(lo,v);hi=Math.max(hi,v);};
    if(fixed)fixed.forEach(include);
    for(const p of points){
      include(p.watts);
      p.reading_bounds_w?.forEach(include);
      p.prediction_at_reference_w?.forEach(include);
    }
    const max=Math.max(Math.abs(lo),Math.abs(hi));
    const [scale,unit]=max>=1 || max===0?[1,'W']:max>=1e-3?[1e3,'mW']:max>=1e-6?[1e6,'μW']:[1e9,'nW'];
    // Normalize first to keep the drawing finite even for very large opposite-signed readings.
    const factor=max || 1, minimum=lo/factor, maximum=hi/factor;
    const pad=(maximum-minimum || 0.1)*.1;
    const high=maximum+pad, low=minimum-pad;
    const y=v=>bottom-(v/factor-low)/(high-low)*(bottom-top);
    const x=i=>points.length===1?(left+right)/2:left+i/(points.length-1)*(right-left);
    let observations='',errors='',positionModel='';
    for(const [i,p] of points.entries()){
      const px=x(i).toFixed(2);
      observations+='M'+px+' '+y(p.watts).toFixed(2)+'h0.01';
      if(p.reading_bounds_w)errors+='M'+px+' '+y(p.reading_bounds_w[0]).toFixed(2)+'V'+y(p.reading_bounds_w[1]).toFixed(2);
      if(p.prediction_at_reference_w)positionModel+='M'+px+' '+y(p.prediction_at_reference_w[0]).toFixed(2)+'V'+y(p.prediction_at_reference_w[1]).toFixed(2)+'h0.01';
    }
    return {scale,unit,x,y,ticks:minimum===maximum?[lo]:[lo,lo/2+hi/2,hi],observations,errors,positionModel};
  },[fixed,points]);
  if(points.length<2)return null;
  const chosen=Math.min(index,points.length-1),active=points[chosen];
  const {scale,unit,x,y}=plot;
  const pick=e=>{
    const rect=e.currentTarget.getBoundingClientRect();
    const at=(e.clientX-rect.left)*768/rect.width;
    setIndex(Math.max(0,Math.min(points.length-1,Math.round((at-left)/(right-left)*(points.length-1)))));
  };
  return <figure className="meter-plot"><figcaption>同一测点的逐次功率 · {unit}</figcaption>
    <svg viewBox="0 0 768 186" role="img" aria-label="实测功率、固定源模型与按同步参考计算的预测" onPointerMove={pick} onClick={pick}>
      {plot.ticks.map((v,i)=><g key={i}><line className="meter-grid" x1={left} x2={right} y1={y(v)} y2={y(v)}/>
        <text x={left-6} y={y(v)+4} textAnchor="end">{fmt(v*scale)}</text></g>)}
      {fixed && <rect className="meter-fixed" x={left} y={y(fixed[1])} width={right-left} height={Math.max(1.5,y(fixed[0])-y(fixed[1]))}/>}
      <line className="meter-cursor" x1={x(chosen)} x2={x(chosen)} y1={top} y2={bottom}/>
      <path className="meter-local" d={plot.positionModel}/><path className="meter-errors" d={plot.errors}/>
      <path className="meter-dots" d={plot.observations}/><circle className="meter-active" cx={x(chosen)} cy={y(active.watts)} r="6"/>
      <text x={left} y="177">原件第 {points[0].row} 行</text><text x={right} y="177" textAnchor="end">第 {points.at(-1).row} 行</text>
    </svg>
    <div className="meter-legend"><span>● 实测</span>{fixed && <span>▬ 固定源预测</span>}{plot.positionModel && <span>┃ 同步参考预测</span>}
      {plot.errors && <span>│ 仪器误差界限</span>}</div>
    <label>查看原件第 {active.row} 行<input aria-label="逐行查看功率" type="range" min="0" max={points.length-1} value={chosen} onChange={e=>setIndex(Number(e.target.value))}/></label>
    <p>实测 {fmt(active.watts*scale)} {unit}{active.prediction_at_reference_w && <> · 按当行参考预测 {band(active.prediction_at_reference_w,scale)} {unit}</>}。</p>
  </figure>;
}
