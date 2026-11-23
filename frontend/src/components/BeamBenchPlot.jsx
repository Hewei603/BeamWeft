import React, { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { adjustableGap, elementMarkerZ, moveAlongAxis, moveObservationPlane, positionControl } from './benchGeometry';
import IncidentRails from './IncidentRails';
import AssemblySpan from './AssemblySpan';
import ThinLensSymbol from './ThinLensSymbol';
import {groupPlotLabels} from './plotLabels';
import BeamReadingRail from './BeamReadingRail';
import PlotNavigation from './PlotNavigation';
import {svgButton,useAxialGesture} from './usePointerGesture';
import {axialPixel, axisTicks, displayProfile, fitInterval, positionText, profileInView, revealPosition, transverseExtent} from './plotViewport';
import {stationRadiusMarks} from './beamWidths';
import EngineeringBeam from './EngineeringBeam';
import {engineeringInView,engineeringProfile} from './beamProjection';

const minimumChartHeight = 336;
const number = n => Number(n.toPrecision(4)).toString();

function envelope(parts, x, y, radiusTolerance, shade) {
  const isolated=parts.filter(part=>part.length===1);
  const lines=parts.filter(part=>part.length>1).map(part=>displayProfile(part,radiusTolerance));
  const boundaries = [1,-1].map(sign=>lines.map(part=>part.map(([z,r])=>
    x(z).toFixed(2)+' '+y(sign*r).toFixed(2))));
  return <>{shade && boundaries[0].map((top,i)=><polygon key={i} className="bench-envelope-area" aria-hidden="true"
    points={top.concat([...boundaries[1][i]].reverse()).join(' ')}/>)}{[1,-1].map((sign,side)=><React.Fragment key={sign}>
    {lines.length>0 && <path d={boundaries[side].map(points=>'M'+points.join(' L')).join(' ')}/>}
    {isolated.map(([[z,r]],i)=><circle key={i} cx={x(z)} cy={y(sign*r)} r="3"><title>z = {positionText(z)} mm · 已知半径 {number(r*1000)} μm</title></circle>)}
  </React.Fragment>)}</>;
}

export default function BeamBenchPlot({ project, positions, activeIndex, onSelect, onInspect, plotTarget, onSelectProbe, result, saved, previous, projection, disabled, beginGesture, onEdit, probe, probeControl, probeSample, routes, activeRoute, armSelection, onSelectArm, readings, onOpenReading }) {
  const svg = useRef(null);
  const gesture = useAxialGesture(disabled, project, beginGesture);
  const chooser = useRef(null), trigger = useRef(null), chooserId=useId();
  const [groupKey,setGroupKey] = useState(null);
  const [size,setSize] = useState({width:960,chartHeight:minimumChartHeight});
  const railHeight=routes.length ? 46*routes.length+14 : 0;
  const [camera, setCamera] = useState(null);
  const [transverseScale,setTransverseScale]=useState('window');
  const clipId = useId();
  useLayoutEffect(()=>{
    const el=svg.current;
    const resize=()=>{
      const width=el.clientWidth, chartHeight=el.clientHeight-railHeight;
      if (width>0 && chartHeight>0) setSize(old=>old.width===width && old.chartHeight===chartHeight
        ? old : {width,chartHeight});
    };
    resize();
    const observer=new ResizeObserver(resize);observer.observe(el);
    return ()=>observer.disconnect();
  },[railHeight]);
  const {width,chartHeight}=size;
  // The axis and data get the additional height. Labels, handles and branch rows keep their physical pixel size.
  const extra=chartHeight-minimumChartHeight;
  const mid=152+extra/2, vertical=86+extra/2;
  const left=width<520?53:72, right=width-(width<520?16:34);
  const [frozenDomain, setFrozenDomain] = useState(null);
  const physicalLength = positions.at(-1)?.end || 0;
  const incident = analysis => analysis?.sections?.paths?.find(p=>p.uid===activeRoute?.path.uid);
  const engineering=projection==='tangential' || projection==='sagittal';
  const branchUid=activeRoute?.path.uid;
  const projected=useMemo(()=>{
    if (!engineering) return [];
    const traces=[];
    for (const [layer,analysis] of [['baseline',saved],['previous',previous===saved?null:previous],['current',result]]) {
      if (!analysis) continue;
      const primary=analysis.planes?.[projection] ?? analysis.primary_center_axes?.[projection];
      if (primary) traces.push({layer,uid:'primary',...engineeringProfile(primary)});
      const arm=analysis.sections?.paths?.find(p=>p.uid===branchUid)?.axes?.[projection];
      if (arm) traces.push({layer,uid:branchUid,...engineeringProfile(arm)});
    }
    return traces;
  },[engineering,projection,branchUid,saved,previous,result]);
  const planes=useMemo(()=>[...new Set([result,saved,previous].filter(Boolean).flatMap(analysis=>[
    ...Object.values(analysis.planes),
    ...Object.values(analysis.sections?.paths?.find(p=>p.uid===branchUid)?.axes || {}),
  ]))],[result,saved,previous,branchUid]);
  let widest = 0, longest = physicalLength, earliest = Math.min(0,...routes.map(r=>r.sourceZ??0));
  for (const plane of planes) {
    for (const [z,radius] of plane.profile || []) {
      if (!engineering) widest=Math.max(widest,radius);
      longest=Math.max(longest,z);
      earliest=Math.min(earliest,z);
    }
  }
  for (const projectedBeam of projected) {
    widest=Math.max(widest,projectedBeam.extent);
    for (const segment of projectedBeam.centers) {
      earliest=Math.min(earliest,segment[0][0]);
      longest=Math.max(longest,segment.at(-1)[0]);
    }
  }
  const observations = engineering ? [] : readings.flatMap(reading =>
    Object.keys(reading.measured_minus_model_mm).map(axis => ({reading, axis, radius: reading.radii_mm[axis]}))
      .filter(point => Number.isFinite(point.radius) && point.radius > 0 && Number.isFinite(reading.z_mm)));
  for (const {radius} of observations) widest = Math.max(widest, radius);
  const stationMarks = !engineering && activeRoute ? stationRadiusMarks(probeSample?.beams, activeRoute.path.uid) : [];
  for (const mark of stationMarks) widest = Math.max(widest, mark.radius);
  for (const reading of readings.filter(r=>Number.isFinite(r.z_mm))) {
    earliest = Math.min(earliest, reading.z_mm);
    longest = Math.max(longest, reading.z_mm);
  }
  // A newly built train can have only coincident faces. Give the ruler a
  // physical scale for the first drag without adding length to the model.
  const bounds = {start:earliest, end:longest > earliest ? longest : earliest + 100};
  const view = frozenDomain ?? (camera ? fitInterval(camera.start,camera.end-camera.start,bounds) : bounds);
  const isLocal=view.start>bounds.start || view.end<bounds.end;
  const inView = z => Number.isFinite(z) && z >= view.start && z <= view.end;
  const overlaps = (start,end) => start <= view.end && end >= view.start;
  const axisEnd = left+(right-left)/1.08;
  const span = view.end-view.start;
  const mmPerPixel = span/(axisEnd-left);
  const {start:viewStart,end:viewEnd}=view;
  const visibleWidths=useMemo(()=>new Map(engineering ? [] : planes.map(plane=>[plane,
    profileInView(plane.profile || [],plane.profile_breaks,{start:viewStart,end:viewEnd})])),
  [engineering,planes,viewStart,viewEnd]);
  const visibleProjections=useMemo(()=> isLocal
    ? projected.map(trace=>engineeringInView(trace,{start:viewStart,end:viewEnd})) : projected,
  [isLocal,projected,viewStart,viewEnd]);
  if (isLocal && transverseScale==='window') {
    widest=0;
    for (const parts of visibleWidths.values()) widest=Math.max(widest,transverseExtent(parts));
    for (const trace of visibleProjections) widest=Math.max(widest,trace.extent);
    for (const {radius,reading} of observations) if (inView(reading.z_mm)) widest=Math.max(widest,radius);
    if (inView(probeSample?.z_mm)) for (const mark of stationMarks) widest=Math.max(widest,mark.radius);
  }
  const range = widest > 0 ? widest * 1.12 : 1;
  const x = z => axialPixel(z,view,left,axisEnd);
  const boundedX = z => x(Math.max(view.start,Math.min(view.end,z)));
  const visibleRows = positions.filter(row=>overlaps(row.start,row.end));
  const groups=groupPlotLabels(visibleRows,x,width,row=>
    Math.max(row.start,view.start)+(Math.min(row.end,view.end)-Math.max(row.start,view.start))/2);
  const changeCamera = next => {
    if (gesture.active.current) return;
    setGroupKey(null);
    setCamera(!next || next.start===bounds.start && next.end===bounds.end ? null : next);
  };
  const keyOf=g=>JSON.stringify(g.rows.map(row=>row.element.uid ?? row.index));
  const opened=groups.find(g=>keyOf(g)===groupKey);
  const isOpen=!!opened;
  useEffect(()=>{
    if(!isOpen)return;
    const first=chooser.current?.querySelector('button[aria-pressed=true]') || chooser.current?.querySelector('button');
    first?.focus({preventScroll:true});
    const outside=e=>{
      if(!chooser.current?.contains(e.target) && !trigger.current?.contains(e.target))setGroupKey(null);
    };
    document.addEventListener('pointerdown',outside);
    return ()=>document.removeEventListener('pointerdown',outside);
  },[isOpen,groupKey]);
  const closeChooser=()=>{setGroupKey(null);trigger.current?.focus({preventScroll:true});};
  const y = r => mid - r / range * vertical;
  const probeZ = probeControl?.value;
  const start = (event, control, apply, onPress) => {
    if (gesture.beginAxis(event, svg.current, control, mmPerPixel, apply, () => setFrozenDomain(null), onPress)) {
      setFrozenDomain(view);
    }
  };
  const startPan = event => {
    if (view.end-view.start >= bounds.end-bounds.start) return;
    const inverse=svg.current.getScreenCTM().inverse();
    const x=e=>new DOMPoint(e.clientX,e.clientY).matrixTransform(inverse).x;
    const startX=x(event);
    if (gesture.begin(event, svg.current, {
      move:e=>setCamera(fitInterval(view.start-(x(e)-startX)*mmPerPixel, view.end-view.start, bounds)),
      finish() {},
      cancel:()=>setCamera(view),
    }, {readOnly:true})) {
      event.preventDefault();
      svg.current.focus({preventScroll:true});
      setGroupKey(null);
    }
  };
  const startHostMove=(event,index)=>{
    if (gesture.active.current || event.isPrimary===false || event.button!==0) return;
    onSelect(index);
    start(event,positionControl(project,index,positions),moveAlongAxis,onInspect);
  };
  const draw = (analysis, baseline) => analysis && Object.entries(analysis.planes).map(([axis, plane]) => {
    return <g key={axis} data-curve={baseline ? 'baseline' : axis} className={baseline ? 'bench-base' : 'bench-' + axis}>
      {envelope(visibleWidths.get(plane),x,y,.25*range/vertical,!baseline && analysis===result && axis==='tangential')}
    </g>;
  });
  const drawIncident = (analysis, baseline) => Object.entries(incident(analysis)?.axes || {}).map(([axis, plane]) => {
    if (!plane.profile?.length) return null;
    return <g key={axis} data-envelope={activeRoute.path.uid} data-direction={axis} data-saved={baseline}
      className={'bench-arm-profile '+axis+(baseline?' saved':'')}>
      {envelope(visibleWidths.get(plane),x,y,.25*range/vertical,!baseline && analysis===result && axis==='tangential')}
    </g>;
  });
  const {ticks,offset}=axisTicks(view,width<520?3:5);
  const focusRow=positions[activeIndex];
  const armRow=activeRoute?.rows[armSelection?.index];
  const mainZ=focusRow && elementMarkerZ(project,focusRow);
  const armZ=Number.isFinite(activeRoute?.sourceZ)
    ? activeRoute.sourceZ+(armRow ? elementMarkerZ(activeRoute.arm,armRow) : 0) : null;
  const onProbe=plotTarget?.kind==='probe';
  const validProbeZ=probeControl && probeZ>=probeControl.min && probeZ<=probeControl.max ? probeZ : null;
  const focusZ=onProbe ? validProbeZ : armSelection ? armZ : mainZ;
  const focusName=onProbe ? probe?.name : armSelection ? armRow?.element.name || activeRoute?.path.name : focusRow?.element.name;
  useLayoutEffect(()=>{
    if (!plotTarget?.reveal || gesture.active.current) return;
    const next=revealPosition(view,bounds,focusZ);
    if(next===view)return;
    const frame=requestAnimationFrame(()=>changeCamera(next));
    return ()=>cancelAnimationFrame(frame);
  },[plotTarget]);
  return <><svg ref={svg} tabIndex={-1} className={'bench-svg'+(routes.length?' multipath':'')}
    style={{height:'calc(var(--bench-chart-height, 336px) + '+railHeight+'px)'}}
    data-z-start={view.start} data-z-end={view.end} data-y-extent-mm={widest>0 ? range : undefined}
    viewBox={'0 -34 '+width+' '+(chartHeight+railHeight)} aria-label="沿传播方向展开的光路与计算光束包络">
    <title>近轴光路。选择元件可查看截面，拖动可调元件可改变轴向位置。</title>
    <defs><clipPath id={clipId}><rect x={left-14} y="33" width={axisEnd-left+28} height={216+extra}/></clipPath></defs>
    {groups.map(group=>{
      const rows=group.rows, multiple=rows.length>1, key=keyOf(group);
      const active=rows.some(row=>row.index===activeIndex);
      const label=multiple?rows.length+' 个元件':rows[0].element.name;
      const activate=e=>{
        if(!multiple){onSelect(rows[0].index);onInspect(e.currentTarget);return;}
        trigger.current=e.currentTarget;setGroupKey(groupKey===key?null:key);
      };
      return <g key={key} role="button" tabIndex={0} aria-label={multiple?'选择此处的 '+label:'定位 '+label+'（'+(rows[0].index+1)+'）'}
        aria-expanded={multiple?key===groupKey:undefined} aria-controls={key===groupKey?chooserId:undefined}
        className={'bench-object-label'+(active?' selected':'')} onClick={activate}
        onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();activate(e);}}}>
        <line x1={group.at} x2={group.at} y1="-7" y2="5"/>
        <rect x={group.boxLeft} y="-32" width={group.boxWidth} height="26" rx="5"/>
        <foreignObject x={group.boxLeft+3} y="-32" width={Math.max(1,group.boxWidth-6)} height="26" pointerEvents="none">
          <div className="bench-object-name">{label}{multiple?' ▾':''}</div>
        </foreignObject>
        <title>{rows.map(r=>(r.index+1)+'. '+r.element.name+' · '+positionText(r.start)+' mm').join('\n')}</title>
      </g>;
    })}
    {ticks.map(({z,label},i)=><g key={i} data-z-tick={z}><line className="bench-grid" x1={x(z)} x2={x(z)} y1="46" y2={246+extra} /><text x={x(z)} y={264+extra} textAnchor="middle">{label}</text></g>)}
    {[-1, 0, 1].map(sign => <g key={sign}><line className={sign ? 'bench-grid' : 'bench-axis'} x1={left} x2={right} y1={y(sign * range)} y2={y(sign * range)} />
      <text x={left - 9} y={y(sign * range) + 4} textAnchor="end">{sign && !widest ? '—' : number(sign * range * 1000)}</text></g>)}
    <text x={left} y="29">{engineering?'相对工程光轴 · '+(projection==='tangential'?'T':'S'):routes.length?'相对各自束心':'相对束心'} / μm</text><text x={right} y={289+extra} textAnchor="end">{longest===earliest?'参考面重合 · 放置标尺 / mm':offset?'相对 z = '+offset+' mm 的距离 / mm':'轴向距离 z / mm'}</text>
    {isLocal && <rect className="bench-pan-ruler" x={left-10} y={246+extra} width={right-left+12} height="47" fill="transparent" onPointerDown={startPan}>
      <title>拖动刻度平移视野</title></rect>}
    <g clipPath={'url(#'+clipId+')'}>
    {visibleRows.filter(row => row.element.length_mm > 0 && (!adjustableGap(project, row.index) || row.element.medium_role)).map(row =>
      <rect className="bench-material" key={row.index} x={boundedX(row.start)} width={Math.max(2, boundedX(row.end) - boundedX(row.start))} y="78" height={148+extra} />)}
    <AssemblySpan project={project} rows={positions} index={activeIndex} x={boundedX} visible={overlaps} y={220+extra}/>
    {view.start < 0 && inView(0) && <text x={x(0)} y="72" textAnchor="middle">主光路起点</text>}
    {inView(probeZ) && probeZ>=0 && probeZ<=physicalLength && probeZ>=probeControl.min && probeZ<=probeControl.max && <g className="bench-probe-marker" data-plane={probe.uid} data-z-mm={probeZ}
      role="slider" tabIndex={0} aria-label={'移动观察面 '+probe.name} aria-valuenow={probeZ} aria-valuemin={probeControl.min} aria-valuemax={probeControl.max}
      aria-valuetext={'z = '+positionText(probeZ)+' mm'} aria-orientation="horizontal" aria-disabled={disabled}
      onFocus={onSelectProbe}
      onPointerDown={e=>{onSelectProbe();start(e,probeControl,moveObservationPlane);}}
      onKeyDown={e=>{
        if(disabled || gesture.active.current || !['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
        e.preventDefault();
        const increment=Math.min((probeControl.max-probeControl.min)/1000,(view.end-view.start)/100);
        const step=Math.max(increment, Math.abs(probeZ)*Number.EPSILON, Number.MIN_VALUE)*(e.shiftKey?10:1);
        const desired=e.key==='Home'?probeControl.min:e.key==='End'?probeControl.max:probeZ+(e.key==='ArrowRight'?step:-step);
        const target=Math.max(probeControl.min,Math.min(probeControl.max,desired));
        if (isLocal && !inView(target)) changeCamera(revealPosition(view,bounds,target));
        onEdit(moveObservationPlane(project,probeControl,target));
      }}>
      <title>{probe.name} · z = {positionText(probeZ)} mm · 拖动或按左右键移动观察面</title>
      <line x1={x(probeZ)} x2={x(probeZ)} y1="64" y2={222+extra} strokeDasharray="4 4" />
      <circle className="bench-probe-grip" cx={x(probeZ)} cy="61" r="10" />
      <path d={`M ${x(probeZ)-5} 59 h10 l-5 7 z`}/>
      <rect x={x(probeZ)-11} y="52" width="22" height="22" fill="transparent" stroke="none" />
    </g>}
    {visibleRows.map(row => {
      const gap = adjustableGap(project, row.index);
      const z = elementMarkerZ(project,row);
      if (!inView(z)) return null;
      const handle = x(z);
      const selected = row.index === activeIndex;
      const movable = span > 0 && Boolean(positionControl(project, row.index, positions)) && !disabled;
      const rowY = gap ? 238+extra : mid;
      const sag=7*Math.sign(row.element.radius_mm || 0)*(project.system.mode==='standing_wave' && row.index===0?-1:1);
      return <g key={row.element.uid || row.index} className={'bench-handle' + (selected ? ' selected' : '') + (movable ? ' movable' : '')}
        {...svgButton(el=>{onSelect(row.index);onInspect(el);},true)}
        aria-label={'选择 ' + row.element.name + '（' + (row.index + 1) + '）'} aria-pressed={selected}
        onFocus={() => onSelect(row.index)}
        onPointerDown={e=>startHostMove(e,row.index)}>
        <title>{row.element.name} · {positionText(z)} mm</title>
        {selected && <text x={handle} y="51" textAnchor={handle-left<100?'start':axisEnd-handle<100?'end':'middle'}>{gap ? '段末' : '入射面'} · {positionText(z)} mm</text>}
        {gap ? <path className="bench-glyph" d={'M' + (handle - 6) + ' '+(rowY-10)+' l6 9 6 -9 z'} /> :
          row.element.kind === 'lens' ? <ThinLensSymbol className="bench-glyph" x={handle} y={mid} halfHeight={44} focal={row.element.focal_mm}/> :
          row.element.kind === 'mirror' && sag ? <path className="bench-glyph bench-mirror" strokeWidth="4"
            d={`M ${handle-sag} ${mid-48} Q ${handle+sag} ${mid} ${handle-sag} ${mid+48}`}/> :
          <line className="bench-glyph" x1={handle} x2={handle} y1={mid-50} y2={mid+50} strokeWidth={row.element.kind === 'mirror' ? 6 : 3} />}
        <rect x={handle - 12} y={rowY - (gap ? 13 : 46)} width="24" height={gap ? 28 : 94} fill="transparent" />
      </g>;
    })}
    <g pointerEvents="none">
      {engineering ? visibleProjections.map(trace=><EngineeringBeam key={trace.layer+'/'+trace.uid} profile={trace} axis={projection}
        uid={trace.uid} layer={trace.layer} x={x} y={y} tolerance={.25*range/vertical}/>) : <>
      {draw(saved, true)}{drawIncident(saved,true)}
      {previous && previous!==saved && <g className="bench-previous-profile" data-previous-profile="true">
        {draw(previous, false)}{drawIncident(previous,false)}</g>}
      {draw(result, false)}{drawIncident(result,false)}</>}
    </g>
    {inView(probeSample?.z_mm) && stationMarks.map(({uid,axis,radius})=>{
      const primary = uid==='primary';
      const name = primary ? (project.system.mode==='single_pass'?'主光束':'腔模') : activeRoute.path.name;
      return <g key={uid+axis} data-probe-beam={uid} data-radius-mm={radius} aria-hidden="true" pointerEvents="none"
        className={'bench-station-mark '+(primary?'primary':'incident')} transform={'translate('+x(probeSample.z_mm)+' '+y(radius)+')'}>
        <title>{name} · {axis} 半径 {number(radius*1000)} μm</title>
        {primary ? <path d="M0 -7 7 0 0 7 -7 0Z"/> : <circle r="3.5"/>}
      </g>;
    })}
    {observations.filter(({reading})=>inView(reading.z_mm)).map(({reading, axis, radius}) => {
      const px=x(reading.z_mm), py=y(radius), axisName=axis==='tangential'?'T':'S';
      const label=`实测 ${axisName} 半径 ${number(radius*1000)} μm · z = ${positionText(reading.z_mm)} mm · 工程 #${reading.revision_id} · ${reading.source_path}`;
      return <g key={reading.id+axis} role="button" tabIndex={0} aria-label={'打开原件 · '+label}
        className={'bench-observation'+(reading.beam_uid==='primary'?'':' incident')} data-reading={reading.id}
        data-axis={axis} data-z-mm={reading.z_mm} data-radius-mm={radius} onClick={()=>onOpenReading(reading)}
        onKeyDown={e=>{if(e.key==='Enter' || e.key===' '){e.preventDefault();onOpenReading(reading);}}}>
        <title>{label} · {reading.wavelength_nm} nm · {reading.locator} · {reading.conditions}</title>
        {axis==='tangential' ? <circle cx={px} cy={py} r="4"/> : <rect x={px-6} y={py-6} width="12" height="12"/>}
      </g>;
    })}
    </g>
    <g transform={'translate(0 '+extra+')'}><IncidentRails project={project} routes={routes} activeUid={activeRoute?.path.uid} selected={armSelection}
      x={boundedX} inView={inView} overlaps={overlaps} left={left} right={right} disabled={disabled}
      onSelect={onSelectArm} onSelectHost={onSelect} onInspect={onInspect} startHostMove={startHostMove} start={start}/></g>
  </svg>
  <PlotNavigation view={view} bounds={bounds} focusZ={focusZ} focusName={focusName} onChange={changeCamera}
    transverseScale={transverseScale} onTransverseScale={setTransverseScale}/>
  {plotTarget?.reveal && !Number.isFinite(focusZ) && <small role="status" className="bench-hint">
    {onProbe?'观察面':'所选光学对象'}尚无有效轴向位置。{onProbe?'请核对观察面的参考位置及范围。':'请核对支路的接入位置。'}</small>}
  <BeamReadingRail readings={readings.filter(r=>inView(r.z_mm))} width={width} x={x} left={left} right={right} onOpenReading={onOpenReading}
    beamName={uid=>uid==='primary'?'主光束':routes.find(r=>r.path.uid===uid)?.path.name || uid}/>
  {opened && <div ref={chooser} id={chooserId} className="bench-object-chooser" role="group" aria-label="选择相邻元件"
    onKeyDown={e=>{if(e.key==='Escape'){e.preventDefault();closeChooser();}}}>
    <strong>选择元件</strong>
    {opened.rows.map(row=><button type="button" key={row.element.uid || row.index} aria-pressed={activeIndex===row.index}
      onClick={()=>{onSelect(row.index);closeChooser();onInspect(trigger.current);}}>{row.index+1}. {row.element.name}<small>{positionText(row.start)} mm</small></button>)}
    <button type="button" onClick={closeChooser}>关闭</button>
  </div>}</>;
}
