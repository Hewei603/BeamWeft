import {isBuiltInProvider} from '../brand';
import React, {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import ScientificPlot from './ScientificPlot';
import StudyTolerance, {RefinedBoundary} from './StudyTolerance';
import StudyAdjustment from './StudyAdjustment';
import {adjustmentView} from './studyAdjustmentRoles';
import {resolveInputField} from './studyObjectSchema';
import {opticalParameterLabels} from './OpticalScanView';
import {modelFields} from './modelFields';
import {compareDeviations,deviationText} from './studyDeviation';
import {acceptanceInequality} from './studyLimits';

const emptyColumn=()=>({path:'',values:'',start:'',stop:''});
const emptyAxis=()=>({mode:'list',count:'5',columns:[emptyColumn()]});
const emptyMeasure=()=>({output:'',direction:'min',target:'',lower:'',upper:'',rationale:''});
const format=v=>typeof v==='number'?Number(v.toPrecision(7)).toString():v;
const withUnit=(value,unit)=>unit?value+' '+unit:String(value);

function inputOptions(record,history) {
  const options=[];
  const schema=record.definition.inputs || {};
  const opticalSystem=isBuiltInProvider(record.definition.provider) && schema.properties?.elements?.items?.$ref==='#/$defs/OpticalElement';
  const layoutPaths=new Map(Object.entries(history?.layout_parameters || {}).map(([root,items])=>
    [root,new Map(items.map(item=>[JSON.stringify(item.path),item]))]));
  function visit(value,path,labels,field) {
    if(typeof value==='number' && Number.isFinite(value)) {
      let label=labels.join(' › ');
      if(record.definition.input_ports?.[path[0]]?.quantity==='optics.ordered-layout.v1') {
        const setting=layoutPaths.get(path[0])?.get(JSON.stringify(path.slice(1)));
        if(!setting)return;
        if(setting.label)label=[...labels.slice(0,-1),setting.label].join(' › ');
      }
      options.push({key:JSON.stringify(path),label,value,description:field?.description});
    } else if(value && typeof value==='object' && !('attachment_id' in value)) {
      for(const [key,v] of Object.entries(value)) {
        const index=Array.isArray(value);
        const nested=resolveInputField(index?(field?.prefixItems?.[key] ?? field?.items):(field?.properties?.[key] ?? field?.additionalProperties),schema);
        const catalogLabel=isBuiltInProvider(record.definition.provider) && field?.$ref==='#/$defs/OpticalElement' && opticalParameterLabels[key];
        const title=nested?.title;
        const descriptive=title && title.toLowerCase().replaceAll(' ','_')!==key;
        const label=index?(v?.name || v?.uid || '第 '+(Number(key)+1)+' 项'):(descriptive && title) || catalogLabel || title || key;
        visit(v,[...path,index?Number(key):key],[...labels,label],nested);
      }
    }
  }
  for(const [key,v] of Object.entries(record.inputs)){
    const ref=record.connections?.[key],port=record.definition.input_ports?.[key];
    if(ref && !(ref.revision_id && port?.quantity==='optics.ordered-layout.v1'))continue;
    const field=resolveInputField(schema.properties?.[key],schema);
    const title=field?.title;
    const declared=title && title.toLowerCase().replaceAll(' ','_')!==key;
    const label=port?.label || (declared && title) || (opticalSystem && modelFields[key]) || title || key;
    const withUnit=typeof v==='number' && port?.unit && !label.endsWith(' / '+port.unit)?label+' / '+port.unit:label;
    visit(v,[key],[withUnit],field);
  }
  return options;
}

function Parameter({options,column,mode,onChange}) {
  const [filter,setFilter]=useState('');
  const selected=options.find(o=>o.key===column.path);
  const term=filter.trim().toLowerCase();
  const matches=options.filter(o=>(o.label+' '+o.key).toLowerCase().includes(term));
  const visible=matches.slice(0,80);
  if(selected && !visible.includes(selected))visible.unshift(selected);
  return <div className="grid-parameter">
    {options.length>20 && <label>查找参数<input value={filter} onChange={e=>setFilter(e.target.value)} placeholder="元件名称或字段名"/></label>}
    <label>变化参数<select aria-label="变化参数" title={selected?.description} required value={column.path} onChange={e=>onChange({path:e.target.value})}>
      <option value="">选择要改变的量</option>{visible.map(o=><option key={o.key} value={o.key}>{o.label}（当前 {format(o.value)}）</option>)}
    </select></label>
    {matches.length>80 && <small>共 {matches.length} 项，输入名称可缩小范围。</small>}
    {mode==='list'?<label>取值<input required value={column.values} placeholder="用空格或逗号分隔" onChange={e=>onChange({values:e.target.value})}/></label>:
      <div className="grid-fields">{[['start','起点'],['stop','终点']].map(([key,label])=><label key={key}>{label}
        <input type="number" step="any" required value={column[key]} onChange={e=>onChange({[key]:e.target.value})}/></label>)}</div>}
  </div>;
}

function buildAxis(axis) {
  const paths=axis.columns.map(c=>JSON.parse(c.path));
  if(axis.mode==='linear')return {paths,samples:{count:Number(axis.count),start:axis.columns.map(c=>Number(c.start)),stop:axis.columns.map(c=>Number(c.stop))}};
  const samples=axis.columns.map(c=>c.values.trim().split(/[，,\s]+/u).filter(Boolean).map(Number));
  if(samples.some(col=>!col.length || col.length!==samples[0].length || col.some(v=>!Number.isFinite(v))))throw Error('同一组参数的取值数须一致，每项都需要有限数值。');
  return {paths,samples:samples[0].map((_,i)=>samples.map(col=>col[i]))};
}

function initialForm(report) {
  const axes=report.recipe.axes.map(axis=>{
    const list=Array.isArray(axis.samples);
    return {mode:list?'list':'linear',count:list?'5':String(axis.samples.count),columns:axis.paths.map((path,i)=>({
      path:JSON.stringify(path),values:list?axis.samples.map(row=>row[i]).join(', '):'',
      start:list?'':String(axis.samples.start[i]),stop:list?'':String(axis.samples.stop[i])}))};
  });
  return {axes,measures:report.recipe.measures.map(m=>({output:m.output,direction:m.direction || '',
    target:m.target==null?'':String(m.target),lower:m.lower==null?'':String(m.lower),upper:m.upper==null?'':String(m.upper),rationale:m.rationale || ''})),registration:report.recipe.registration || ''};
}

const hasObjectives=report=>report.recipe.measures.some(m=>m.direction);

function verdict(row, ranked) {
  if(row.state==='unavailable')return '待核对';
  if(row.state==='outside')return '超出约束';
  if(!ranked)return '符合约束';
  if(row.preferred)return '优选';
  return row.rank?'可行 · 第 '+row.rank+' 层':'可行';
}

function objectiveText(rule,unit) {
  if(rule.direction==='target')return '最接近 '+withUnit(rule.target,unit);
  return {min:'越小越好',max:'越大越好'}[rule.direction] || '';
}

function ruleText(rule, unit) {
  if(!rule)return '未参与评价';
  const parts=[];
  if(rule.direction)parts.push(objectiveText(rule,unit));
  if(rule.lower!=null || rule.upper!=null) {
    parts.push(acceptanceInequality({...rule,label:'读数',unit}));
  }
  return parts.join('，');
}

function RuleChanges({report,source}) {
  const oldRules=new Map(source.recipe.measures.map(m=>[m.output,m]));
  const newRules=new Map(report.recipe.measures.map(m=>[m.output,m]));
  const changes=[];
  for(const key of new Set([...oldRules.keys(),...newRules.keys()])) {
    const old=oldRules.get(key),next=newRules.get(key),metric=report.measures[key] || source.measures[key];
    const before=ruleText(old,metric.unit),after=ruleText(next,metric.unit);
    const oldReason=old?.rationale || '',newReason=next?.rationale || '';
    if(before!==after || oldReason!==newReason) {
      changes.push(<li key={key}><strong>{metric.label}</strong> {before!==after
        ? <>{before} → {after}</> : '判定条件相同'}{oldReason!==newReason &&
          <small>评价依据从「{oldReason || '未注明'}」改为「{newReason || '未注明'}」。</small>}</li>);
    }
  }
  return changes.length>0?<ul className="search-changes" aria-label="评价条件变化">{changes}</ul>:null;
}

function SearchPlot({report,columns,options,onOpen}){
  const measures=Object.entries(report.measures).map(([key,m])=>({key:'m:'+key,label:[m.label,m.unit].filter(Boolean).join(' / '),read:r=>r.values[key]}));
  const inputs=columns.map((p,i)=>({key:'p:'+i,label:options.find(o=>o.key===JSON.stringify(p))?.label || p.join(' › '),read:r=>r.coordinates[i]}));
  const choices=[...inputs,...measures];
  const [xKey,setX]=useState(measures.length>1?measures[0].key:inputs[0].key),[yKey,setY]=useState(measures.length>1?measures[1].key:measures[0].key);
  const x=choices.find(o=>o.key===xKey),y=choices.find(o=>o.key===yKey);
  const ranked=hasObjectives(report);
  const traces=[['preferred','优选','diamond','#70e1d0'],['feasible',ranked?'其他可行':'符合约束','circle-open','#a4bad9'],['outside','超出约束','x','#ffae80']].map(([state,name,symbol,color])=>{
    const rows=report.rows.filter(r=>((ranked && r.preferred)?'preferred':r.state)===state && Number.isFinite(x.read(r)) && Number.isFinite(y.read(r)));
    return {type:'scatter',mode:'markers',name,x:rows.map(x.read),y:rows.map(y.read),customdata:rows.map(r=>r.run_id),marker:{symbol,color,size:9}};
  }).filter(t=>t.x.length);
  const shapes=[],offscreen=[];
  for(const [axis,choice] of [['x',x],['y',y]]) {
    const rule=report.recipe.measures.find(m=>'m:'+m.output===choice.key);
    if(!rule || !traces.length)continue;
    const values=traces.flatMap(trace=>trace[axis]),low=Math.min(...values),high=Math.max(...values);
    const span=high-low;
    const margin=low===high?Math.abs(low)*.05:Number.isFinite(span)?span*.05:0;
    const edges=rule.lower===rule.upper?[[rule.lower,'限值']]:[[rule.lower,'下限'],[rule.upper,'上限']];
    if(rule.direction==='target')edges.push([rule.target,'目标']);
    for(const [bound,label] of edges) {
      if(bound==null)continue;
      const target=label==='目标';
      if(bound<low-margin || bound>high+margin){
        if(target)offscreen.push((axis==='x'?'横轴':'纵轴')+'目标 '+withUnit(bound,report.measures[rule.output].unit)+' 超出图中候选范围');
        continue;
      }
      const positions=axis==='x'
        ? {xref:'x',x0:bound,x1:bound,yref:'paper',y0:0,y1:1}
        : {xref:'paper',x0:0,x1:1,yref:'y',y0:bound,y1:bound};
      const color=target?'#80c4fc':'#e5bb7d';
      shapes.push({...positions,type:'line',layer:'below',line:{color,width:1.5,dash:target?'dot':'dash'},
        label:{text:label,font:{color,size:10},textposition:axis==='x'?'end':'start'}});
    }
  }
  const selector=(label,key,update)=><label>{label}<select aria-label={label} value={key} onChange={e=>update(e.target.value)}>
    {choices.map(o=><option value={o.key} key={o.key}>{o.label}</option>)}</select></label>;
  return <><div className="grid-fields">{selector('横轴',xKey,setX)}{selector('纵轴',yKey,setY)}</div>
    {traces.length>0 && <ScientificPlot data={traces} ariaLabel="候选组合散点图，点击点打开计算记录" filename={'parameter-search-'+report.id}
      onPoint={(_,id)=>id && onOpen(id)} layout={{autosize:true,height:335,shapes,margin:{l:72,r:24,t:32,b:90},xaxis:{title:{text:x.label}},yaxis:{title:{text:y.label}},
      legend:{orientation:'h',y:-.26},paper_bgcolor:'#131d2a',plot_bgcolor:'#131d2a',font:{color:'#d2dfe5'}}}/>}
    {offscreen.length>0 && <small>{offscreen.join('，')}。</small>}</>;
}

function Deviation({row,metric,definition,precise=false}) {
  const deviation=row.target_deviations?.[metric];
  return <small title={deviation==null?undefined:definition.label+' · '+withUnit(deviation,definition.unit)}>绝对偏差 {deviation==null?'—':withUnit(deviationText(deviation,precise),definition.unit)}</small>;
}

function CandidatePair({report,columns,options,ordered,onOpen,initial}) {
  const candidates=ordered.filter(row=>row.run_id && Object.keys(row.values).length);
  const [first,setFirst]=useState(initial?.first ?? 0),[second,setSecond]=useState(initial?.second ?? 1);
  const summary=useRef(null);
  useEffect(()=>{if(initial)summary.current?.focus();},[initial]);
  if(candidates.length<2)return null;
  const left=candidates[first],right=candidates[second];
  const selector=(label,index,update)=><label>{label}<select aria-label={label} value={index} onChange={e=>update(Number(e.target.value))}>
    {candidates.map((row,i)=><option key={i} value={i}>{row.coordinates.map(format).join(' / ')} · 计算 #{row.run_id}</option>)}</select></label>;
  const visit=row=>onOpen(row.run_id,{mode:'pair',first,second});
  return <details className="search-pair" open={Boolean(initial) || undefined}><summary ref={summary}>逐项对照两个候选</summary>
    <small>取值顺序按{columns.map(p=>options.find(o=>o.key===JSON.stringify(p))?.label || p.join(' › ')).join('、')}。</small>
    <div className="grid-fields">{selector('候选 A',first,setFirst)}{selector('候选 B',second,setSecond)}</div>
    {first===second?<p>已选中同一个候选，更换一项可查看差值。</p>:<div className="study-scroll"><table aria-label="两候选目标对照">
      <thead><tr><th>评价量</th><th>A</th><th>B</th><th>B 相对 A</th></tr></thead>
      <tbody>{report.recipe.measures.map(rule=>{
        const key=rule.output,metric=report.measures[key],a=left.values[key],b=right.values[key];
        const measured=Number.isFinite(a) && Number.isFinite(b);
        const sign=measured?Math.sign(b-a):0;
        const close=measured && sign && format(a)===format(b);
        const delta=!measured?'缺少可比较读数':!sign?'相同':Number.isFinite(b-a)?(sign>0?'+':'')+format(b-a):'差值超出数值范围';
        let change='',preciseDeviation=false;
        const target=rule.direction==='target';
        if(target){
          const errorA=left.target_deviations?.[key],errorB=right.target_deviations?.[key];
          const order=compareDeviations(errorA,errorB);
          if(order!==null){
            change=order===0?'与目标等距':order<0?'A 更接近目标':'B 更接近目标';
            preciseDeviation=order!==0 && deviationText(errorA)===deviationText(errorB);
          }
        } else if(measured && sign && rule.direction)change=(rule.direction==='max' ? sign>0 : sign<0)?'B 在此目标上较优':'A 在此目标上较优';
        return <tr key={key}><th scope="row">{metric.label} {metric.unit}<small>{ruleText(rule,metric.unit)}</small></th>
          <td title={a==null?undefined:String(a)}>{(close?String(a):format(a)) ?? '—'}{target && <Deviation row={left} metric={key} definition={metric} precise={preciseDeviation}/>}</td>
          <td title={b==null?undefined:String(b)}>{(close?String(b):format(b)) ?? '—'}{target && <Deviation row={right} metric={key} definition={metric} precise={preciseDeviation}/>}</td><td>{delta}{change && <small>{change}</small>}</td></tr>;
      })}</tbody></table></div>}
    <div className="grid-fields"><button type="button" className="bench-text-btn" onClick={()=>visit(left)}>打开候选 A 的计算 #{left.run_id}</button>
      {first!==second && <button type="button" className="bench-text-btn" onClick={()=>visit(right)}>打开候选 B 的计算 #{right.run_id}</button>}</div>
  </details>;
}

const Results=React.memo(function Results({caseId,report,files,openFiles,onOpen,onAsk,options,source,onSource,busy,sourceError,initialView,installed,onRefine,onSetLimits,onSavePolicy}) {
  const [showAll,setShowAll]=useState(false);
  const columns=report.recipe.axes.flatMap(a=>a.paths);
  const order=r=>r.rank ?? (r.preferred?1:r.state==='feasible'?2:r.state==='outside'?1000:1001);
  const ordered=report.rows.map((row,index)=>({...row,index})).sort((a,b)=>order(a)-order(b));
  const rows=showAll?ordered:ordered.slice(0,12);
  const measures=Object.entries(report.measures);
  const targets=new Set(report.recipe.measures.filter(m=>m.direction==='target').map(m=>m.output));
  const visit=(id,searchView)=>onOpen(id,null,null,{baselineId:report.baseline_id,searchId:report.id,searchView});
  const restricted=report.recipe.measures.filter(m=>m.lower!=null || m.upper!=null);
  const archivedView=useMemo(()=>report.adjustment_evaluation ? adjustmentView(report.adjustment_evaluation) : null,[report]);
  const adjustment=initialView ? (initialView.mode==='adjustment' ? initialView : null) : archivedView;
  const pair=initialView?.mode==='pair' ? initialView : null;
  const tolerance=(adjustment || pair) ? null : initialView;
  const ranked=hasObjectives(report);
  const outside=report.outside ?? report.rows.filter(row=>row.state==='outside').length;
  const partialOutside=(report.incomplete ?? report.unavailable)-report.unavailable;
  return <section aria-label="候选比较结果"><p>已比较 {ordered.length} 个组合，满足约束 {report.feasible} 个{ranked?'，其中 '+report.preferred+' 个保留为优选。':'。本次仅按约束筛选。'}
    {outside>0 && <>超出约束 {outside} 个。{partialOutside>0 && <>其中 {partialOutside} 个还有评价量缺失。</>}</>}
    {report.unavailable>0 && <>另有 {report.unavailable} 个缺少可比较的结果，仍待核对。</>}</p>
    {onAsk && <button type="button" className="bench-text-btn" onClick={()=>onAsk('关于候选比较 #'+report.id+'，',{artifact_ids:[report.id]})}>与 Agent 讨论这次比较</button>}
    {report.refinement && <RefinedBoundary {...{report,options,onSource,busy}} onOpen={visit}/>}
    {ranked && <small>优选表示本次取值中，没有其他可行组合能改善至少一个目标，同时保持其余目标同样好或更好。</small>}
    {report.source_search_id && <div className="search-lineage">
      <p>本次评价沿用比较 #{report.source_search_id} 的候选计算。 <button type="button" disabled={busy} className="bench-text-btn" onClick={()=>onSource(report.source_search_id)}>查看原比较 #{report.source_search_id}</button></p>
      {source && <RuleChanges report={report} source={source}/>}
      {sourceError && <small role="status">暂未读取原评价条件。{sourceError}</small>}
    </div>}
    {report.rows.some(r=>r.state!=='unavailable') && <SearchPlot report={report} columns={columns} options={options} onOpen={visit}/>}
    <p>{report.recipe.measures.filter(m=>m.direction).map(m=>report.measures[m.output].label+' · '+objectiveText(m,report.measures[m.output].unit)).join('，')}
      {restricted.map(m=><small key={m.output}>{acceptanceInequality({...report.measures[m.output],...m})}</small>)}
      {report.recipe.measures.filter(m=>m.rationale).map(m=><small key={'reason-'+m.output}>{report.measures[m.output].label} · {m.rationale}</small>)}</p>
    <CandidatePair report={report} columns={columns} options={options} ordered={ordered} onOpen={visit} initial={pair}/>
    {!!restricted.length && <StudyTolerance key={'margin-'+JSON.stringify(tolerance)} caseId={caseId} report={report} options={options}
      onOpen={visit} initial={tolerance} {...{installed,busy,onRefine,onAsk}}/>}
    <StudyAdjustment key={'policy-'+JSON.stringify(adjustment)} initial={adjustment} hasLimits={restricted.length>0}
      {...{caseId,report,files,openFiles,options,onAsk,onSetLimits,onSavePolicy}} onOpen={visit}/>
    <div className="study-scroll"><table><caption className="candidate-list-caption"><strong>全部方案的候选计算</strong>
      <small>按本次评价结果排序，显示 {rows.length} / {ordered.length} 个组合。{report.adjustment_evaluation?.designs?.length>1 && '切换上方方案只影响工况评价。'}</small>
    </caption><thead><tr>{columns.map(p=><th key={JSON.stringify(p)}>{options.find(o=>o.key===JSON.stringify(p))?.label || p.join(' › ')}</th>)}
      {measures.map(([k,m])=><th key={k}>{m.label} {m.unit}</th>)}<th>结果</th></tr></thead>
      <tbody>{rows.map(r=><tr key={r.index}>{r.coordinates.map((v,j)=><td key={j} title={String(v)}>{format(v)}</td>)}{measures.map(([k,m])=><td key={k} title={r.values[k]!=null?String(r.values[k]):r.measure_issues?.[k]}>
        {format(r.values[k]) ?? '—'}{targets.has(k) && <Deviation row={r} metric={k} definition={m}/>}
        {r.root_measurements?.includes(k) && (r.measurement_source_search_id ?? report.root_search_id) && <button type="button" className="bench-text-btn" disabled={busy}
          aria-label={m.label+'的首轮取值，查看比较 #'+(r.measurement_source_search_id ?? report.root_search_id)}
          onClick={()=>onSource(r.measurement_source_search_id ?? report.root_search_id)}>首轮取值 #{r.measurement_source_search_id ?? report.root_search_id}</button>}
      </td>)}
        <td>{verdict(r,ranked)}
          {r.science_status==='partial' && ' · 部分计算'}{r.outside?.length>0 && <small>{r.outside.map(k=>report.measures[k].label).join('、')}</small>}
          {source?.rows[r.index] && verdict(source.rows[r.index],hasObjectives(source))!==verdict(r,ranked) &&
            <small>原评价为{verdict(source.rows[r.index],hasObjectives(source))}</small>}
          {r.message && <small>{r.message}</small>}{r.run_id && <button type="button" className="bench-text-btn" onClick={()=>visit(r.run_id)}>计算 #{r.run_id}</button>}</td></tr>)}</tbody>
    </table></div>{ordered.length>12 && <button type="button" onClick={()=>setShowAll(v=>!v)}>{showAll?'收起表格':'显示全部 '+ordered.length+' 个组合'}</button>}</section>;
});

function SearchPanel({caseId,record,files,openFiles,onOpen,onAsk,installed,onSaved,initialId,initialView}) {
  const [history,setHistory]=useState(null),[report,setReport]=useState(null),[axes,setAxes]=useState([emptyAxis()]);
  const options=useMemo(()=>inputOptions(record,history),[record,history]);
  const [measures,setMeasures]=useState([emptyMeasure()]),[registration,setReg]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[attempt,retry]=useState(0);
  const [method,setMethod]=useState('grid'),[origin,setOrigin]=useState(null);
  const [returnWindow,setReturnWindow]=useState(null);
  const resultStart=useRef(null),feedback=useRef(null),firstLimit=useRef(null);
  const setLimits=useCallback(()=>{
    setMethod('reassess');
    requestAnimationFrame(()=>firstLimit.current?.focus());
  },[]);
  const showReport=useCallback(r=>{
    const f=initialForm(r);
    setReport(r);setAxes(f.axes);setMeasures(f.measures);setReg(f.registration);setMethod('reassess');
  },[]);
  const sourceId=report?.source_search_id;
  const loadedSourceId=origin?.id;
  useEffect(()=>{
    if(!sourceId || sourceId===loadedSourceId)return;
    let active=true;
    workbenchApi.studySearches(caseId,record.id,{search_id:sourceId}).then(data=>{
      if(active)setOrigin({id:sourceId,data});
    }).catch(e=>{if(active)setOrigin({id:sourceId,error:e.message});});
    return ()=>{active=false;};
  },[sourceId,loadedSourceId,caseId,record.id]);
  useEffect(()=>{
    let active=true;
    Promise.all([workbenchApi.studySearches(caseId,record.id),initialId?workbenchApi.studySearches(caseId,record.id,{search_id:initialId}):null]).then(([v,r])=>{
      if(active){
        if(r && !v.items.some(item=>item.id===r.id))v.items=[r,...v.items];
        setHistory(v);setError('');
        if(r)showReport(r);
      }})
      .catch(e=>{if(active)setError(e.message);});
    return ()=>{active=false;};
  },[caseId,record.id,attempt,initialId,showReport]);
  function updateAxis(i,patch){setAxes(old=>old.map((a,j)=>i===j?{...a,...patch}:a));}
  const open=useCallback(async (id,searchView)=>{
    if(!id){setReport(null);setMethod('grid');return;}
    setBusy(true);setError('');
    try {const r=await workbenchApi.studySearches(caseId,record.id,{search_id:id});
      showReport(r);setReturnWindow(searchView?{id,searchView}:null);
      setHistory(h=>h.items.some(item=>item.id===id)?h:{...h,items:[r,...h.items]});
    } catch(e){setError(e.message);}finally{setBusy(false);}
  },[caseId,record.id,showReport]);
  async function older(){
    setBusy(true);
    try{const h=await workbenchApi.studySearches(caseId,record.id,{before_id:history.next_before_id});
      const loaded=new Set(history.items.map(item=>item.id));
      setHistory({...h,items:[...history.items,...h.items.filter(item=>!loaded.has(item.id))]});setError('');
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  const reassess=Boolean(report) && (method==='reassess' || !installed);
  const linked=axes.some(a=>a.columns.some(c=>c.path && record.connections?.[JSON.parse(c.path)[0]]));
  function savedComparison(data) {
    showReport(data);setReturnWindow(null);
    setHistory(h=>({...h,items:[data,...h.items.filter(r=>r.id!==data.id)]}));onSaved();
    requestAnimationFrame(()=>resultStart.current?.focus());
  }
  async function refine(request) {
    setBusy(true);setError('');
    try {savedComparison(await workbenchApi.refineStudyTolerance(caseId,request));}
    finally {setBusy(false);}
  }
  async function savePolicy(request) {
    setBusy(true);setError('');
    try {savedComparison(await workbenchApi.saveAdjustmentPolicy(caseId,request));}
    finally {setBusy(false);}
  }
  async function submit(e){
    e.preventDefault();setBusy(true);setError('');
    try {
      const criteria=measures.map(m=>({output:m.output,direction:m.direction || null,
        ...(m.direction==='target'?{target:Number(m.target)}:{}),
        lower:m.lower===''?null:Number(m.lower),upper:m.upper===''?null:Number(m.upper),rationale:m.rationale}));
      const data=reassess
        ? await workbenchApi.reassessStudy(caseId,{search_id:report.id,measures:criteria})
        : await workbenchApi.searchStudy(caseId,{run_id:record.id,axes:axes.map(buildAxis),registration:linked?registration:'',measures:criteria});
      if(data.source_search_id===report?.id && report)setOrigin({id:report.id,data:report});
      savedComparison(data);
    }catch(e){setError(e.message);requestAnimationFrame(()=>feedback.current?.focus());}finally{setBusy(false);}
  }
  const source=sourceId===loadedSourceId?origin:null;
  const availableMeasures={...report?.measures,...history?.measures};
  return <>{error && <p role="alert" ref={feedback} tabIndex={-1}>{error} {!history && <button type="button" onClick={()=>retry(n=>n+1)}>重新读取</button>}</p>}
    {!history && !error && <p role="status">正在读取搜索记录…</p>}
    {!!history?.items.length && <label>已存搜索<select aria-label="已存搜索" value={report?.id || ''} disabled={busy} onChange={e=>open(Number(e.target.value))}>
      <option value="">选择一组记录</option>{history.items.map(r=><option key={r.id} value={r.id}>#{r.id} · {r.policy_source_id || r.adjustment_evaluation
        ? '装调工况评价 · 源自 #'+r.source_search_id
        : r.feasible+' 个可行'+(r.ranking_mode==='pareto'?'，'+r.preferred+' 个优选':'')+(r.source_search_id?' · 重评自 #'+r.source_search_id:'')}</option>)}</select></label>}
    {history?.next_before_id && <button type="button" disabled={busy} onClick={older}>更早的搜索</button>}
    {report && <div ref={resultStart} tabIndex={-1}><Results key={report.id} caseId={caseId} report={report} files={files} openFiles={openFiles} options={options} onOpen={onOpen} onAsk={onAsk}
      installed={installed} onRefine={refine} onSavePolicy={savePolicy} onSetLimits={setLimits} initialView={returnWindow?.id===report.id ? returnWindow.searchView : report.id===initialId ? initialView : null}
      source={source?.data} sourceError={source?.error} onSource={open} busy={busy}/></div>}
    {history && (installed || report) && ((reassess || options.length>0) && Object.keys(availableMeasures).length ? <form onSubmit={submit}><fieldset disabled={busy}>
      {report && <label>比较方式<select aria-label="比较方式" value={reassess?'reassess':'grid'} onChange={e=>setMethod(e.target.value)}>
        <option value="reassess">沿用已算候选，调整评价条件</option>
        {installed && <option value="grid">改变取值，重新计算候选</option>}</select></label>}
      {reassess?<p>用比较 #{report.id} 已存的计算结果评价这 {report.rows.length} 个候选。</p>:<>
      {axes.map((a,i)=><fieldset key={i}><legend>参数组 {i+1}</legend>
        <label>取值方式<select aria-label="取值方式" value={a.mode} onChange={e=>updateAxis(i,{mode:e.target.value})}><option value="list">指定取值</option><option value="linear">等距取值</option></select></label>
        {a.mode==='linear' && <label>取点数<input type="number" required min="2" max="128" value={a.count} onChange={e=>updateAxis(i,{count:e.target.value})}/></label>}
        {a.columns.map((c,j)=><div key={j}><Parameter options={options} column={c} mode={a.mode}
          onChange={patch=>updateAxis(i,{columns:a.columns.map((v,k)=>k===j?{...v,...patch}:v)})}/>
          {a.columns.length>1 && <button type="button" onClick={()=>updateAxis(i,{columns:a.columns.filter((_,k)=>k!==j)})}>移除此参数</button>}</div>)}
        <button type="button" onClick={()=>updateAxis(i,{columns:[...a.columns,emptyColumn()]})}>加入同步变化的参数</button>
        {axes.length>1 && <button type="button" onClick={()=>setAxes(axes.filter((_,j)=>j!==i))}>移除此组</button>}</fieldset>)}
      <button type="button" onClick={()=>setAxes([...axes,emptyAxis()])}>加入独立参数组</button>
      <small>组内的值逐项配对，各组取全部组合。一次最多比较 128 个组合。</small></>}
      {measures.map((m,i)=>{
        const update=patch=>setMeasures(measures.map((v,j)=>j===i?{...v,...patch}:v));
        const metric=availableMeasures[m.output];
        const measureContext=metric && !metric.unit?'comparison-measure-'+record.id+'-'+i:undefined;
        return <fieldset key={i}><legend>评价量 {i+1}</legend><label>结果量<select aria-label="结果量" required value={m.output} onChange={e=>update(m.output
          ? {output:e.target.value,target:'',lower:'',upper:'',rationale:''} : {output:e.target.value})}>
          <option value="">选择要比较的数值</option>{Object.entries(availableMeasures).map(([key,v])=><option key={key} value={key} disabled={measures.some((other,j)=>i!==j && other.output===key)}>{v.label} {v.unit} · {key}</option>)}</select></label>
          <label>比较方向<select aria-label="比较方向" value={m.direction} onChange={e=>update({direction:e.target.value})}><option value="min">越小越好</option><option value="max">越大越好</option><option value="target">最接近目标值</option><option value="">只作约束</option></select></label>
          {measureContext && <small id={measureContext}>所用读数为「{metric.label}」。</small>}
          {m.direction==='target' && <label>{['目标值',metric?.unit].filter(Boolean).join(' / ')}
            <input aria-label="目标值" aria-describedby={measureContext} aria-description={metric?.unit} type="number" step="any" required value={m.target} onChange={e=>update({target:e.target.value})}/></label>}
          <div className="grid-fields">{[['lower','允许下限'],['upper','允许上限']].map(([k,label])=><label key={k}>
            {[label,metric?.unit].filter(Boolean).join(' / ')}<input ref={i===0 && k==='lower'?firstLimit:null} aria-label={label} aria-describedby={measureContext} aria-description={metric?.unit} type="number" step="any" value={m[k]} placeholder="不限" onChange={e=>update({[k]:e.target.value})}/></label>)}</div>
          <details><summary>目标或限值的依据</summary><label>实验依据或暂定假设<input maxLength={500} value={m.rationale} onChange={e=>update({rationale:e.target.value})}/></label></details>
          {measures.length>1 && <button type="button" onClick={()=>setMeasures(measures.filter((_,j)=>j!==i))}>移除此评价量</button>}</fieldset>;
      })}
      <button type="button" onClick={()=>setMeasures([...measures,emptyMeasure()])}>加入目标或约束</button>
      {!reassess && linked && <label>候选光路与基线的参考面及工况<input required maxLength={500} value={registration} onChange={e=>setReg(e.target.value)}/></label>}
      <button type="submit" className="btn btn-primary">{busy?'正在比较…':reassess?'保存新评价':'运行并保存比较'}</button>
    </fieldset></form>:<p>这条记录需要已有数值输入和可比较的数值输出，才能创建搜索。</p>)}</>;
}

export default function StudySearch(props){
  const [visited,setVisited]=useState(Boolean(props.initialId));
  const details=useRef(null);
  useEffect(()=>{if(props.initialId){details.current.open=true;details.current.querySelector('summary')?.focus();}},[props.initialId]);
  return <details className="study-search" ref={details} onToggle={e=>{if(e.target===e.currentTarget && e.currentTarget.open)setVisited(true);}}>
    <summary>参数搜索与方案比较</summary>{visited && <SearchPanel {...props}/>}</details>;
}
