import React, {useMemo, useRef, useState} from 'react';
import {localId} from '../api/localId';
import DeviceParameters from './DeviceParameters';
import {InspectorHeading,InspectorToggle} from './BenchInspector';
import LiveLayoutStudy from './LiveLayoutStudy';
import {BeamSourceFields} from './BeamSourceFields';
import {componentLabels, connectionAt, portsOf, networkSelections, networkInputReferences, pathFaces, pathsOf} from './layoutSelections';
import {benchLayout} from './benchGeometry';
import {svgButton,usePointerGesture} from './usePointerGesture';
import NetworkViewport from './NetworkViewport';
import Numeric,{NetworkFieldContext} from './NetworkNumeric';
import {portHitHeight} from './networkCamera';
import './NetworkBench.css';

const blank = {components:[], links:[], drives:[], basis:''};
const builtinModels = {
  coupler: {label:'分束耦合器', model:{kind:'coupler', through_fraction:0.5}},
  reflector: {label:'反射面', model:{kind:'reflector', reflectance:1, phase_deg:180}},
  matrix: {label:'自定义散射元件', model:{kind:'matrix', ports:['1','2'], matrix:[[[0,0],[1,0]],[[1,0],[0,0]]]}},
};
const terminalKey = t => JSON.stringify([t.component,t.port]);
const same = (a,b) => a && b && a.component===b.component && a.port===b.port;
const fmt = v => Number.isFinite(v) ? Number(v.toPrecision(5)).toString() : '—';
const slots = c => Math.ceil(portsOf(c).length/2);
const pinLabel = (port, index) => Array.from(port).length <= 6 ? port : '#' + (index+1);

