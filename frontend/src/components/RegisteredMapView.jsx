import React, {useEffect, useMemo, useRef, useState} from 'react';
import ScientificPlot from './ScientificPlot';
import RegistrationDetails from './RegistrationDetails';
import {cellAt,mapDomains,mapScales} from './registeredMapScale.js';
import './RegisteredMapView.css';

const panels={reference:'参考分布',observed:'观测分布',residual:'残差'};
const densityColors=[[0,'#00204c'],[.25,'#414d6b'],[.5,'#7d7c78'],[.75,'#bcae6c'],[1,'#ffea46']];
const diffColors=[[0,'#2166ac'],[.25,'#92c5de'],[.5,'#f7f7f7'],[.75,'#f4a582'],[1,'#b2182b']];
const gradient=colors=>'linear-gradient(90deg,'+colors.map(([fraction,color])=>color+' '+fraction*100+'%').join(',')+')';
const fmt=value=>Number.isFinite(value) ? String(Number(value.toPrecision(5))) : '无可比值';

function MapLegend({difference,range}) {
  const [low,high]=range;
  const colors=difference?diffColors:densityColors;
  return <div className="registered-map-legend">
    <span>{difference?'差值密度':'同尺度归一化密度'} / mm⁻²</span>
    <div className="map-gradient" style={{background:low===high?colors[difference?2:0][1]:gradient(colors)}}/>
    <div className="map-ticks">{(low===high?[low]:[low,(low+high)/2,high]).map(v=><span key={v}>{fmt(v)}</span>)}</div>
  </div>;
}

function MapPanel({kind,grid,scales,view,selectView,onProbe,probe,centers}) {
  const residual=kind==='residual',range=residual?scales.difference:scales.density;
  const xTitle=grid.x_label+' / mm', yTitle=grid.y_label+' / mm';
  const figure=useRef(null),[size,setSize]=useState({width:320,height:300});
  useEffect(()=>{
    const observer=new ResizeObserver(([{contentRect}])=>{
      if(contentRect.width>0 && contentRect.height>0)setSize({width:contentRect.width,height:contentRect.height});
    });
    observer.observe(figure.current.querySelector('.oa-plot'));
    return ()=>observer.disconnect();
  },[]);
  const plot=useMemo(()=>{
    const x=grid.x_edges_mm,y=grid.y_edges_mm;
    const data=[{type:'heatmap',name:panels[kind],x,y,z:grid[kind],showscale:false,
      zmin:range[0]===range[1] && residual?-1:range[0],zmax:range[0]===range[1]?1:range[1],
      colorscale:residual?diffColors:densityColors,hoverongaps:false,
      hovertemplate:xTitle+' · %{x:.5g}<br>'+yTitle+' · %{y:.5g}<br>'+panels[kind]+' · %{z:.5g} mm⁻²<extra></extra>'}];
    for(const role of centers ? ['reference','observed'] : []) {
      const center=centers[role];
      if((role===kind || residual) && center && [center.x_mm,center.y_mm].every(Number.isFinite)) {
        const marker=role==='reference'?{symbol:'circle-open',size:13,color:'#00ddd5'}:{symbol:'diamond-open',size:16,color:'#e34aa6'};
        data.push({type:'scatter',mode:'markers',showlegend:false,name:panels[role]+'质心',x:[center.x_mm],y:[center.y_mm],
          marker:{...marker,line:{width:2}},hovertemplate:'%{x:.5g}, %{y:.5g} mm<extra>%{fullData.name}</extra>'});
      }
    }
    const point=probe && {x:(x[probe.column]+x[probe.column+1])/2,y:(y[probe.row]+y[probe.row+1])/2};
    const margin={l:55,r:13,t:16,b:48,autoexpand:false};
    const xRange=view?[...view.x]:[x[0],x.at(-1)], yRange=view?[...view.y]:(grid.y_down?[y.at(-1),y[0]]:[y[0],y.at(-1)]);
    const domain=mapDomains(xRange,yRange,Math.max(1,size.width-margin.l-margin.r),Math.max(1,size.height-margin.t-margin.b));
    const layout={autosize:true,margin,
      paper_bgcolor:'#131d2a',plot_bgcolor:'#344653',font:{color:'#d2dfe5',size:11},
      xaxis:{title:{text:xTitle,standoff:6},range:xRange,domain:domain.x,zeroline:false,nticks:5},
      yaxis:{title:{text:yTitle,standoff:6},range:yRange,domain:domain.y,zeroline:false,nticks:5},
      shapes:point ? [
        {type:'line',xref:'x',x0:point.x,x1:point.x,yref:'y domain',y0:0,y1:1,line:{color:'#a62e76',width:1,dash:'dot'}},
        {type:'line',xref:'x domain',x0:0,x1:1,yref:'y',y0:point.y,y1:point.y,line:{color:'#a62e76',width:1,dash:'dot'}},
      ]:[]};
    return {data,layout};
  },[kind,grid,range,residual,xTitle,yTitle,view,probe,centers,size]);
  return <figure ref={figure}>
    <figcaption>{kind==='residual'?'残差（观测 − 参考）':panels[kind]}</figcaption>
    <ScientificPlot {...plot} ariaLabel={panels[kind]+'配准分布图'} onViewport={selectView} onPoint={onProbe} filename={'spatial-'+kind}/>
    <MapLegend difference={residual} range={range}/>
  </figure>;
}

