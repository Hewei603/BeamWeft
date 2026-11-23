import React, {useEffect, useMemo, useRef, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import ScientificPlot from './ScientificPlot';
import {acceptanceInequality} from './studyLimits';

const stateNames={pass:'符合限值',outside:'超出限值',unknown:'依据不足'};
const format=value=>Number(value.toPrecision(10)).toString();
const numberText=(value,unit,signed=false)=>Number.isFinite(value)
  ? (signed && value>0?'+':'')+format(value)+(unit?' '+unit:'') : '待核对';
const keyOf=path=>JSON.stringify(path);

export function axisField(path,options) {
  const option=options.find(o=>o.key===keyOf(path));
  const label=option?.label || path.join(' › ');
  const lastLabel=label.split(' › ').at(-1);
  return {label,unit:option?.unit || (lastLabel.includes(' / ')?lastLabel.split(' / ').at(-1):'')};
}

function startingSelection(report,options) {
  const columns=report.recipe.axes.flatMap(axis=>axis.paths);
  const original=columns.map(path=>options.find(o=>o.key===keyOf(path))?.value);
  let row=report.rows.findIndex(r=>r.run_id===report.baseline_id);
  if(row<0)row=report.rows.findIndex(r=>r.coordinates.every((v,i)=>v===original[i]));
  return {row_index:row,axis_index:0,coordinate_index:0,scope:'slice'};
}

export function Limits({limits}) {
  return <ul className="tolerance-limits" aria-label="装调验收条件">{limits.map(m=><li key={m.output}>
    {acceptanceInequality(m)}
  </li>)}</ul>;
}

function Evidence({point,report,fields,selection,onOpen}) {
  const witness=point.witness_coordinates || report.rows[point.row_index]?.coordinates;
  const start=report.recipe.axes.slice(0,selection.axis_index).reduce((count,axis)=>count+axis.paths.length,0);
  const describe=(vector,from)=>vector.map((v,i)=>fields[from+i].label+' = '+format(v)).join('，');
  return <div className="tolerance-evidence">
    {selection.scope==='all' && <small>所采工况中，{point.counts.pass} 个达标、{point.counts.outside} 个超限、{point.counts.unknown} 个待核对。</small>}
    {point.issues?.length>0 ? <ul>{point.issues.map(issue=><li key={issue.output}>
      {issue.label}{Number.isFinite(issue.value) && <> {numberText(issue.value,issue.unit)}</>}
      {issue.lower!=null && <> · 下限 {numberText(issue.lower,issue.unit)}</>}{issue.upper!=null && <> · 上限 {numberText(issue.upper,issue.unit)}</>}
      {issue.message && !Number.isFinite(issue.value) && <small>{issue.message}</small>}
    </li>)}</ul> : point.state==='unknown' && point.reason && <small>{point.reason}</small>}
    {point.alternative_coordinates?.map((v,i)=><small key={i}>联动设定 · {describe(v,start)}</small>)}
    {witness && selection.scope==='all' && <small>证据工况 · {describe(witness,0)}</small>}
    {point.coordinates?.length>1 && selection.scope==='slice' && <small>联动设定 · {describe(point.coordinates,start)}</small>}
    {point.run_id && <button type="button" className="bench-text-btn" onClick={()=>onOpen(point.run_id,selection)}>查看计算 #{point.run_id}</button>}
  </div>;
}

function RefinementForm({side,unit,onRefine,busy}) {
  const [resolution,setResolution]=useState(''),[error,setError]=useState('');
  async function submit(e) {
    e.preventDefault();
    const value=Number(resolution);
    if(!(value>0 && Number.isFinite(value))){setError('请输入大于零的有限精度。');return;}
    setError('');
    try {await onRefine(side,value);} catch(cause){setError(cause.message);}
  }
  return <details className="tolerance-refine"><summary>缩小边界区间</summary>
    <form onSubmit={submit}><fieldset disabled={busy}>
      <label>边界精度{unit?' / '+unit:''}<input aria-label="边界精度" required type="number" min="0" step="any"
        value={resolution} onChange={e=>setResolution(e.target.value)}/></label>
      <small>在达标点和超限点之间加算中点，直到这两个点之间的距离达到指定精度，或遇到计算限制。</small>
      <button type="submit" className="btn btn-secondary">{busy?'正在计算…':'计算并保存边界'}</button>
      {error && <p role="alert">{error}</p>}
    </fieldset></form>
  </details>;
}

function Side({side,name,window,field,report,fields,selection,onOpen,onRefine,busy}) {
  const result=window[side],passed=result.passed,stop=result.stop;
  const sign=side==='lower'?-1:1;
  return <section className="tolerance-side" aria-label={name}><h5>{name}</h5>
    {passed && <><p>相邻达标点至 <strong>{numberText(passed.position,field.unit)}</strong>
      {Number.isFinite(result.margin) && <>，相对名义点 {numberText(sign*result.margin,field.unit,true)}</>}</p>
      <Evidence point={passed} {...{report,fields,selection,onOpen}}/></>}
    {stop && <><p>下一采样点 <strong>{numberText(stop.position,field.unit)}</strong> · {stateNames[stop.state]}</p>
      <Evidence point={stop} {...{report,fields,selection,onOpen}}/></>}
    {result.termination==='edge' && <small>已到扫描边缘，这一侧尚未找到超限点。</small>}
    {onRefine && result.termination==='outside' && passed && stop && <RefinementForm side={side} unit={field.unit} busy={busy} onRefine={onRefine}/>}
  </section>;
}

const stationStyles={
  pass:{label:'达标',symbol:'circle',color:'#72dac7'},
  outside:{label:'超限',symbol:'x',color:'#ffae80'},
  unknown:{label:'待核对',symbol:'diamond-open',color:'#e9c862'},
};

function StationPlot({data,field,onSelect,busy}) {
  const stations=data.stations;
  if(!stations?.length)return null;
  const traces=Object.entries(stationStyles).map(([state,style])=>{
    const matches=stations.filter(p=>p.state===state);
    return {type:'scatter',mode:'markers',name:style.label,x:matches.map(p=>p.position),y:matches.map(()=>style.label),
      customdata:matches.map(p=>p.row_index),marker:{symbol:style.symbol,size:10,color:style.color},
      hovertemplate:'%{x} '+field.unit+' · '+style.label+'<extra></extra>'};
  }).filter(trace=>trace.x.length);
  const nominal=data.nominal.position;
  const layout={height:143,autosize:true,margin:{l:46,r:13,t:20,b:43},showlegend:false,hovermode:'closest',
    paper_bgcolor:'#16252e',plot_bgcolor:'#16252e',font:{color:'#cddedc',size:11},
    xaxis:{title:{text:field.label.split(' › ').at(-1),standoff:3},nticks:6,zeroline:false,gridcolor:'#ffffff12',fixedrange:true},
    yaxis:{type:'category',categoryorder:'array',categoryarray:traces.map(t=>t.name).reverse(),showgrid:false,fixedrange:true},
    shapes:[{type:'line',x0:nominal,x1:nominal,y0:0,y1:1,xref:'x',yref:'paper',line:{color:'#ccdae1',width:1.4,dash:'dot'},layer:'below'}],
    annotations:[{x:nominal,y:1,xref:'x',yref:'paper',text:'名义',showarrow:false,yanchor:'bottom',font:{size:10,color:'#dae6ed'}}]};
  return <figure className="tolerance-plot"><figcaption>各采样位置的验收结果</figcaption>
    <ScientificPlot data={traces} layout={layout} showToolbar={false} ariaLabel="装调采样点分布，点击采样点选择名义候选"
      onPoint={(_,index)=>{if(!busy && Number.isInteger(index))onSelect(index);}}/>
  </figure>;
}

function WindowResult({data,report,fields,axisFields,selection,onOpen,onRefine,onAsk,onSelect,busy}) {
  const field=axisFields[selection.axis_index][selection.coordinate_index];
  const point=data.nominal;
  const values=report.rows[selection.row_index].coordinates;
  let coordinate=0;
  const fixed=axisFields.flatMap((group,i)=>group.map(f=>{
    const value=values[coordinate++];
    return i===selection.axis_index ? null : f.label+' = '+format(value);
  })).filter(Boolean);
  function ask() {
    const nominal=fields.map((f,i)=>f.label+' = '+format(values[i])).join('，');
    const scope=selection.scope==='all'?'要求其余参数组的所有已采样工况都达标':'其余参数固定在名义设定';
    const subject='关于比较 #'+report.id+' 的装调余量，名义设定为 '+nominal+'，沿「'+field.label+'」查看，'+scope+'。';
    const address='候选行 '+selection.row_index+'，参数组 '+selection.axis_index+'，组内排序坐标 '+selection.coordinate_index+'（均从 0 计）。';
    onAsk(subject+'\n'+address+'\n',{artifact_ids:[report.id]});
  }
  return <section aria-label="已采样的装调余量" className="tolerance-result">
    <Limits limits={data.limits}/>
    {selection.scope==='slice' && fixed.length>0 && <small>其余参数固定在 {fixed.join('，')}。</small>}
    <p>名义点 <strong>{numberText(point.position,field.unit)}</strong> · {stateNames[point.state]}。本次核对 {data.sample_count} 个采样位置。</p>
    <StationPlot {...{data,field,onSelect,busy}}/>
    <Evidence point={point} {...{report,fields,selection,onOpen}}/>
    {point.state==='pass'?<div className="tolerance-sides">
      {[['lower','较小取值方向'],['upper','较大取值方向']].map(([side,name])=>
        <Side key={side} window={data} {...{side,name,field,report,fields,selection,onOpen,onRefine,busy}}/>)}
    </div>:<p>从符合限值的名义点出发，才能查看两侧的装调余量。</p>}
    <small>结论覆盖本次采样的位置和工况。相邻两点之间仍可能超限。</small>
    {onAsk && <button type="button" className="bench-text-btn" onClick={ask}>引用当前装调余量问 Agent</button>}
  </section>;
}

export default function StudyTolerance({caseId,report,options,onOpen,onAsk,initial,installed,busy,onRefine}) {
  const [open,setOpen]=useState(Boolean(initial));
  const [selection,setSelection]=useState(()=>initial || startingSelection(report,options));
  const [reply,setReply]=useState(null),[attempt,retry]=useState(0);
  const header=useRef(null);
  const axisFields=useMemo(()=>report.recipe.axes.map(axis=>axis.paths.map(p=>axisField(p,options))),[options,report]);
  const fields=axisFields.flat(),chosen=axisFields[selection.axis_index];
  useEffect(()=>{if(initial)header.current?.focus();},[initial]);
  useEffect(()=>{
    if(!open || selection.row_index<0)return;
    const controller=new AbortController();
    workbenchApi.studyTolerance(caseId,{search_id:report.id,...selection},controller.signal)
      .then(data=>{if(!controller.signal.aborted)setReply({selection,attempt,data});})
      .catch(cause=>{if(!controller.signal.aborted)setReply({selection,attempt,error:cause.message});});
    return ()=>controller.abort();
  },[open,selection,attempt,caseId,report.id]);
  const current=reply?.selection===selection && reply.attempt===attempt ? reply : null;
  const edit=patch=>setSelection(old=>({...old,...patch}));
  const refine=installed && onRefine && chosen.length===1 && selection.scope==='slice'
    ? (side,resolution)=>onRefine({search_id:report.id,...selection,side,resolution}) : null;
  return <details className="study-tolerance" open={open} onToggle={e=>{if(e.target===e.currentTarget)setOpen(e.currentTarget.open);}}>
    <summary ref={header}>查看装调余量</summary>
    {open && <><div className="grid-fields tolerance-selectors">
      <label>名义候选<select aria-label="名义候选" value={selection.row_index} disabled={busy} onChange={e=>edit({row_index:Number(e.target.value)})}>
        <option value="-1">选择要装调的候选</option>{report.rows.map((r,i)=><option key={i} value={i}>
          {r.coordinates.map((v,j)=>numberText(v,fields[j].unit)).join(' · ')}{r.run_id?' · 计算 #'+r.run_id:''}
        </option>)}</select></label>
      {axisFields.length>1 && <label>扫描参数组<select aria-label="扫描参数组" disabled={busy} value={selection.axis_index}
        onChange={e=>edit({axis_index:Number(e.target.value),coordinate_index:0})}>{axisFields.map((group,i)=>
          <option value={i} key={i}>{group.map(f=>f.label).join(' ＋ ')}</option>)}</select></label>}
      {chosen.length>1 && <label>排序坐标<select aria-label="排序坐标" disabled={busy} value={selection.coordinate_index}
        onChange={e=>edit({coordinate_index:Number(e.target.value)})}>{chosen.map((f,i)=><option value={i} key={i}>{f.label}</option>)}</select></label>}
      {axisFields.length>1 && <label>其余参数组<select aria-label="其余参数组" disabled={busy} value={selection.scope} onChange={e=>edit({scope:e.target.value})}>
        <option value="slice">固定在名义候选的取值</option><option value="all">要求所有已采样工况均达标</option>
      </select></label>}
    </div>
      <small>检查 {chosen[selection.coordinate_index].label}。{chosen.length>1 && '同组参数按已存的联动组合一起变化。'}</small>
      {selection.row_index<0 ? <p>原计算的完整取值未出现在这组候选中，请选择名义候选。</p>
        : !current ? <p role="status">正在核对两侧的采样点…</p>
        : current.error ? <p role="alert">{current.error} <button type="button" onClick={()=>retry(n=>n+1)}>重试</button></p>
        : <WindowResult data={current.data} {...{report,fields,axisFields,selection,onOpen,onAsk,busy}} onRefine={refine}
          onSelect={index=>{if(index!==selection.row_index)edit({row_index:index});}}/>}
    </>}
  </details>;
}

const refineStatus={resolved:'已达到指定精度',budget:'已用完本轮加密点数',unknown:'中间采样点的依据不足',precision:'已到数值分辨率'};
export function RefinementExecution({report}) {
  const execution=report.refinement_execution || report.original_refinement_execution;
  if (!execution) return null;
  return <small>{report.refinement_execution?'本次执行':'首次保存时'} · 新计算 {execution.computed} 个，沿用原计算 {execution.reused} 个，
    无计算记录 {execution.without_record} 个。</small>;
}

export function RefinedBoundary({report,options,onSource,onOpen,busy}) {
  const r=report.refinement;
  const path=report.recipe.axes[r.axis_index].paths[r.coordinate_index],{label,unit}=axisField(path,options);
  const selection={row_index:r.row_index,axis_index:r.axis_index,coordinate_index:r.coordinate_index,scope:r.scope};
  return <div className="search-lineage" aria-label="边界加密结果">
    <p>{label} · {r.side==='lower'?'较小':'较大'}取值方向 · {refineStatus[r.status] || r.status}</p>
    <p>核对 {r.evaluations} 个中点。达标点 {numberText(r.pass_position,unit)}，超限点 {numberText(r.outside_position,unit)}。
      当前间距 {numberText(r.width,unit)}，指定精度 {numberText(r.requested_resolution,unit)}。</p>
    <RefinementExecution report={report}/>
    {r.blocked && <div role="note"><p>中点 {numberText(r.blocked.position,unit)} 待核对。{r.blocked.reason}</p>
      {r.blocked.issues?.map(issue=><small key={issue.output}>{issue.label} · {issue.message}</small>)}
      {r.blocked.run_id && <button type="button" className="bench-text-btn" onClick={()=>onOpen(r.blocked.run_id)}>查看中点计算 #{r.blocked.run_id}</button>}
    </div>}
    <button type="button" className="bench-text-btn" disabled={busy}
      onClick={()=>onSource(r.source_search_id,selection)}>返回比较 #{r.source_search_id} 的装调余量</button>
  </div>;
}