function PhaseSlider({disabled, project, link, beginGesture, onEdit}) {
  const pointer=usePointerGesture(disabled);
  const update=useRef(null), cancelled=useRef(false);
  const phase=link.phase_deg??0;
  const thumb=Math.abs(phase)<=180 ? phase : (((phase%360)+540)%360)-180;
  function changeProject(degrees) {
    if (degrees===thumb) return project;
    const network=project.network;
    return {...project,network:{...network,links:network.links.map(l=>l===link?{...l,phase_deg:degrees}:l)}};
  }
  function begin(event) {
    let transaction;
    if (pointer.begin(event,event.currentTarget,{
      finish() { update.current=null; transaction.finish(); },
      cancel() { update.current=null; cancelled.current=true; transaction.cancel(); },
    })) {
      cancelled.current=false;
      transaction=beginGesture(project,'微调连接相位');
      update.current=degrees=>transaction.update(changeProject(degrees));
    }
  }
  return <label>微调相位<input type="range" min="-180" max="180" step="1" aria-label="微调连接相位"
    aria-valuetext={phase+'°'} value={thumb} onPointerDown={begin}
    onBlur={e=>{
      const doc=e.currentTarget.ownerDocument;
      if(e.relatedTarget instanceof Element && e.relatedTarget.ownerDocument===doc && !doc.hidden &&
        doc.hasFocus() && pointer.active.current){
        // Moving to another field commits this value. A subsequent native
        // capture loss belongs to the old drag, even while the mouse is held.
        cancelled.current=true;
        pointer.active.current.finish();
      }
    }}
    onKeyDown={e=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','PageUp','PageDown','Home','End'].includes(e.key))cancelled.current=false;}}
    onChange={e=>{
      const value=Number(e.target.value);
      if (update.current) update.current(value);
      else if (!cancelled.current) onEdit(changeProject(value));
    }}/></label>;
}

function Diagram({network,labels,chosen,locateRequest,select,inspect,beginMove,disabled,result,references,openReference}) {
  const svg=useRef(null);
  const pointer=usePointerGesture(disabled);
  const [moving,setMoving]=useState(null);
  const nodes=network.components;
  const nodeNames=useMemo(()=>{
    const pen=document.createElement('canvas').getContext('2d');
    if(!pen)return labels;
    pen.font='12px system-ui, sans-serif';
    return new Map([...labels].map(([uid,name])=>{
      if(pen.measureText(name).width<=114)return [uid,name];
      // Keep both ends, including the distinguishing number in a catalog or station name.
      const chars=Array.from(name);
      let low=0, high=chars.length-1, best='…';
      while(low<=high){
        const take=Math.floor((low+high)/2), tail=Math.floor(take/2);
        const text=chars.slice(0,take-tail).join('')+'…'+chars.slice(chars.length-tail).join('');
        if(pen.measureText(text).width<=114){best=text;low=take+1;}else high=take-1;
      }
      return [uid,best];
    }));
  },[labels]);
  const maxSlots=Math.max(2,...nodes.map(slots));
  const rowGap=Math.max(140,maxSlots*22+80);
  const coords=new Map(nodes.map((c,i)=>[c.uid, moving?.uid===c.uid ? moving.xy :
    c.diagram_xy ?? [110+(i%3)*215,90+Math.floor(i/3)*rowGap]]));
  const minX=Math.min(...Array.from(coords.values(),xy=>xy[0]-155));
  const minY=Math.min(...nodes.map(c=>coords.get(c.uid)[1]-slots(c)*11-36));
  const maxX=Math.max(...Array.from(coords.values(),xy=>xy[0]+165));
  const maxY=Math.max(...nodes.map(c=>coords.get(c.uid)[1]+slots(c)*12+65));
  const ends=new Map();
  for(const c of nodes) {
    const [x,y]=coords.get(c.uid), half=slots(c);
    portsOf(c).forEach((p,i)=>{
      const right=i>=half;
      ends.set(terminalKey({component:c.uid,port:p}),[x+(right?65:-65),y+(i%half-(half-1)/2)*22,right?1:-1]);
    });
  }
  function begin(e,c,at) {
    const target=svg.current;
    const inverse=target.getScreenCTM().inverse();
    const point=ev=>new DOMPoint(ev.clientX,ev.clientY).matrixTransform(inverse);
    const start=point(e), [x,y]=coords.get(c.uid);
    const viewBox=target.getAttribute('viewBox');
    let xy=null, transaction;
    if(pointer.beginDrag(e,target,{
      press:trigger=>inspect(at,trigger),
      start() { transaction=beginMove(c.uid); },
      move(ev) {
        const p=point(ev);
        if(p.x===start.x && p.y===start.y) { xy=null; setMoving(null); return; }
        xy=[Math.round(Math.max(0,Math.min(10000,x+p.x-start.x))),
          Math.round(Math.max(0,Math.min(10000,y+p.y-start.y)))];
        setMoving({uid:c.uid,xy,viewBox});
      },
      finish() {
        transaction.finish(xy && (xy[0]!==x || xy[1]!==y) ? xy : null);
        setMoving(null);
      },
      cancel() { transaction.cancel(); setMoving(null); },
    })) {
      e.preventDefault();
      e.currentTarget.focus({preventScroll:true});
      select(at);
    }
  }
  const readings=new Map((result?.ports||[]).map(p=>[terminalKey(p),p]));
  let nextY=minY+16;
  const inputs=references.filter(r=>ends.has(terminalKey(r.at))).sort((a,b)=>
    ends.get(terminalKey(a.at))[1]-ends.get(terminalKey(b.at))[1]).map(r=>{
    const end=ends.get(terminalKey(r.at));
    const y=Math.max(nextY,end[1]-27),boxHeight=25+20*r.rows.length;
    nextY=y+boxHeight+12;
    return {...r,end,y,boxHeight};
  });
  const left=inputs.length?minX-167:minX;
  const bottom=inputs.length?Math.max(maxY,nextY):maxY;
  const center=coords.get(chosen?.component);
  return <NetworkViewport svg={svg} bounds={{x:left,y:minY,width:maxX-left,height:bottom-minY}}
    focus={center && {uid:chosen.component,x:center[0],y:center[1]}} locateRequest={locateRequest} frozen={moving?.viewBox}>
    {inputs.map(({at,rows,end,y,boxHeight})=>{
      const x=left+6, right=x+146, center=y+boxHeight/2;
      const beams=[...new Set(rows.map(r=>r.beam || '原光路待核对'))];
      return <g className="port-upstream" key={terminalKey(at)}>
        <path d={`M ${right} ${center} C ${right+42} ${center} ${end[0]+end[2]*38} ${end[1]} ${end[0]} ${end[1]}`}/>
        <rect x={x} y={y} width="146" height={boxHeight} rx="5"/>
        <foreignObject x={x+8} y={y+2} width="130" height="19">
          <div className="source-caption upstream-title">{beams.join(' / ')}</div>
        </foreignObject>
        {rows.map((r,i)=>{
          const label=[r.quantity,...(beams.length>1?[r.beam]:[]),r.label].join(' · '),top=y+19+20*i;
          const name='查看'+r.quantity+'来源 '+(r.beam || '')+' '+r.label;
          return <g key={r.quantity} role={r.target?'button':undefined} tabIndex={r.target?0:undefined} aria-label={name}
            onClick={()=>r.target && openReference(r.target)} onKeyDown={e=>{
              if(r.target && ['Enter',' '].includes(e.key)){e.preventDefault();openReference(r.target);}
            }}>
            <rect x={x+3} y={top} width="140" height="20" className="upstream-hit"/>
            <foreignObject x={x+8} y={top} width="130" height="20" pointerEvents="none">
              <div className="source-caption">{label}</div>
            </foreignObject>
            <title>{name}</title>
          </g>;
        })}
      </g>;
    })}
    {network.links.map(link=>{
      const a=ends.get(terminalKey(link.a)),b=ends.get(terminalKey(link.b));
      if(!a || !b)return null;
      const bend=Math.max(35,Math.min(70,Math.abs(a[0]-b[0])/2));
      const path=`M ${a[0]} ${a[1]} C ${a[0]+a[2]*bend} ${a[1]} ${b[0]+b[2]*bend} ${b[1]} ${b[0]} ${b[1]}`;
      return <g key={terminalKey(link.a)} tabIndex="0" role="button" aria-label={'选择连接 '+link.a.component+' / '+link.a.port+' 至 '+link.b.component+' / '+link.b.port}
        onClick={e=>inspect(link.a,e.currentTarget)} onKeyDown={e=>{if([' ','Enter'].includes(e.key)){e.preventDefault();inspect(link.a,e.currentTarget);}}}>
        <path d={path} className="link-target"/><path d={path} className={same(link.a,chosen)||same(link.b,chosen)?'selected-link':'port-link'}/>
        {(link.length_mm>0 || !!link.phase_deg) && <text className="link-note" x={(a[0]+b[0])/2} y={(a[1]+b[1])/2-5} textAnchor="middle">
          {[link.length_mm>0 && (link.length_mm<.001?fmt(link.length_mm*1e6)+' nm':fmt(link.length_mm)+' mm'),link.phase_deg && fmt(link.phase_deg)+'°'].filter(Boolean).join(' · ')}
        </text>}<title>物理长度 {link.length_mm} mm · 附加相位 {link.phase_deg??0}°</title></g>;
    })}
    {nodes.map(c=>{
      const [x,y]=coords.get(c.uid),ports=portsOf(c),half=slots(c),label=labels.get(c.uid);
      const at=chosen?.component===c.uid?chosen:{component:c.uid,port:ports[0]};
      return <g key={c.uid}>
        <g className="port-body" {...svgButton(el=>inspect(at,el),true)} aria-label={'选择元件 '+label} onPointerDown={e=>begin(e,c,at)}>
          <rect x={x-62} y={y-half*11-25} width={124} height={half*22+32} rx={6} className={c.uid===chosen?.component?'selected-node':''}/>
          <text x={x} y={y-half*11-10} textAnchor="middle">{nodeNames.get(c.uid)}</text>
          <title>{label} · 拖动调整示意位置</title>
        </g>
        {ports.map((p,index)=>{
          const t={component:c.uid,port:p},key=terminalKey(t),[px,py,sign]=ends.get(key);
          const reading=readings.get(key),source=network.drives.some(d=>same(d.at,t));
          const wattsLabel=reading?.external ? (source?'入 '+fmt(reading.incoming_w):'出 '+fmt(reading.outgoing_w))+' W' : '';
          const outward=wattsLabel?Math.max(12,12+wattsLabel.length*6):12;
          return <g key={p} tabIndex="0" role="button" aria-label={'选择 '+label+' 端口 '+p} onClick={e=>inspect(t,e.currentTarget)}
            onKeyDown={e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();inspect(t,e.currentTarget);}}}
            className={'port-pin'+(source?' source':'')+(same(chosen,t)?' active':'')}>
            <rect x={px-(sign>0?25:outward)} y={py-13} width={25+outward} height={portHitHeight} className="pin-target"/><circle cx={px} cy={py} r={4}/>
            <text x={px-sign*9} y={py+3} textAnchor={sign>0?'end':'start'}>{pinLabel(p,index)}</text>
            {wattsLabel && <text className="port-watts" x={px+sign*8} y={py-5} textAnchor={sign>0?'start':'end'}>{wattsLabel}</text>}
            <title>{label} / {p}{reading?' · 入射 '+fmt(reading.incoming_w)+' W · 出射 '+fmt(reading.outgoing_w)+' W':''}</title>
          </g>;
        })}
      </g>;
    })}
  </NetworkViewport>;
}

