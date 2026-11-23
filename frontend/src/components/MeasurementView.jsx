import React, {useEffect, useMemo, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import ScientificPlot from './ScientificPlot';
import './MeasurementView.css';

const separators={auto:'自动识别',tab:'制表符',comma:'逗号',semicolon:'分号',whitespace:'连续空白',single:'单列原值'};
const defaults={sheet:'',delimiter:'auto',decimal_mark:'auto',first_row:'',last_row:'',x_column:'auto',y_column:'',x_lower:'',x_upper:''};
const axisName=(item,col)=>col==null?'原件记录号':(item.labels?.[col] || '第 '+col+' 列');
const escapePlot=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');

export function MeasurementResult({item,onQuote,onRange}) {
  const [picked,setPicked]=useState(null);
  const [zoom,setZoom]=useState(null);
  const {x_lower:lower,x_upper:upper}=item.selection;
  const restricted=lower!=null || upper!=null;
  const bounds='横轴范围 '+(lower ?? '−∞')+' 至 '+(upper ?? '+∞');
  const xName=axisName(item,item.selection.x_column), yName=axisName(item,item.selection.y_column);
  const {data,layout}=useMemo(()=>{
    const points=item.points || [];
    const trace={type:'scatter',mode:item.sampled || item.skipped || item.outside || points.length<2 ? 'markers':'lines+markers',
      x:points.map(p=>p[1]), y:points.map(p=>p[2]), customdata:points.map(p=>p[0]),
      marker:{size:points.length>200?4:7,color:'#76d6cd'}, line:{width:1.5,color:'#76d6cd'},
      hovertemplate:'原件第 %{customdata} 条记录<br>x = %{x}<br>y = %{y}<extra></extra>'};
    return {data:[trace],layout:{height:310,autosize:true,margin:{l:78,r:20,t:14,b:55},showlegend:false,
      paper_bgcolor:'#131d2a',plot_bgcolor:'#131d2a',font:{color:'#ccdae4'},
      xaxis:{title:{text:escapePlot(xName)},gridcolor:'#3c4d5d'},yaxis:{title:{text:escapePlot(yName)},gridcolor:'#3c4d5d'}}};
  },[item,xName,yName]);
  if(!item.valid) return <p role="status">{item.message} {restricted && bounds}
    {restricted && onRange && <button type="button" onClick={()=>onRange(null)}>清除横轴范围</button>}</p>;
  const focused=item.points.find(p=>p[0]===picked);
  const reading=p=>'第 '+p[0]+' 条 · x = '+p[1]+'，y = '+p[2];
  const quote=()=>{
    const s=item.selection;
    const words=['请核对附件 #'+s.attachment_id+'「'+item.source.path+'」的这段读数。',
      (s.sheet?'工作表「'+s.sheet+'」，':'分隔方式为'+separators[s.delimiter]+'，')+'小数点用 '+s.decimal_mark+' 。',
      '读取第 '+s.first_row+' 至 '+item.last_scanned+' 条记录。横轴为'+(s.x_column==null?'原件记录号':'第 '+s.x_column+' 列')+'，纵轴为第 '+s.y_column+' 列。',
      restricted?bounds+'，包含端点。':'',
      focused?'我选中原件'+reading(focused)+'。':'','我想进一步了解：'].filter(Boolean);
    onQuote(words.join('\n'),s.attachment_id);
  };
  return <section className="measurement-result" aria-label="原件数值曲线">
    <p><strong>{yName}</strong> 随 {xName} 的变化</p>
    <small>{item.source.path}{item.selection.sheet?' · '+item.selection.sheet:' · '+separators[item.selection.delimiter]}</small>
    <p>读取原件第 {item.selection.first_row} 至 {item.last_scanned} 条记录。{restricted && bounds+'，'}共 {item.valid} 条有效读数。
      {!!item.outside && <> {item.outside} 条在横轴范围外。</>}
      {!!item.skipped && <> 跳过 {item.skipped} 条空白或非数值记录，例如第 {item.skipped_examples.join('、')} 条。
        {restricted && item.missing_x>0 && <>其中 {item.missing_x} 条缺少有效横坐标，无法确定是否在选区内。</>}</>}</p>
    <ScientificPlot data={data} layout={layout} ariaLabel="可缩放原件读数图，点击数据点查看记录号" filename={'measurement-'+item.source.attachment_id}
      onPoint={(_,row)=>setPicked(row)} onXRange={onRange?setZoom:undefined}/>
    {onRange && <div className="action-row">{zoom && <button type="button" className="btn btn-secondary" onClick={()=>onRange(zoom)}>按图中横轴范围读取原件</button>}
      {restricted && <button type="button" className="btn btn-secondary" onClick={()=>onRange(null)}>清除横轴范围</button>}</div>}
    {item.sampled && <p className="field-note">为便于显示，图中保留 {item.points.length} 个原始点，包括各段的坐标端点和极值。下方极值来自选区内全部有效记录。缩小记录范围可以查看细节。</p>}
    <div className="measurement-extrema">{[['最小读数',item.minimum],['最大读数',item.maximum]].map(([title,p])=>
      <button key={title} type="button" onClick={()=>setPicked(p[0])}><span>{title}</span><strong>{p[2]}</strong><small>原件第 {p[0]} 条 · x = {p[1]}</small></button>)}</div>
    {focused && <p role="status">已选中{reading(focused)}。</p>}
    {onQuote && <button className="btn btn-secondary" type="button" onClick={quote}>引用这段读数问 Agent</button>}
  </section>;
}

function initialForm(s) {
  return {...defaults,...s,first_row:s.first_row ?? '',last_row:s.last_row ?? '',x_lower:s.x_lower ?? '',x_upper:s.x_upper ?? '',
    x_column:s.x_column ?? 'record', y_column:s.y_column ?? ''};
}
function selection(form,fileId) {
  return {attachment_id:fileId,sheet:form.sheet,delimiter:form.delimiter,decimal_mark:form.decimal_mark,
    first_row:form.first_row===''?null:Number(form.first_row),last_row:form.last_row===''?null:Number(form.last_row),
    x_column:form.x_column==='record'?null:form.x_column==='auto'||form.x_column===''?'auto':Number(form.x_column),y_column:form.y_column===''?null:Number(form.y_column),
    x_lower:form.x_lower===''?null:Number(form.x_lower),x_upper:form.x_upper===''?null:Number(form.x_upper)};
}

export default function MeasurementView({caseId,file,onQuote,selections,onUseSelection}) {
  const [query,setQuery]=useState(()=>selections?.get(file.id) || {attachment_id:file.id});
  const [result,setResult]=useState(null), [form,setForm]=useState(defaults);
  const [controlsOpen,setControlsOpen]=useState(null);
  const [settled,setSettled]=useState(null), [error,setError]=useState('');
  useEffect(()=>{
    const controller=new AbortController();
    workbenchApi.inspectMeasurement(caseId,query,controller.signal).then(item=>{
      if(controller.signal.aborted) return;
      if(item.valid || (item.scanned>0 && item.selection.y_column==null && query.first_row!=null)) selections?.set(file.id,item.selection);
      setControlsOpen(current=>current ?? !item.valid);
      setResult(item);setForm(initialForm(item.selection));setError('');
    }).catch(cause=>{if(!controller.signal.aborted)setError(cause.message);})
      .finally(()=>{if(!controller.signal.aborted)setSettled(query);});
    return ()=>controller.abort();
  },[caseId,query,file.id,selections]);
  const pending=settled!==query;
  const set=(key,value)=>setForm(old=>({...old,...(key==='x_column' && old.x_column!==value?{x_lower:'',x_upper:''}:{}),[key]:value}));
  const workbook=file.read_mode==='workbook';
  const choices=Array.from(new Set([...(result?.columns || []), ...Object.keys(result?.labels || {}).map(Number)])).sort((a,b)=>a-b);
  const selectionChanged=result && JSON.stringify(selection(form,file.id))!==JSON.stringify(selection(initialForm(result.selection),file.id));
  const hasRowWindow=result?.last_scanned>=result?.selection.first_row;
  const confirmedTextWindow=query.first_row!=null && (workbook || (query.delimiter && query.delimiter!=='auto'));
  const columns=(label,key)=> <label>{label}<input list={'cols-'+file.id} inputMode="numeric" type="number" min="1" max="16384" step="1"
    placeholder="列号" value={form[key]==='record'||form[key]==='auto'?'':form[key]} onChange={e=>set(key,e.target.value)}/></label>;
  const apply=e=>{
    e.preventDefault();setError('');
    setQuery(selection(form,file.id));
  };
  return <section className="measurement-view" aria-label="浏览表格读数">
    {pending && <p role="status">正在从原件读取所选记录…</p>}
    {error && <p role="alert">{error} <button type="button" onClick={()=>setQuery({...query})}>重新读取</button></p>}
    <details open={controlsOpen===true} onToggle={event=>{if(event.currentTarget===event.target)setControlsOpen(event.currentTarget.open);}}><summary>选择数据列与范围</summary>
      <form onSubmit={apply}><fieldset disabled={pending}><div className="measurement-fields">
        {workbook && <label>工作表<select value={form.sheet} onChange={e=>{const sheet=e.target.value;setForm({...defaults,sheet});setQuery({attachment_id:file.id,sheet});}}>
          {(result?.sheets || [form.sheet]).map(s=><option key={s} value={s}>{s}</option>)}</select></label>}
        {!workbook && <label>分隔方式<select value={form.delimiter} onChange={e=>{const delimiter=e.target.value;setForm(old=>({...old,delimiter,...(delimiter==='single'?{x_column:'record',y_column:1}:{})}));}}>{Object.entries(separators).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>}
        <label>小数标记<select value={form.decimal_mark} onChange={e=>set('decimal_mark',e.target.value)}><option value="auto">自动识别</option><option value=".">点（1.25）</option><option value=",">逗号（1,25）</option></select></label>
        <label>横轴取值<select value={form.x_column==='record'?'record':'column'} onChange={e=>set('x_column',e.target.value==='record'?'record':(choices[0] || 1))}><option value="column">指定列</option><option value="record">原件记录号</option></select></label>
        {form.x_column!=='record' && columns('横轴列号','x_column')}{columns('纵轴列号','y_column')}
        <datalist id={'cols-'+file.id}>{choices.map(c=><option key={c} value={c}>{result?.labels[c] || '第 '+c+' 列'}</option>)}</datalist>
        <label>数据首条记录<input type="number" min="1" step="1" placeholder="自动" value={form.first_row} onChange={e=>set('first_row',e.target.value)}/></label>
        <label>数据末条记录<input type="number" min={form.first_row || 1} step="1" placeholder="至文件结尾" value={form.last_row} onChange={e=>set('last_row',e.target.value)}/></label>
        <label>横轴下限<input type="number" step="any" placeholder="不限" value={form.x_lower} onChange={e=>set('x_lower',e.target.value)}/></label>
        <label>横轴上限<input type="number" step="any" placeholder="不限" min={form.x_lower===''?undefined:form.x_lower} value={form.x_upper} onChange={e=>set('x_upper',e.target.value)}/></label>
      </div><button type="submit" className="btn btn-secondary">按所选范围读取</button>
      </fieldset></form>
      <p className="field-note">文本按分隔记录计数，工作簿按表格行号计数。列标题取自原件，缺少标注时使用列号。</p>
      {result?.source && <small className="measurement-checksum">原件 SHA-256 · {result.source.sha256}</small>}</details>
    {result && !pending && !error && <MeasurementResult key={JSON.stringify(result.selection)} item={result} onQuote={onQuote}
      onRange={range=>setQuery({...result.selection,x_lower:range?.[0]??null,x_upper:range?.[1]??null})}/>}
    {result && !pending && !error && !result.valid && result.selection.y_column==null &&
      <p className="field-note">文本列也可作为计算输入。请核对数据首行{workbook?'及工作表':'和分隔方式'}后读取选区。</p>}
    {hasRowWindow && (result.selection.y_column!=null || confirmedTextWindow) && !pending && !error && onUseSelection && <button type="button" disabled={selectionChanged}
      title={selectionChanged?'先按所选范围读取，以采用修改后的选区':undefined} className="btn btn-secondary" onClick={()=>
      onUseSelection({source:result.source,table:{...result.selection,last_row:result.last_scanned??result.selection.last_row},labels:result.labels,columns:choices})}>用这个选区计算</button>}
  </section>;
}