export default function RegisteredMapView({output}) {
  const {grid,registration,common_signal:signal,centroids}=output;
  const container=useRef(null);
  const [sideBySide,setSideBySide]=useState(false), [shown,setShown]=useState('residual');
  const [view,setView]=useState(null),[probe,setProbe]=useState(null),[markCenters,setMarkCenters]=useState(false);
  const scales=useMemo(()=>grid && mapScales(grid),[grid]);
  useEffect(()=>{
    if(!container.current) return;
    const observer=new ResizeObserver(([entry])=>setSideBySide(entry.contentRect.width>=840));
    observer.observe(container.current);
    return ()=>observer.disconnect();
  },[Boolean(scales)]);
  if(!scales) return <p role="status">记录中缺少可绘制的配准单元或坐标边界。可以展开完整计算输出核对。</p>;
  const kinds=sideBySide ? Object.keys(panels) : [shown];
  function selectView(next) {
    const x=grid.x_edges_mm,y=grid.y_edges_mm;
    const full={x:[x[0],x.at(-1)],y:grid.y_down?[y.at(-1),y[0]]:[y[0],y.at(-1)]};
    const matches=(a,b)=>['x','y'].every(axis=>a[axis].every((value,i)=>
      Math.abs(value-b[axis][i])<=1e-9*Math.abs(full[axis][1]-full[axis][0])));
    setView(previous=>!next || matches(next,full) ? null : previous && matches(next,previous) ? previous : next);
  }
  function inspect(x,_extra,y) {
    const column=cellAt(grid.x_edges_mm,x),row=cellAt(grid.y_edges_mm,y);
    if(row>=0 && column>=0)setProbe({column,row});
  }
  return <section ref={container} className="registered-maps" aria-label="配准分布与残差">
    {!sideBySide && <div role="group" aria-label="选择分布图" className="registered-map-switch">{Object.entries(panels).map(([key,label])=>
      <button type="button" key={key} aria-pressed={shown===key} onClick={()=>setShown(key)}>{label}</button>)}</div>}
    <div className={'registered-map-plots'+(sideBySide?' map-triptych':'')}>{kinds.map(kind=>
      <MapPanel key={kind} kind={kind} grid={grid} scales={scales} view={view} selectView={selectView}
        onProbe={inspect} probe={probe} centers={markCenters?centroids:null}/>)}</div>
    <p>两幅分布各自在可比区域内积分为 1。残差为正表示该处观测的信号占比更高。{scales.gaps && '灰色空格表示缺少可比数据。'}</p>
    <div className="map-actions">{centroids && <label className="map-centers"><input type="checkbox" checked={markCenters} onChange={e=>setMarkCenters(e.target.checked)}/>
      <span>显示质心（○ 参考，◇ 观测）</span></label>}
      {view && <button className="btn btn-secondary" type="button" onClick={()=>selectView(null)}>恢复完整视场</button>}</div>
    {probe && <p className="map-probe" aria-live="polite"><span>所选单元 · {grid.x_label} {fmt((grid.x_edges_mm[probe.column]+grid.x_edges_mm[probe.column+1])/2)} mm，
      {grid.y_label} {fmt((grid.y_edges_mm[probe.row]+grid.y_edges_mm[probe.row+1])/2)} mm</span>
      {Object.entries(panels).map(([key,label])=><span key={key}> {label} {fmt(grid[key][probe.row][probe.column])} mm⁻²</span>)}</p>}
    {registration && <details className="map-registration"><summary>坐标配准与有效视场</summary>
      <RegistrationDetails registration={registration}/>
      <p>共同区域为 {fmt(output.overlap_area_mm2)} mm²，覆盖观测图面积的 {fmt(100*output.coverage_observed)}%，
        参考图面积的 {fmt(100*output.coverage_reference)}%。</p>
      {signal && <p>归一化前，该区域积分为参考 {fmt(signal.reference)} {signal.reference_unit}，
        观测 {fmt(signal.observed)} {signal.observed_unit}。{signal.calibration_basis && '信号换算依据为'+signal.calibration_basis+'。'}</p>}
    </details>}
  </section>;
}