function MatrixFields({component,change}) {
  const [text,setText]=useState(null),[issue,setIssue]=useState('');
  const model=component.model;
  return <details><summary>散射矩阵与自定义端口</summary><p>每行为出射、每列为入射。复振幅用 [实部, 虚部] 表示。</p>
    <textarea aria-label="端口与散射矩阵 JSON" rows="6" value={text??JSON.stringify({ports:model.ports,matrix:model.matrix})} onChange={e=>setText(e.target.value)}/>
    <button type="button" onClick={()=>{try{const v=JSON.parse(text??JSON.stringify(model));
      if(!Array.isArray(v.ports)||!v.ports.length||!v.ports.every(p=>typeof p==='string'&&p.trim())||
        new Set(v.ports).size!==v.ports.length||v.ports.length>16||!Array.isArray(v.matrix)||v.matrix.length!==v.ports.length||
        !v.matrix.every(row=>Array.isArray(row)&&row.length===v.ports.length&&row.every(z=>Array.isArray(z)&&z.length===2&&z.every(Number.isFinite))))
        throw Error('请核对端口名称、方阵尺寸和复数格式。');
      change({...model,ports:v.ports,matrix:v.matrix});setText(null);setIssue('');
    }catch(e){setIssue(e.message);}}}>应用矩阵</button>{issue&&<p role="alert">{issue}</p>}
    <Numeric scope="component" label="矩阵标定波长 / nm" optional min={1e-12} placeholder="常量矩阵" value={model.reference_wavelength_nm} change={reference_wavelength_nm=>change({...model,reference_wavelength_nm})}/>
    <p>留空表示在本次试算中采用常量矩阵。</p></details>;
}

function DriveProfile({profile, layout, change}) {
  const paths=pathsOf(layout).filter(p=>p.uid!=='primary' || (layout.system.mode==='single_pass' && layout.system.elements.length));
  const geometry=benchLayout(layout);
  const exits=pathFaces(geometry,profile?.beam_uid).map(({element,index,scope})=>({
    value:JSON.stringify(['exit',scope,element.uid]),label:(scope==='arm'?'支路 ':'主路 ')+(index+1)+' · '+element.name+' 出射面',
  }));
  const planeLabels=componentLabels(layout.observation_planes || []);
  const choices=[
    ...(layout.observation_planes || []).map(p=>({value:JSON.stringify(['plane',p.uid]),label:planeLabels.get(p.uid)})),
    ...exits,
  ];
  const ref=profile?.element_exit;
  const currentRef=ref?JSON.stringify(['exit',ref.scope,ref.element_uid]):profile?.plane_uid?JSON.stringify(['plane',profile.plane_uid]):'';
  function setReference(value) {
    const [kind,id,element]=value?JSON.parse(value):[];
    change({kind:'layout',beam_uid:profile.beam_uid,...(kind==='plane' && {plane_uid:id}),
      ...(kind==='exit' && {element_exit:{scope:id,element_uid:element}})});
  }
  return <details><summary>入射横向光场</summary>
    <label>光场来源<select aria-label="光场来源" value={profile?.kind || ''} onChange={e=>{
      const kind=e.target.value;
      change(kind==='gaussian' ? {kind,tangential:{radius_mm:null,m2:1,wavefront:'waist'}}
        : kind==='layout' ? {kind,beam_uid:paths[0].uid} : null);
    }}><option value="">未指定</option><option value="gaussian">理想高斯光源</option>
      <option value="layout" disabled={!paths.length}>工程参考面上的复光场</option></select></label>
    {profile?.kind==='gaussian' && <BeamSourceFields source={profile} change={change}/>}
    {profile?.kind==='layout' && <><label>来源光路<select aria-label="来源光路" value={profile.beam_uid} onChange={e=>change({kind:'layout',beam_uid:e.target.value})}>
      {!paths.some(p=>p.uid===profile.beam_uid) && <option value={profile.beam_uid}>原光路不可用</option>}
      {paths.map(p=><option key={p.uid} value={p.uid}>{p.name}</option>)}</select></label>
      <label>来源参考面<select aria-label="来源参考面" value={currentRef} onChange={e=>setReference(e.target.value)}>
        <option value="">光路末端</option>{currentRef && !choices.some(c=>c.value===currentRef) &&
          <option value={currentRef}>原参考面已移除</option>}
        {choices.map(c=><option key={c.value} value={c.value}>{c.label}</option>)}</select></label>
      <p>采用该面的横向振幅和波前。功率取端口实际接收值，相干驱动的载波相位以本端口为参考。</p></>}
  </details>;
}

export default function NetworkBench({inspector,project,result,onEdit,beginGesture,disabled,error,onRetry,onAsk,revision,previewRevision,onUseQuantity,devices,caseId,liveStudies,layout,opticalLayout,selection,setSelection,openReference,notifyInput}) {
  const models={...builtinModels};
  for(const [name,device] of Object.entries(devices || {})) {
    if(device.representation==='scattering') models['study:'+name]={label:device.title,
      model:{kind:'registered',ports:device.ports,prescription:device.prescription}};
  }
  const network=project.network??blank,solved=result?.network;
  const issues=result?.input_issues?.filter(issue=>issue.path?.[0]==='network') || [];
  const wavelength=network.wavelength_nm??project.system.wavelength_nm;
  const [kind,setKind]=useState('coupler');
  const canvas=useRef(null);
  const [locateRequest,setLocateRequest]=useState(0);
  const locateComponent=component=>{
    setSelection({component});
    setLocateRequest(value=>value+1);
    const svg=canvas.current?.querySelector('svg.port-diagram');
    svg?.scrollIntoView({block:'center'});svg?.focus({preventScroll:true});
  };
  const update=patch=>onEdit({...project,network:{...network,...patch}});
  const component=network.components.find(c=>c.uid===selection?.component ||
    (selection?.instance && c.component_origin?.instance_uid===selection.instance))??network.components[0];
  const port=component && (portsOf(component).includes(selection?.port)?selection.port:portsOf(component)[0]);
  const at=component && {component:component.uid,port};
  const uid=component?.uid;
  const labels=useMemo(()=>componentLabels(network.components),[network.components]);
  const selections=useMemo(()=>networkSelections(layout,uid,port,labels),[layout,uid,port,labels]);
  const replaceComponent=(uid,patch)=>update({components:network.components.map(c=>c.uid===uid?{...c,...patch}:c)});
  const beginMove=uid=>{
    const transaction=beginGesture(project,'排列端口图元件');
    return {
      finish(xy) {
        if (xy) transaction.update({...project,network:{...network,components:network.components.map(c=>
          c.uid===uid?{...c,diagram_xy:xy}:c)}});
        transaction.finish();
      },
      cancel:transaction.cancel,
    };
  };
  const {link,drive,peer:activeLinkEnd}=at ? connectionAt(network, at) : {};
  const free=t=>!network.links.some(l=>same(l.a,t)||same(l.b,t)) && !network.drives.some(d=>same(d.at,t));
  const choices=network.components.flatMap(c=>portsOf(c).map(p=>({component:c.uid,port:p,label:labels.get(c.uid)+' / '+p}))).filter(t=>!same(t,at)&&free(t));
  const ports=solved?.ports ?? [], islands=solved?.islands ?? [];
  const reading=ports.find(t=>same(t,at));
  const selectedIsland=islands.find(g=>g.components.includes(uid));
  const otherIslands=islands.filter(g=>g.status==='completed' && g!==selectedIsland);
  const warnings=solved?.warnings || [];
  const islandIssues=islands.filter(g=>g.message);
  const names=new Map(network.components.map(c=>[c.uid,c.name]));
  const issueTexts=new Set(islandIssues.map(g=>g.components.map(u=>names.get(u)).join('、')+' · '+g.message));
  const extraWarnings=warnings.filter(w=>!issueTexts.has(w));
  const balance=g=><p key={g.components[0]} className="network-balance">{g.components.map(u=>labels.get(u)).join('、')} · 单模参考 · 入射 {fmt(g.input_w)} W · 外部出射 {fmt(g.external_w)} W · 被动损耗 {fmt(g.removed_w)} W</p>;
  const setLink=patch=>update({links:network.links.map(l=>l===link?{...l,...patch}:l)});
  const setDrive=params=>update({drives:[...network.drives.filter(d=>!same(d.at,at)),...(params?[{at,...params}]:[])]});
  const feedKey=v=>JSON.stringify([v.path_uid,v.scope,v.element_uid]);
  const feeds=(result?.sections?.power_budgets??[]).filter(b=>b.wavelength_nm===wavelength)
    .flatMap(b=>b.rows.map(r=>({ref:{path_uid:b.path_uid,scope:r.scope,element_uid:r.uid},label:b.name+' · '+r.name})));
  const sourceValue=drive?.path_output?feedKey(drive.path_output):drive?'manual':'';
  function add(){
    const uid=localId(),entry=models[kind];
    const c={uid,name:entry.label+' '+(network.components.length+1),model:structuredClone(entry.model)};
    update({components:[...network.components,c]});setSelection({component:uid,port:portsOf(c)[0]});
  }
  function remove(){
    const uid=component.uid;
    update({components:network.components.filter(c=>c.uid!==uid),links:network.links.filter(l=>l.a.component!==uid&&l.b.component!==uid),drives:network.drives.filter(d=>d.at.component!==uid)});
    setSelection(null);
  }
  return <NetworkFieldContext.Provider value={{
    network:{identity:'network',owner:''},component:{identity:uid,owner:labels.get(uid)},
    terminal:{identity:at && terminalKey(at),owner:at && (labels.get(uid)+' / '+port)},notify:notifyInput,
  }}>
    <section className="network-bench" aria-label="端口光路">
    <div className="network-canvas" id="current-optical-canvas" role="group" aria-label="端口连接画布" tabIndex={-1} ref={canvas}><header><strong>端口连接图</strong><span>{wavelength==null?'波长待补':fmt(wavelength)+' nm'} · 图上功率为单模匹配参考</span>
      <InspectorToggle inspector={inspector} label="端口属性"/></header>
      {issues.map(issue=><p key={issue.path.join('/')} role="status">{issue.message} {issue.path[1]==='wavelength_nm' &&
        <button type="button" className="bench-text-btn" onClick={event=>{
          const panel=event.currentTarget.closest('.network-bench');inspector.open(event.currentTarget);
          requestAnimationFrame(()=>panel?.querySelector('input[aria-label="网络波长 / nm"]')?.focus());}}>补充网络波长 ↗</button>}</p>)}
      {network.components.length ? <Diagram network={network} labels={labels} chosen={at} locateRequest={locateRequest} select={setSelection} disabled={disabled} result={solved}
        inspect={(at,trigger)=>{setSelection(at);inspector.open(trigger);}}
        references={networkInputReferences(layout,benchLayout(layout),result?.sections)} openReference={openReference}
        beginMove={beginMove}/> : <p>先放置元件，再选择端口建立连接。</p>}
      <p className="bench-hint">点端口可查看功率，拖动空白处可平移局部视野。拖动元件调整图面排布，连接的物理长度在属性中设置。</p>
      {selectedIsland?.status==='completed' && balance(selectedIsland)}
      {selectedIsland?.message && warnings.length>1 && <p role="status">{labels.get(uid)} · {selectedIsland.message}</p>}
      {otherIslands.length>0 && <details className="network-balances"><summary>其他 {otherIslands.length} 个子网络的功率收支</summary>
        {otherIslands.map(balance)}</details>}
      {warnings.length===1 && <p role="status">{warnings[0]}</p>}
      {warnings.length>1 && <details className="network-balances"><summary>全部网络提示 · {warnings.length} 项</summary>
        {islandIssues.map(g=><p key={g.components[0]}>{g.components.map(u=><button key={u} type="button" className="bench-text-btn"
          aria-label={'在端口图定位 '+labels.get(u)} onClick={()=>locateComponent(u)}>{labels.get(u)} ↗</button>)} · {g.message}</p>)}
        {extraWarnings.map((w,i)=><p key={i}>{w}</p>)}</details>}
      {error && <p role="alert">{error} <button type="button" onClick={onRetry}>重算</button></p>}
      {network.components.length>0 && !error && !result && <p role="status">正在计算当前连接…</p>}
      {network.components.length>0 && result && !solved && !issues.length && <p role="status">本次计算没有可用的端口功率结果。</p>}
      {component && <LiveLayoutStudy caseId={caseId} methods={liveStudies} layout={layout} opticalLayout={opticalLayout} selections={selections} revision={revision} previewRevision={previewRevision} disabled={disabled}/>}
    </div>
    <aside className="network-inspector-pane" aria-label="端口属性编辑" {...inspector.props}>
      <InspectorHeading inspector={inspector} title={labels.get(uid) || '端口属性'}/>
    <fieldset className="network-inspector" disabled={disabled}><div className="network-row"><label>元件类型<select aria-label="元件类型" value={kind} onChange={e=>setKind(e.target.value)}>
      {Object.entries(models).map(([key,v])=><option key={key} value={key}>{v.label}</option>)}</select></label><button type="button" onClick={add}>添加元件</button></div>
      <Numeric scope="network" label="网络波长 / nm" value={network.wavelength_nm ?? null} optional min={1e-12} max={1e6}
        placeholder={project.system.wavelength_nm!=null?'沿用主路 '+fmt(project.system.wavelength_nm)+' nm':'待补'}
        change={wavelength_nm=>update({wavelength_nm})}/>
      {project.system.wavelength_nm!=null && network.wavelength_nm!=null && <button type="button" onClick={()=>update({wavelength_nm:null})}>沿用主路波长</button>}
      {component && <><div className="network-row"><label>元件<select aria-label="网络元件" value={component.uid} onChange={e=>setSelection({component:e.target.value})}>
        {network.components.map(c=><option key={c.uid} value={c.uid}>{labels.get(c.uid)}</option>)}</select></label>
        <label>端口<select aria-label="端口" value={port} onChange={e=>setSelection({...at,port:e.target.value})}>{portsOf(component).map((p,i)=><option key={p} value={p}>{pinLabel(p,i)===p?p:pinLabel(p,i)+' · '+p}</option>)}</select></label></div>
        <div className="network-reading" aria-live="polite">{reading ? <>单模参考 · 入射 <b>{fmt(reading.incoming_w)} W</b> · 出射 {Number.isFinite(reading.outgoing_w) && revision ?
          <button type="button" className="bench-quantity" aria-label="用端口出射功率继续分析" onClick={()=>onUseQuantity({revision_id:revision,
            match:{scope:'network',element_uid:component.uid,terminal:port},quantity:'optics.power',label:'出射功率',location:component.name+' / '+port})}>{fmt(reading.outgoing_w)} W</button>
          :<b>{fmt(reading.outgoing_w)} W</b>}</> : error || result ? '所选端口暂无功率读数' : '正在计算端口读数…'}</div>
        {component.component_origin && <p>模型来源 · {component.component_origin.name} v{component.component_origin.version} · {component.component_origin.wavelength_nm} nm。{component.component_origin.note}</p>}
        {link ? <><strong>连接至 {labels.get(activeLinkEnd.component)} / {activeLinkEnd.port}</strong>
          <Numeric label="连接长度 / mm" value={link.length_mm} min={0} change={length_mm=>setLink({length_mm})}/>
          <Numeric label="连接折射率" value={link.n??1} min={1e-12} change={n=>setLink({n})}/>
          <Numeric label="连接功率透过率" value={link.power_transmission??1} min={0} max={1} change={power_transmission=>setLink({power_transmission})}/>
          <details><summary>横向配准</summary><p>从 {labels.get(link.a.component)} / {link.a.port} 到 {labels.get(link.b.component)} / {link.b.port} 的光场中心位移。反向传播取反。</p>
            <Numeric label="T 位移 / mm" value={link.shift_t_mm??0} change={shift_t_mm=>setLink({shift_t_mm})}/>
            <Numeric label="S 位移 / mm" value={link.shift_s_mm??0} change={shift_s_mm=>setLink({shift_s_mm})}/></details>
          <Numeric label="附加相位 / °" value={link.phase_deg??0} change={phase_deg=>setLink({phase_deg})}/>
          <PhaseSlider key={JSON.stringify([link.a,link.b])} link={link} project={project}
            disabled={disabled} beginGesture={beginGesture} onEdit={onEdit}/>
          <button type="button" onClick={()=>update({links:network.links.filter(l=>l!==link)})}>解除连接</button></> :
          <><label>端口入射<select aria-label="端口入射" value={sourceValue} onChange={e=>{const v=e.target.value;
            const settings={...drive};delete settings.power_w;delete settings.path_output;
            setDrive(!v?null:{...settings,...(v==='manual'?{power_w:null}:{path_output:feeds.find(f=>feedKey(f.ref)===v).ref})});}}>
            <option value="">匹配端接</option><option value="manual">手填入射光源</option>
            {drive?.path_output&&!feeds.some(f=>feedKey(f.ref)===sourceValue)&&<option value={sourceValue}>工程出射 · {drive.path_output.element_uid}</option>}
            {feeds.map(f=><option key={feedKey(f.ref)} value={feedKey(f.ref)}>工程出射 · {f.label}</option>)}</select></label>
            {drive && <>{!drive.path_output && <Numeric label="端口入射功率 / W" optional value={drive.power_w} min={0} change={power_w=>setDrive({...drive,power_w})}/>}
              <label>相干组<input aria-label="相干组" placeholder="留空表示独立光源" value={drive.coherence_group??''} onChange={e=>setDrive({...drive,coherence_group:e.target.value||null})}/></label>
              {drive.coherence_group && <Numeric label="光源参考相位 / °" optional value={drive.phase_deg} change={phase_deg=>setDrive({...drive,phase_deg})}/>}
              <DriveProfile profile={drive.profile} layout={layout} change={profile=>setDrive({...drive,profile})}/></>}
            {!drive && <label>连接到<select aria-label="连接到" value="" onChange={e=>{if(!e.target.value)return;const [uid,p]=JSON.parse(e.target.value);
              update({links:[...network.links,{a:at,b:{component:uid,port:p},length_mm:0,n:1,power_transmission:1,phase_deg:0}]});}}>
              <option value="">选择空闲端口</option>{choices.map(t=><option key={terminalKey(t)} value={terminalKey(t)}>{t.label}</option>)}</select></label>}</>}
        <details><summary>元件参数</summary><label>元件名称<input key={component.uid+component.name} defaultValue={component.name} maxLength="100"
          onBlur={e=>{if(e.target.value.trim() && e.target.value!==component.name)replaceComponent(component.uid,{name:e.target.value.trim()});}}/></label>
          <label>横向场适用范围<select aria-label="横向场适用范围" value={component.transverse_action ?? ''}
            onChange={e=>replaceComponent(component.uid,{transverse_action:e.target.value || null})}>
            <option value="">采用元件模型的适用范围</option>
            <option value="uniform">照明区域内各点采用相同散射系数</option><option value="matched_mode">仅指定匹配模式</option>
          </select></label>
          {component.model.prescription && <DeviceParameters key={component.uid} devices={devices} recipe={component.model.prescription}
            onChange={prescription=>replaceComponent(component.uid,{model:{...component.model,prescription}})}/>}
          {component.model.kind==='coupler' && <><Numeric scope="component" label="直通功率比例" value={component.model.through_fraction} min={0} max={1}
            change={through_fraction=>replaceComponent(component.uid,{model:{...component.model,through_fraction}})}/>
            <p>1→3、2→4 为直通，交叉通道相移 +90°，四个端口均可逆向入射。</p></>}
          {component.model.kind==='reflector' && <><Numeric scope="component" label="反射功率比例" value={component.model.reflectance} min={0} max={1} change={reflectance=>replaceComponent(component.uid,{model:{...component.model,reflectance}})}/>
            <Numeric scope="component" label="反射相位 / °" value={component.model.phase_deg} change={phase_deg=>replaceComponent(component.uid,{model:{...component.model,phase_deg}})}/></>}
          {component.model.kind==='matrix' && <MatrixFields key={component.uid} component={component} change={model=>{
            const removed=t=>t.component===component.uid&&!model.ports.includes(t.port);
            if(network.links.some(l=>removed(l.a)||removed(l.b))||network.drives.some(d=>removed(d.at)))throw Error('请先解除待删除端口上的连接或光源。');
            replaceComponent(component.uid,{model});}}/>}
          <button type="button" onClick={remove}>移除元件及其连接</button></details></>}
      <details><summary>模型依据与范围</summary><p>{solved?.scope||'单色、线性被动散射。每个端口代表一个模式，连接两端的横模与偏振基底须对应。'}</p>
        <label>模型依据<textarea value={network.basis??''} onChange={e=>update({basis:e.target.value})}/></label>
        {solved?.origins?.map(o=><p key={terminalKey(o.terminal)}>工程取值 · {o.path_name} · {o.element_name} · {o.watts?.join(' 至 ')??'待补'} W</p>)}</details>
      <button type="button" onClick={()=>onAsk(component?'的「'+labels.get(uid)+'」端口 '+port:'',at)}>引用端口光路问 Agent</button>
    </fieldset></aside>
  </section></NetworkFieldContext.Provider>;
}
