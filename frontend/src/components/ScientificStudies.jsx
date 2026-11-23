import React, {useEffect, useId, useLayoutEffect, useMemo, useRef, useState} from 'react';
import { providerLabel } from '../brand';
import {workbenchApi} from '../api/workbench';
import {API_BASE_URL} from '../api/client';
import {localId} from '../api/localId';
import {storedStudyForm, StudyDraftShelf, studyDraftOrigin, studyFormSlot} from './studyDrafts';
import ScientificPlot from './ScientificPlot';
import StudyReplay from './StudyReplay';
import StudyUsage from './StudyUsage';
import StudySearch from './StudySearch';
import StudyStateValue from './StudyStateValue';
import {DeferredDetails, JsonDetails, JsonText} from './DeferredDetails';
import {editValues, InputField, originalField, shape, valueFor} from './ScientificInput';
import {choiceValue, useLayoutStudyChoices} from './useLayoutStudyChoices';
import {fileRef} from './OriginalFileInput';
import CapabilityBrowser, {useStudyDefinition} from './CapabilityBrowser';
import OpticalFieldView from './OpticalFieldView';
import RegisteredMapView from './RegisteredMapView';
import TransverseCenterResult from './TransverseCenterResult';
import {ModeOverlapResult} from './ModeOverlap';
import { PowerResult } from './PathPower';
import {compatiblePorts, exclusiveFields, fitsPort, sourceKey} from './studyConnections';
import ExclusiveStudyInput from './ExclusiveStudyInput';
import SourceConditions from './SourceConditions';
import {curveTraces, propertyValue, sourceRowGroups, sourceRowLabel, tableRows} from './studyTable.js';
import {downloadBlob,downloadJSON} from './downloadFile';
import {compareReceipts, SolverReceipt} from './SolverReceipt';
import {useNativeRaster} from './useNativeRaster';
import './ScientificStudies.css';
const display = value => typeof value==='number' ? String(Number(value.toPrecision(8)))
  : value==null ? '—' : typeof value==='boolean' ? (value?'是':'否') : typeof value==='object' ? JSON.stringify(value) : String(value);

const quantityText=(value,port)=>port?.shape==='state' ? '冻结状态' : port?.shape==='interval' && Array.isArray(value)
  ? value[0]===value[1] ? display(value[0]) : display(value[0])+' 至 '+display(value[1]) : display(value);

function authoredInputs(record) {
  const keys=record.authored_fields || Object.keys(record.inputs);
  return {...Object.fromEntries(keys.filter(key=>Object.hasOwn(record.inputs,key) && !record.connections?.[key])
    .map(key=>[key,record.inputs[key]])),...record.authored_aliases};
}

function fileUses(record,file) {
  const schema=record.definition.inputs;
  const same=ref=>ref?.attachment_id===file.attachment_id && ref.sha256===file.sha256;
  // Earlier receipts stored the frozen inputs and hashes but omitted the paths.
  const paths=file.input_paths ?? Object.entries(schema.properties).flatMap(([key,spec])=>{
    const kind=originalField(schema,spec), value=record.inputs[key];
    if(kind==='single' && same(value))return [[key]];
    return kind==='list' && Array.isArray(value) ? value.flatMap((ref,i)=>same(ref)?[[key,i]]:[]) : [];
  });
  return paths.map(path=>{
    let field=schema;
    return path.map(k=>{
      const resolved=schema.$defs?.[shape(field).$ref?.split('/').at(-1)] || shape(field);
      if(typeof k==='number'){field=resolved.prefixItems?.[k] || resolved.items || {};return '第 '+(k+1)+' 项';}
      field=resolved.properties?.[k] || resolved.additionalProperties || {};
      return field.title || k;
    }).join(' › ');
  }).join('、');
}

function ProjectOutputPicker({items, value, methodChosen, onChange}) {
  const [query,setQuery]=useState('');
  const options=useMemo(()=>{
    const counts=new Map();
    const choices=items.map(([key,{port,location,value:reading}])=>{
      const summary=port.shape==='state' ? '' : quantityText(reading,port)+' '+port.unit;
      const label=[location,port.label,summary].filter(Boolean).join(' · ');
      counts.set(label,(counts.get(label) || 0)+1);
      return {key,label};
    });
    return choices.map(({key,label})=>({key,label:counts.get(label)>1 ? label+' · '+key : label}));
  },[items]);
  const words=query.trim().toLocaleLowerCase().split(/\s+/);
  const matches=options.filter(item=>words.every(word=>item.label.toLocaleLowerCase().includes(word)));
  const current=options.find(item=>item.key===value);
  const visible=current && !matches.includes(current) ? [current,...matches] : matches;
  const controls=<div className="study-input">
    <label>筛选工程数据<input type="search" value={query} placeholder="元件、位置、物理量或单位"
      onChange={e=>setQuery(e.target.value)}/></label>
    <label>选择工程取值<select aria-label="选择工程取值" value={value} onChange={e=>onChange(e.target.value)}>
      <option value="">请选择来源</option>{visible.map(item=><option key={item.key} value={item.key}>{item.label}</option>)}</select></label>
    {query && <small role="status">匹配 {matches.length} 项{current && !matches.includes(current) && '，保留当前选择'}。</small>}
  </div>;
  return methodChosen && current ? <details><summary>方法按此数据筛选 · {current.label}</summary>{controls}</details> : controls;
}

// The first archived observation schema exposed the entire beam array as a single cell.
// Its rows have stable identities but did not store display names.
const archivedSectionTable=['planes',{'uid':'观察面标识','z_mm':'位置 / mm','beams.*.uid':'光束标识',
  'beams.*.wavelength_nm':'波长 / nm','beams.*.radii_mm.tangential':'T 半径 / mm','beams.*.radii_mm.sagittal':'S 半径 / mm',
  'beams.*.notes.tangential':'T 待补条件','beams.*.notes.sagittal':'S 待补条件','message':'定位说明'}];

function pixelWindow(view, grid) {
  if (!view || !grid) return null;
  function indices(range, axis) {
    const first=axis[0], last=axis.at(-1);
    return [Math.ceil(Math.max(Math.min(...range),Math.min(first,last))),Math.floor(Math.min(Math.max(...range),Math.max(first,last)))];
  }
  const [left,right]=indices(view.x,grid.x),[top,bottom]=indices(view.y,grid.y);
  if (![left,right,top,bottom].every(Number.isFinite) || right<left || bottom<top) return null;
  return [left,top,right-left+1,bottom-top+1];
}

export function ScientificStudyOutput({definition, output, fileEvidence, onWindow, pending, compact=false,recordId=definition.id,caseId}) {
  const [limit,setLimit]=useState(25);
  const [viewport,setViewport]=useState(null);
  const [colorScale,setColorScale]=useState('linear');
  const host=useRef(null), [narrowRaster,setNarrowRaster]=useState(false);
  const plot=useRef(null),[exportError,setExportError]=useState('');
  const [exportingGrid,setExportingGrid]=useState(false);
  const canExportGrid=Boolean(caseId && recordId && definition.native_grid && output.native_grid_sha256);
  const adaptiveGrid=canExportGrid && definition.raster?.native_density;
  const adaptiveViewport=adaptiveGrid?viewport:null;
  const native=useNativeRaster(caseId,recordId,adaptiveGrid?output.native_grid_sha256:null,adaptiveViewport,narrowRaster?300:420);
  const detail=native.data?.density?.length?native.data:null;
  useEffect(()=>{
    if(!definition.raster || !host.current)return;
    const observer=new ResizeObserver(([entry])=>{
      if(entry.contentRect.width>0)setNarrowRaster(entry.contentRect.width<480);
    });
    observer.observe(host.current);
    return ()=>observer.disconnect();
  },[definition.raster]);
  const metrics=Object.entries(definition.metrics || {}).map(([path,label])=>[label,propertyValue(output,path),definition.output_ports?.[path]])
    .filter(([,value])=>value!=null);
  const table=definition.name==='analyze_observation_planes' && definition.provider==='LaserClaw' && definition.version==='1'
    ? archivedSectionTable : definition.table;
  const rows=table && propertyValue(output,table[0]);
  const visible=useMemo(()=>{
    if(!Array.isArray(rows))return null;
    const sources=sourceRowGroups(definition,fileEvidence);
    const columns={...table[1]};
    for(const source of sources) columns[source.indexPath] ??= '';
    const fields=Object.keys(columns);
    const declaredInputs=Object.keys(definition.table_input_rows || {}).length;
    const qualifySources=sources.length>1 || sources.some(source=>source.labels.length<declaredInputs);
    return {...tableRows(rows,columns,limit), columnCount:Object.keys(table[1]).length, qualifySources,
      sources:sources.map(source=>({...source,indexColumn:fields.indexOf(source.indexPath)}))};
  },[rows,table,limit,definition,fileEvidence]);
  const densityRange=useMemo(()=>{
    const range=native.data?.density_range;
    if(range?.length===2 && range.every(Number.isFinite))return {min:range[0],max:range[1]};
    const grid=definition.raster && propertyValue(output,definition.raster.z);
    if(!Array.isArray(grid))return null;
    let min=Infinity,max=-Infinity;
    for(const row of grid) {
      if(!Array.isArray(row))continue;
      for(const value of row) {
        if(!Number.isFinite(value))continue;
        min=Math.min(min,value);
        max=Math.max(max,value);
      }
    }
    return min<=max?{min,max}:null;
  },[definition.raster,output,native.data]);
  const canUseDb=definition.raster?.power_db && densityRange?.max>0;
  const chart=useMemo(()=>{
    const raster=definition.raster;
    if (!definition.chart && !raster) return null;
    const [xTitle,yTitle,series]=definition.chart || [raster.x_label,raster.y_label,[]];
    const data=curveTraces(series,output);
    if(raster) {
      const x=detail?.x_edges_mm ?? propertyValue(output,raster.x), y=detail?.y_edges_mm ?? propertyValue(output,raster.y),
        z=detail?.density ?? propertyValue(output,raster.z);
      if(Array.isArray(x) && Array.isArray(y) && Array.isArray(z) && z.length && z[0]?.length){
        const declared=raster.value_label_path && propertyValue(output,raster.value_label_path);
        const label=typeof declared==='string' && declared.length?declared:raster.label;
        const coords=xTitle+' · %{x:.5g}<br>'+yTitle+' · %{y:.5g}<br>'+label+' · ';
        const trace={type:'heatmap',x,y,z,name:label,colorscale:'Cividis',zsmooth:false,
          hovertemplate:coords+'%{z:.6g}<extra></extra>',colorbar:{title:{text:label},thickness:12},hoverongaps:false};
        if(detail && densityRange && densityRange.max>densityRange.min)Object.assign(trace,{zmin:densityRange.min,zmax:densityRange.max});
        if(canUseDb && colorScale==='dB'){
          const peak=densityRange.max;
          trace.z=z.map(row=>row.map(v=>v==null || v<0?null:v===0?-50:Math.max(-50,10*Math.log10(v/peak))));
          Object.assign(trace,{zmin:-50,zmax:0,customdata:z,hovertemplate:coords+'%{customdata:.6g}<br>%{z:.3g} dB<extra></extra>',
            colorbar:{title:{text:detail?'相对原生整图峰值 / dB':'相对本图峰值 / dB'},thickness:12}});
        }
        if(narrowRaster) {
          Object.assign(trace.colorbar,{orientation:'h',x:.5,xanchor:'center',yref:'container',y:.012,yanchor:'bottom',len:.88,
            thickness:9,title:{...trace.colorbar.title,side:'top'}});
          if(densityRange) {
            const low=trace.zmin ?? (raster.power_db && densityRange.min>=0 ? 0 : densityRange.min);
            const high=trace.zmax ?? densityRange.max;
            const ticks=low===high ? [low] : [low,low/2+high/2,high];
            if(low<high)Object.assign(trace,{zmin:low,zmax:high});
            Object.assign(trace.colorbar,{tickvals:ticks,ticktext:ticks.map(v=>Number(v.toPrecision(4)).toString())});
          }
        }
        data.unshift(trace);
      }
    }
    const xFull=detail?.x_range_mm || (raster && propertyValue(output,raster.x));
    const yFull=detail?.y_range_mm || (raster && propertyValue(output,raster.y));
    const xRange=adaptiveGrid && (adaptiveViewport?.x || (xFull?.length && [xFull[0],xFull.at(-1)]));
    const yRange=adaptiveGrid && (adaptiveViewport?.y || (yFull?.length && (raster.y_down?[yFull.at(-1),yFull[0]]:[yFull[0],yFull.at(-1)])));
    return data.length ? {data,layout:{autosize:true,margin:raster && narrowRaster?{l:54,r:14,t:25,b:105,autoexpand:false}:{l:70,r:20,t:25,b:55},xaxis:{title:{text:xTitle},constrain:'domain',...(xRange && {range:xRange,autorange:false})},
      yaxis:{title:{text:yTitle},...(yRange?{range:yRange,autorange:false}:raster?.y_down && {autorange:'reversed'}),...(raster?.equal_scale && {scaleanchor:'x',scaleratio:1,constrain:'domain'})},
      legend:{orientation:'h'},paper_bgcolor:'#131d2a',plot_bgcolor:'#131d2a',font:{color:'#d2dfe5'}}} : null;
  },[definition.chart,definition.raster,output,colorScale,narrowRaster,densityRange,canUseDb,adaptiveGrid,detail,adaptiveViewport]);
  const canSelect=onWindow && definition.raster?.pixel_window_input && chart?.data[0]?.type==='heatmap';
  const heatmap=chart?.data[0]?.type==='heatmap' ? chart.data[0] : null;
  const plotName=recordId?definition.name+'-'+recordId:definition.name;
  async function exportGrid() {
    setExportingGrid(true);setExportError('');
    try {downloadBlob(plotName+'.npz',await workbenchApi.nativeStudyGrid(caseId,recordId));}
    catch(cause){setExportError('原始分辨率网格未导出。'+cause.message);}
    finally{setExportingGrid(false);}
  }
  const window=canSelect ? pixelWindow(viewport,chart.data[0]) : null;
  const warnings=new Set([...(Array.isArray(output.warnings)?output.warnings:[]),...(Array.isArray(output.model_warnings)?output.model_warnings:[])]);
  const sectionMode=definition.view==='optics.section-mode-comparison.v1' && output.plane && output.beams?.length===2;
  const prominent=[!sectionMode && output.summary,!sectionMode && output.message,output.disclaimer,...warnings]
    .filter(v=>typeof v==='string' && v);
  const assumptions=(Array.isArray(output.assumptions) && !sectionMode?output.assumptions:[]).filter(v=>typeof v==='string' && v && !prominent.includes(v));
  const notes=[...prominent,...assumptions];
  const conditions = new Map();
  for (const [path, assessment] of Object.entries(output.output_assessments || {})) {
    if (!definition.output_requirements?.[path] || !assessment.message) continue;
    const labels = conditions.get(assessment.message) || [];
    labels.push(definition.metrics?.[path] || definition.output_ports[path].label);
    conditions.set(assessment.message,labels);
  }
  const centers=definition.view==='measurement.transverse-center-comparison.v1';
  const metricList=!centers && metrics.length>0 && <dl>{metrics.map(([label,value,port])=><div key={label}><dt>{label}</dt><dd>{quantityText(value,port)}</dd></div>)}</dl>;
  const colorControl=definition.raster?.power_db && chart && <label>强度色标<select aria-label="强度色标" value={canUseDb?colorScale:'linear'} onChange={e=>setColorScale(e.target.value)}>
    <option value="linear">线性强度</option><option value="dB" disabled={!canUseDb}>{detail?'相对原生整图峰值':'相对本图峰值'} · 0 至 −50 dB</option></select>
    {!canUseDb && <small>图中没有可用于 dB 归一化的正强度。</small>}</label>;
  const plotFirst=Boolean(compact && definition.raster && chart);
  const maps=definition.view==='measurement.registered-map-comparison.v1' && output.grid;
  const foldAssumptions=compact || maps || Boolean(rows?.length);
  return <section ref={host} className={'study-output'+(definition.raster?' has-raster':'')+(narrowRaster?' narrow-raster':'')} aria-label="计算结果">
    {(foldAssumptions?prominent:notes).filter(note=>!conditions.has(note)).map((note,i)=><p key={i}
      className={warnings.has(note)?'study-result-warning':undefined}>{note}</p>)}
    {maps && <RegisteredMapView output={output}/>}
    {centers && <TransverseCenterResult output={output} showWarnings={false}/>}
    {sectionMode && <ModeOverlapResult result={output}/>}
    {!plotFirst && metricList}
    {!sectionMode && conditions.size>0 && <section className="study-conditions" aria-label="结果成立条件"><h5>结果成立条件</h5>
      {Array.from(conditions,([message,labels])=><p key={message}><strong>{labels.join('、')}</strong> · {message}</p>)}</section>}
    {Array.isArray(output.next_steps) && output.next_steps.filter(step=>typeof step==='string' && step && !notes.includes(step))
      .map((step,i)=><p key={i}>{step}</p>)}
    {!plotFirst && colorControl}
    {(definition.view==='optics.paraxial-profile.v1' || (!definition.view && definition.provider==='LaserClaw'
      && definition.name==='analyze_optical_system')) && output.planes?.tangential && output.planes?.sagittal
      ? <OpticalFieldView analysis={output}/> : chart && <ScientificPlot ref={plot} key={narrowRaster?'horizontal':'vertical'} {...chart} onViewport={canSelect || adaptiveGrid?setViewport:undefined}
        wheelZoom={Boolean(adaptiveGrid)} onResetViewport={adaptiveGrid?()=>setViewport(null):undefined}
        ariaLabel={definition.title+(definition.raster?'分布图':'曲线')} filename={plotName}/>}
    {(heatmap || canExportGrid) && <div className="study-raster-footer">{heatmap && <small role="status">{adaptiveGrid && (native.loading?'正在读取当前视野的原生网格。':
      native.data?.status==='outside_grid'?'当前视野在计算范围外。':detail?
      (detail.native_window[2]===heatmap.z[0].length && detail.native_window[3]===heatmap.z.length?'原生采样。':'每个显示单元为所含网格的面积加权平均密度。'):'已归档概览。')}
      {native.data?.status!=='outside_grid' && <>图中网格为 {heatmap.z[0].length} 列 × {heatmap.z.length} 行。</>}
      {adaptiveGrid && !viewport && ' 框选或滚轮放大可查看细节。'}</small>}
      <div className="action-row">{adaptiveGrid && viewport && <button className="btn btn-secondary" type="button" onClick={()=>setViewport(null)}>返回全图</button>}
      {heatmap && <button className="btn btn-secondary" type="button" disabled={Boolean(native.loading)} onClick={async()=>{
        setExportError('');try{await plot.current?.exportImage('png');}catch(cause){setExportError(cause.message);}
      }}>导出分布图 PNG</button>}{canExportGrid && <button className="btn btn-secondary" type="button" disabled={exportingGrid}
        onClick={exportGrid}>{exportingGrid?'正在复算并校验网格…':'导出原始分辨率网格 (.npz)'}</button>}</div></div>}
    {exportError && <p role="alert">{exportError}</p>}
    {native.error && <p role="alert">视野细节未加载。{native.error} <button type="button" onClick={native.retry}>重试当前视野</button></p>}
    {plotFirst && <>{colorControl}{metricList}</>}
    {canSelect && (window ? <div className="action-row"><span>左列 {window[0]}，上行 {window[1]} · {window[2]} × {window[3]} 像素</span>
      <button type="button" disabled={pending} onClick={()=>onWindow(definition.raster.pixel_window_input,window)}>用图中选区重算</button></div>
      : <small>拖动框选图中区域，可以按像素中心取样并从原件重算。</small>)}
    {Array.isArray(rows) && rows.length>0 && <><div className="study-scroll"><table><thead><tr>
      {Object.entries(table[1]).map(([field,label])=><th key={field} scope="col">{label}</th>)}
      {!!visible.sources.length && <th scope="col">原件行号</th>}</tr></thead><tbody>
      {visible.rows.map((row,i)=><tr key={i}>{row.slice(0,visible.columnCount).map((cell,j)=><td key={j} className={typeof cell==='string'?'study-verbatim':undefined}>{display(cell)}</td>)}
        {!!visible.sources.length && <td>{visible.sources.map((source,j)=>
          <div key={j}>{sourceRowLabel(source,row[source.indexColumn],visible.qualifySources)}</div>)}</td>}</tr>)}
      </tbody></table></div>{visible.more && <button type="button" onClick={()=>setLimit(n=>n+25)}>继续查看数据行</button>}</>}
    {foldAssumptions && assumptions.length>0 && <DeferredDetails className="study-model-assumptions" title={'模型假设与数值设置 · '+assumptions.length+' 项'}>
      <ul>{assumptions.map((note,i)=><li key={i}>{note}</li>)}</ul></DeferredDetails>}
    <JsonDetails title="完整计算输出" value={output} defaultOpen={!metrics.length && !chart && !rows?.length && !notes.length}/>
  </section>;
}

function rowWindow(ref) {
  const {attachment_id,sheet,delimiter,decimal_mark,first_row,last_row,x_column,x_lower,x_upper}=ref.table;
  return JSON.stringify([ref.sha256,attachment_id,sheet,delimiter,decimal_mark,first_row,last_row,
    x_lower==null && x_upper==null ? null : x_column,x_lower,x_upper]);
}
const tableLabel=item=>`${item.source.path} · ${item.table.sheet ? item.table.sheet+' · ' : ''}第 ${item.table.first_row}–${item.table.last_row ?? '末'} 条`+
  (item.table.x_lower!=null || item.table.x_upper!=null ? ` · x [${item.table.x_lower ?? '−∞'}, ${item.table.x_upper ?? '∞'}]` : '');

function FileColumnFields({title,target,textColumn,reference,source,needsRegistration,update}) {
  const listId=useId();
  const calibration=reference.calibration;
  const number=value=>value===''?'':Number(value);
  const units=textColumn?[]:[target.unit,...Object.keys(target.alternate_units || {})];
  const choices=new Map();
  for(const unit of units) {
    const label=unit.normalize('NFKC');
    if(!choices.has(label) || reference.unit===unit) choices.set(label,unit);
  }
  return <div className="study-file-column"><label>{title}的原件列号<input required type="number" min="1" max="16384" step="1"
    list={listId} value={reference.column} onChange={e=>update({column:number(e.target.value)})}/></label>
    <datalist id={listId}>{source?.columns?.map(col=><option key={col} value={col}>{source.labels?.[col]}</option>)}</datalist>
    {source?.columns?.length>0 && <small>{source.columns.map(col=>col+' · '+(source.labels?.[col] || '未标注')).join('　')}</small>}
    {textColumn?<small>逐行保留原件中的文本。</small>:<label>{title}的原件单位<select aria-label={title+'的原件单位'} required value={reference.unit} onChange={e=>update({unit:e.target.value})}><option value="">按原件选择</option>
      {[...choices].map(([label,u])=><option key={label} value={u}>{label}</option>)}</select></label>}
    <label className="study-check"><input type="checkbox" checked={!!reference.omit_incomplete_rows}
      onChange={e=>update({omit_incomplete_rows:e.target.checked})}/>整行排除缺失读数，记录行号</label>
    {needsRegistration && <label>跨来源测点如何配对<input required value={reference.registration || ''} maxLength={500}
      onChange={e=>update({registration:e.target.value})}/></label>}
    {Object.entries(target.conditions || {}).map(([key,rule])=><label key={key}>{title}的实测工况 · {rule.label}
      <input required type={rule.value_type==='text'?'text':'number'} step="any" value={reference.condition_values?.[key] ?? ''}
        onChange={e=>update({condition_values:{...reference.condition_values,[key]:rule.value_type==='text'?e.target.value:number(e.target.value)}})}/></label>)}
    {!!Object.keys(target.conditions || {}).length && <label>工况的原件位置或依据<input required maxLength={500}
      value={reference.condition_note || ''} onChange={e=>update({condition_note:e.target.value})}/></label>}
    {!textColumn && <details><summary>校准{calibration?' · 已启用':''}</summary><label className="study-check"><input type="checkbox" checked={!!calibration}
      onChange={e=>update({calibration:e.target.checked?{gain:1,offset:0,note:''}:null})}/>按 原始读数 × 系数 ＋ 偏移 校准</label>
      {calibration && <><label>系数<input type="number" step="any" required value={calibration.gain} onChange={e=>update({calibration:{...calibration,gain:number(e.target.value)}})}/></label>
        <label>偏移（原件单位）<input type="number" step="any" required value={calibration.offset ?? 0} onChange={e=>update({calibration:{...calibration,offset:number(e.target.value)}})}/></label>
        <label>校准依据<input required maxLength={500} value={calibration.note} onChange={e=>update({calibration:{...calibration,note:e.target.value}})}/></label></>}</details>}
    </div>;
}

function ProjectOrigin({caseId,revision,item,value,target}) {
  const [source,setSource]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(false);
  const budget=item && Object.hasOwn(item,'stage_law') && source?.analysis?.sections?.power_budgets?.find(p=>p.path_uid===item.path_uid);
  const path=item?.path_uid==='primary' ? source?.project.system : source?.project.incident_paths?.find(p=>p.uid===item?.path_uid);
  const element=item?.scope==='network'
    ? source?.project.network?.components.find(c=>c.uid===item.element_uid)
    : path?.elements.find(e=>e.uid===item?.element_uid);
  async function open() {
    setLoading(true);
    try {const data=await workbenchApi.getProject(caseId,revision,true);setSource(data.current);setError('');}
    catch(e){setError(e.message);}finally{setLoading(false);}
  }
  return <div><strong>{target?.label}</strong><span>工程 #{revision} · {item?.location}
    {target?.shape!=='state' && <> · {quantityText(value,target)} {target?.unit}</>}</span>
    {item?.scope==='layout_prefix' && <p>此记录采用工程坐标 {display(item.source_z_mm)} 至 {display(item.z_mm)} mm 之间的光路，
      以光源为局部 z = 0。</p>}
    {target?.shape==='state' && <StudyStateValue title="查看本次采用的状态" port={target} value={value}/>}
    {item?.incomplete?.length>0 && <small>本段及前序段的待补参数：{item.incomplete.join('、')}</small>}
    <DeferredDetails title="查看工程取值依据"><p>{item?.project_title}</p>
      {source ? <><h5>来源版本 #{source.revision_id} · {source.project.title}</h5>
        {budget ? <PowerResult result={budget}/> : <JsonText value={(['layout','layout_prefix'].includes(item?.scope) && source.project) || element || (item?.scope==='network' && source.project.network) || path || source.project.system}/>}</>
        : <button type="button" disabled={loading} onClick={open}>{loading?'正在读取…':'打开来源版本 #'+revision}</button>}
      {error && <p role="alert">{error}</p>}
      <JsonDetails title="冻结的输入依据" value={item}/></DeferredDetails></div>;
}

function ModelSources({caseId,revision,setRevision,onOutputs}) {
  const [versions,setVersions]=useState(null),[error,setError]=useState(''),[attempt,retry]=useState(0),[more,setMore]=useState(false);
  useEffect(()=>{
    let live=true;
    workbenchApi.modelVersions(caseId).then(v=>{if(live)setVersions(v);}).catch(e=>{if(live)setError(e.message);});
    return ()=>{live=false;};
  },[caseId,attempt]);
  useEffect(()=>{
    let live=true;
    if(revision) workbenchApi.projectOutputs(caseId,revision).then(v=>{if(live){onOutputs(v);setError('');}}).catch(e=>{if(live)setError(e.message);});
    return ()=>{live=false;};
  },[caseId,revision,onOutputs,attempt]);
  async function older() {
    setMore(true);
    try {const v=await workbenchApi.modelVersions(caseId,versions.next_before_id);
      setVersions(prev=>({...v,items:[...prev.items,...v.items]}));}catch(e){setError(e.message);}finally{setMore(false);}
  }
  if(!versions?.items.length && !revision && !error)return null;
  return <div className="study-input"><label>引用工程版本<select aria-label="引用工程版本" value={revision || ''}
    onChange={e=>setRevision(Number(e.target.value)||null)}><option value="">选择已存工程或试算</option>
    {revision && !versions?.items.some(v=>v.id===revision) && <option value={revision}>版本 #{revision}</option>}
    {versions?.items.map(v=><option value={v.id} key={v.id}>{v.variant_name || (v.kind==='optical_snapshot'?'试算':'工程')} #{v.id} · {v.title} · {v.change_note}</option>)}</select></label>
    {versions?.next_before_id && <button type="button" disabled={more} onClick={older}>更早的工程版本</button>}
    {error && <p role="alert">{error} <button type="button" onClick={()=>retry(n=>n+1)}>重新读取来源</button></p>}</div>;
}

function StudyForm({shelf,draftSlot,draftSource,caseId,defaultRevision,definition, initial, initialFile, records, initialConnections, offeredState, onSubmit, pending, tableSources, openFiles,files,ready}) {
  const schema=definition.inputs;
  const fileText=definition.text_column_inputs || {};
  const defaults=editValues(definition,Object.fromEntries(Object.entries(schema.properties).filter(([,f])=>f.default!=null).map(([k,f])=>[k,f.default])));
  // Display defaults in the controls while preserving which inputs the experimenter actually supplied.
  const [restored]=useState(()=>storedStudyForm(shelf.read(draftSlot)));
  const [values,setValues]=useState(()=>restored?.values ?? editValues(definition,{...initialFile,...initial}));
  const [raw,setRaw]=useState(()=>restored ? restored.raw : initial && Object.keys(initial).some(key=>!Object.hasOwn(schema.properties,key))
    ? JSON.stringify(initial,null,2) : null);
  const [connections,setConnections]=useState(()=>restored?.connections ?? initialConnections ?? {});
  const alternatives=useMemo(()=>exclusiveFields(schema),[schema]);
  const [choice,setChoice]=useState(()=>restored?.choice ?? (alternatives.find(k=>initialConnections?.[k] || initial?.[k]!=null) || ''));
  const active=key=>!alternatives.includes(key) || key===choice;
  const activeConnections=Object.fromEntries(Object.entries(connections).filter(([key])=>active(key)));
  const [revision,setRevision]=useState(()=>restored ? restored.revision : Object.values(initialConnections || {}).find(r=>r.revision_id)?.revision_id || defaultRevision || null);
  const [model,setModel]=useState(null);
  const [error,setError]=useState('');
  const snapshot=useMemo(()=>({values,raw,connections,choice,revision}),[values,raw,connections,choice,revision]);
  const [openingSnapshot]=useState(snapshot);
  const previous=useRef(snapshot), token=useRef(restored?.token);
  const [saved,setSaved]=useState(restored?null:snapshot);
  useLayoutEffect(()=>{
    if (previous.current===snapshot) return;
    previous.current=snapshot;
    token.current=localId();
    shelf.write(draftSlot,{...snapshot,token:token.current});
    shelf.addEntry(draftSlot,definition.title,draftSource);
  },[shelf,draftSlot,snapshot,definition.title,draftSource]);
  const manualInputs=raw===null?{...defaults,...values}:(()=>{
    try{return JSON.parse(raw) || {};}catch{return {};}
  })();
  const provided=key=>!!connections[key] || (manualInputs[key]!=null && manualInputs[key]!=='' && manualInputs[key]!=='null');
  const layoutObjects=useLayoutStudyChoices(caseId,definition,activeConnections,manualInputs,offeredState,ready && raw===null);
  const parse=(key,value)=>valueFor({...shape(schema.properties[key]),interval:definition.input_ports?.[key]?.shape==='interval'},value);
  function formInputs() {
    const inputs={};
    for(const [key,value] of Object.entries(values)) {
      if (connections[key] || !active(key)) continue;
      const field=schema.properties[key];
      if (value==='') {
        if (field.default!=null && field.anyOf?.some(item=>item.type==='null')) inputs[key]=null;
        continue;
      }
      try {inputs[key]=parse(key,value);}
      catch(cause){throw Error((field.title || key)+' · '+cause.message,{cause});}
    }
    return inputs;
  }
  function readObject() {
    const inputs=JSON.parse(raw);
    if(!inputs || Array.isArray(inputs) || typeof inputs!=='object') throw Error('完整输入需要一个 JSON 对象。');
    return inputs;
  }
  function changeSource(key, reference) {
    try {
      if(raw!==null) {
        const inputs=readObject();
        if(reference && Object.hasOwn(inputs,key)) {
          setValues(prev=>({...prev,...editValues(definition,{[key]:inputs[key]})}));
          delete inputs[key];
        } else if (!reference && values[key]) {
          inputs[key]=parse(key,values[key]);
        }
        setRaw(JSON.stringify(inputs,null,2));
      }
      setConnections(prev=>{
        const next={...prev};
        if(reference) next[key]=reference; else delete next[key];
        return next;
      });
      setError('');
    } catch(cause){setError('请先核对完整输入。'+cause.message);}
  }
  function updateColumn(key,patch) {
    setConnections(old=>{
      const next={...old,[key]:{...old[key],...patch}};
      if(Object.hasOwn(patch,'omit_incomplete_rows')) {
        for(const [field,ref] of Object.entries(old)) {
          if(ref.table && rowWindow(ref)===rowWindow(old[key])) next[field]={...next[field],omit_incomplete_rows:patch.omit_incomplete_rows};
        }
      }
      return next;
    });
  }
  function copyState(key, state) {
    try {
      if(raw!==null) setRaw(JSON.stringify({...readObject(),[key]:state},null,2));
      setValues(prev=>({...prev,[key]:JSON.stringify(state,null,2)}));
      setConnections(prev=>{
        const next={...prev};
        delete next[key];
        return next;
      });
      setError('');
    } catch(cause){setError('请先核对完整输入。'+cause.message);}
  }
  function switchMode() {
    try {
      if(raw===null) setRaw(JSON.stringify(formInputs(),null,2));
      else {
        const inputs=readObject();
        if(Object.entries(inputs).some(([k,v])=>!Object.hasOwn(schema.properties,k) || v==='' ||
            (v===null && !schema.properties[k].anyOf?.some(item=>item.type==='null'))))
          throw Error('此输入含空值或额外字段，请继续在 JSON 中编辑以保留原值。');
        if(Object.keys(activeConnections).some(key=>Object.hasOwn(inputs,key))) throw Error('已引用的字段同时包含手填值，请选择一种来源。');
        const supplied=alternatives.filter(key=>inputs[key]!=null || activeConnections[key]);
        if(supplied.length>1)throw Error('这些输入互为替代，请保留其中一项。');
        if(supplied.length) {
          const key=supplied[0];setChoice(key);
          if(inputs[key]!=null && connections[key])setConnections(prev=>{const next={...prev};delete next[key];return next;});
        }
        setValues(prev=>({...Object.fromEntries(Object.entries(prev).filter(([key])=>connections[key] || !active(key))),...editValues(definition,inputs)}));setRaw(null);
      }
      setError('');
    } catch(cause){setError(cause.message);}
  }
  async function submit(event) {
    event.preventDefault();
    try {
      const inputs=raw===null ? formInputs() : readObject();
      if(raw===null && alternatives.length && !choice)throw Error('请选择本次使用的输入。');
      if(raw===null)for(const [key,field] of Object.entries(schema.properties)) {
        const kind=field['x-laserclaw-input'], control=layoutObjects.choices[kind];
        const value=choiceValue(kind,manualInputs[key]);
        if(control && active(key) && !connections[key] && value && !control.choices.some(item=>item.value===value))
          throw Error('「'+(field.title || key)+'」的原标识在所选光路中缺失，请重新选择。');
      }
      if(Object.keys(activeConnections).some(key=>Object.hasOwn(inputs,key))) throw Error('已引用的字段同时包含手填值，请选择一种来源。');
      setError('');
      const submitted=token.current;
      const result=await onSubmit(inputs,activeConnections);
      if (result?.study_record?.id && result.status==='completed') {
        shelf.remove(draftSlot,submitted);
        if (token.current===submitted) setSaved(snapshot);
      }
    } catch(cause) {setError((cause instanceof SyntaxError?'请检查 JSON 格式。':'')+cause.message);}
  }
  return <form onSubmit={submit}><p>{definition.scope}</p>
    {saved!==snapshot && <p role="status">{restored && snapshot===openingSnapshot?'已恢复此标签页的计算草稿。':'当前输入已保留在此标签页。'}</p>}
    <fieldset disabled={pending}>
    {raw===null && <ExclusiveStudyInput fields={alternatives} schema={schema} choice={choice} onChange={setChoice}/>}
    {!!Object.keys(definition.input_ports || {}).length && <ModelSources caseId={caseId} revision={revision} setRevision={setRevision} onOutputs={setModel}/>}
    {openFiles && (Object.keys(fileText).length>0 || Object.values(definition.input_ports || {}).some(port=>port.shape==='series') ||
      (!files.length && Object.values(schema.properties).some(field=>originalField(schema,field)))) &&
      <button type="button" className="btn btn-secondary" onClick={()=>openFiles()}>从实验资料选取数据</button>}
    {Object.entries({...definition.input_ports,...fileText}).filter(([key])=>active(key)).map(([key,target])=>{
      const textColumn=Object.hasOwn(fileText,key);
      const options=textColumn?[]:compatiblePorts(target,records);
      if(!textColumn && revision && model?.revision_id===revision)for(const [port,item] of Object.entries(model.outputs)) {
        if(fitsPort(target,item.port))options.push({revision_id:revision,port,declaration:item.port,value:item.value,
          label:[`工程 #${revision}`,item.location,item.port.label,item.port.shape==='state' ? '' : quantityText(item.value,item.port)+' '+item.port.unit].filter(Boolean).join(' · ')});
      }
      const selected=connections[key];
      if(textColumn || target.shape==='series')for(const item of tableSources) {
        const candidate={table:item.table,sha256:item.source.sha256};
        const other=Object.values(connections).find(ref=>ref.table && rowWindow(ref)===rowWindow(candidate));
        options.push({...candidate,item,label:tableLabel(item),omit_incomplete_rows:!!other?.omit_incomplete_rows});
      }
      if(!options.length && !selected)return null;
      if(selected && !options.some(v=>sourceKey(v)===sourceKey(selected))) {
        options.push({...selected,label:selected.table?'原件 #'+selected.table.attachment_id:(selected.revision_id?'工程 #'+selected.revision_id:'#'+selected.run_id)+' · '+selected.port});
      }
      const title=schema.properties[key].title || key;
      const shared=definition.registration_inputs?.[key], sharedLabel=schema.properties[shared]?.title || shared;
      const inherits=shared && (selected?.registration_from || !selected?.registration?.trim());
      const registrationField=selected && target.shape==='state' && <label>{target.registration_label || '参考面与轴向对应关系'}
        <input required={!shared} maxLength={500} value={inherits?'':selected.registration || ''}
          placeholder={shared?'留空沿用「'+sharedLabel+'」':undefined} onChange={e=>{
            const next={...selected,registration:e.target.value};
            delete next.registration_from;
            setConnections(prev=>({...prev,[key]:next}));
          }}/></label>;
      const picked=selected && options.find(v=>sourceKey(v)===sourceKey(selected));
      const fromOffer=selected && offeredState?.reference && sourceKey(offeredState.reference)===sourceKey(selected);
      const frozenValue=picked?.value ?? (fromOffer ? offeredState.value : undefined);
      const item=picked?.item;
      return <div key={key} className="study-input"><label>{title}的来源<select aria-label={title+'的来源'} value={selected?sourceKey(selected):''}
        onChange={e=>{const option=options.find(v=>sourceKey(v)===e.target.value);
          if(!option){changeSource(key,null);return;}
          changeSource(key,option.table?{table:option.table,sha256:option.sha256,column:'',...(!textColumn && {unit:''}),
            omit_incomplete_rows:!!option.omit_incomplete_rows}:option.revision_id?{revision_id:option.revision_id,port:option.port}:{run_id:option.run_id,port:option.port});}}>
        <option value="">手动填写</option>{options.map(v=><option value={sourceKey(v)} key={sourceKey(v)}>{v.label}</option>)}</select></label>
        <SourceConditions source={picked?.declaration} target={target}/>
        {selected?.table && <FileColumnFields title={title} target={target} textColumn={textColumn} reference={selected} source={item}
          needsRegistration={definition.paired_inputs?.some(group=>group.includes(key) && group.every(provided) &&
            group.some(k=>(!Object.hasOwn(fileText,k) || connections[k]) && (!connections[k]?.table || rowWindow(connections[k])!==rowWindow(selected))))}
          update={patch=>updateColumn(key,patch)}/>}
        {selected && target.shape==='state' && <><StudyStateValue key={sourceKey(selected)} title="查看引用状态" port={target}
          source={selected} caseId={caseId} value={frozenValue} onCopy={state=>copyState(key,state)}/>
          {shared ? <details open={inherits?undefined:true}><summary>{inherits?'沿用「'+sharedLabel+'」 · 可单独填写':'此来源单独登记了配准说明'}</summary>
            {registrationField}</details> : registrationField}</>}
        {selected && target.shape!=='state' && definition.condition_matches?.some(m=>m.other===key) &&
          <details open={selected.registration ? true : undefined}><summary>跨工况引用</summary><label>将此值用于另一工况的依据<input maxLength={500}
            value={selected.registration || ''} onChange={e=>setConnections(prev=>({...prev,[key]:{...selected,registration:e.target.value}}))}/></label></details>}</div>;
    })}
    <div className="study-fields">
    {layoutObjects.error && <p role="status">暂时无法读取所选光路的对象目录，可继续填写原标识。 <button type="button" onClick={layoutObjects.retry}>重新读取对象目录</button></p>}
    {raw===null ? Object.entries(schema.properties).filter(([key])=>active(key) && !connections[key]).map(([name,field])=>{
      const fallback=field['x-default-from']?.field;
      const inherited=fallback && (connections[fallback] || (values[fallback]?.trim() && values[fallback].trim()!=='null'));
      const inheritedTitle=inherited && (schema.properties[fallback]?.title || fallback);
      return <InputField key={name} name={name} field={field} value={values[name] ?? (inherited ? '' : defaults[name])} schema={schema} files={files}
        choices={layoutObjects.choices[field['x-laserclaw-input']]?.choices} port={definition.input_ports?.[name]} required={name===choice || (fallback && field.default==null ? !inherited : schema.required?.includes(name))} inheritedTitle={inheritedTitle}
        onChange={value=>setValues(prev=>({...prev,[name]:value}))}/>;
    }) :
      <div className="study-input"><label htmlFor="study-json-input">完整输入 JSON</label>
        <textarea id="study-json-input" rows={8} spellCheck={false} required value={raw} onChange={e=>setRaw(e.target.value)}/></div>}
    </div><div className="action-row"><button className="btn btn-primary" type="submit">{pending?'正在计算…':'运行并保存结果'}</button>
      <button type="button" className="btn btn-secondary" onClick={switchMode}>{raw===null?'编辑完整 JSON':'逐项编辑'}</button></div></fieldset>
    {error && <p role="alert">{error}</p>}</form>;
}

function ContinueFromResult({record, onContinue, disabled}) {
  const offers = Object.entries(record.available_outputs || {});
  const [choice, setChoice] = useState('');
  if (!offers.length) return null;
  const key = offers.length === 1 ? offers[0][0] : choice;
  const port = record.available_outputs[key];
  const labels = new Set(), repeatedLabels = new Set();
  for (const [, offer] of offers) {
    if (labels.has(offer.label)) repeatedLabels.add(offer.label);
    labels.add(offer.label);
  }
  const describe = ([path, declaration]) => {
    const value = propertyValue(record.output, path);
    const reading = ['scalar', 'interval'].includes(declaration.shape) ? quantityText(value,declaration) : '';
    const quantity = [reading, declaration.shape === 'state' ? '' : declaration.unit].filter(Boolean).join(' ');
    return [declaration.label, repeatedLabels.has(declaration.label) && path, quantity].filter(Boolean).join(' · ');
  };
  return <div className="study-continuation">
    {offers.length > 1 && <label>用于后续计算的物理量<select aria-label="用于后续计算的物理量" value={key} onChange={e=>setChoice(e.target.value)}>
      <option value="">请选择一个结果量</option>{offers.map(entry=><option key={entry[0]} value={entry[0]}>{describe(entry)}</option>)}</select></label>}
    <button type="button" className="btn btn-secondary" disabled={disabled || !port} onClick={()=>onContinue({
      reference:{run_id:record.id,port:key}, declaration:port, title:record.definition.title, label:describe([key,port]),
      ...(port.shape==='state' && {value:propertyValue(record.output,key)})
    })}>{offers.length === 1 ? '用'+port.label+'继续计算' : '用所选物理量继续计算'}</button>
    {port?.shape==='state' && <StudyStateValue key={key} title="查看所选输出状态" port={port} value={propertyValue(record.output,key)}/>}
  </div>;
}

function StudyLayoutActions({record, canvas, onOpen, onUseCanvas}) {
  const [pending, setPending] = useState(''), [error, setError] = useState('');
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const inputs = record.layout_inputs || [];
  if (!inputs.length) return null;
  const dirty = canvas?.dirty || canvas?.rawPending || canvas?.positionPending;
  async function open(input) {
    setError(''); setPending(input.input_key);
    try { await onOpen(record.id, input.input_key, () => mounted.current); }
    catch (cause) { if (mounted.current) setError(cause.message); }
    finally { if (mounted.current) setPending(''); }
  }
  return <section className="study-project-basis" aria-label="继续编辑计算使用的光路">
    {inputs.map(input => {
      const source = record.project_basis?.projects.find(project => project.id === input.revision_id);
      const partial = input.scope === 'layout_prefix';
      const text = input.kind === 'project'
        ? (partial ? '本次只使用了'+(input.location || '来源工程的一段光路')+'。' : '') +
          (source?.model_kind === 'draft' ? '画布将打开完整的试算方案 #'+input.revision_id+'。'
            : '从完整来源工程 #'+input.revision_id+' 建立独立方案。')
        : (input.history_unknown ? '历史记录缺少输入绑定。' : input.upstream_run_id ? '光路取自上游计算 #'+input.upstream_run_id+'。' : '') +
          '独立方案使用计算时保存的光路参数，仅包含输入中记录的元件信息。';
      return <div key={input.input_key}>
        {inputs.length > 1 && <strong>{input.label} · {input.input_key}</strong>}
        <p>{text}</p>
        <button type="button" className="btn btn-secondary" disabled={dirty || Boolean(pending) || !canvas}
          onClick={()=>open(input)}>{pending === input.input_key ? '正在打开方案…' : '在画布打开独立方案'}</button>
        {onUseCanvas && input.kind === 'frozen' && canvas?.sourceId && canvas.studyOrigin?.run_id === record.id &&
          canvas.studyOrigin.input_key === input.input_key && <button type="button" className="bench-text-btn"
            disabled={dirty || Boolean(pending)} onClick={()=>onUseCanvas(input.input_key)}>
            用画布方案 #{canvas.sourceId} 准备复算</button>}
      </div>;
    })}
    {dirty && <p>请先在画布保存或放弃当前编辑，再打开方案。</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}

function StudyProjectBasis({record, canvas, onReplay}) {
  const basis = record.project_basis;
  if (!basis) return null;
  if (!basis.projects.length) return <p className="study-project-basis" aria-label="工程来源">
    {basis.complete ? '本次计算及其上游没有绑定工程版本。' : basis.message}
  </p>;
  const dirty = canvas?.dirty || canvas?.rawPending || canvas?.positionPending;
  const revision = canvas?.sourceId;
  return <section className="study-project-basis" aria-label="工程来源"><h5>工程来源</h5>
    <ul>{basis.projects.map(source => {
      const upstream = source.used_by.filter(id => id !== record.id);
      const direct = source.used_by.includes(record.id);
      const here = !dirty && source.id === revision;
      return <li key={source.id}>
        <strong>{source.model_kind === 'draft' ? '试算快照' : '工程'} #{source.id}</strong>
        {direct && ' · 本次直接引用'}{upstream.length > 0 && ' · 经上游计算 '+upstream.map(id=>'#'+id).join('、')}
        {here && ' · 与画布版本一致'}
        {basis.complete && revision && !dirty && !here && <button type="button" className="bench-text-btn" aria-label={'预览换用 #'+revision+' 替换来源 #'+source.id}
          onClick={()=>onReplay(record.id,{sourceId:source.id,replacementId:revision})}>预览换用 #{revision}</button>}
      </li>;
    })}</ul>
    {dirty && <p>画布有未保存或待应用的修改。这份记录仍使用上列版本。保存要比较的版本后可预览复算。</p>}
    {!dirty && revision && basis.projects.some(source => source.id !== revision) && <p>画布显示版本 #{revision}。这份结果保留了计算时的输入。</p>}
    {!basis.complete && <p>{basis.message}</p>}
  </section>;
}

export default function ScientificStudies(props) {
  return <StudyWorkspace key={JSON.stringify([props.caseId,props.draftScope])} {...props}/>;
}

function StudyWorkspace({entryRef,caseId,draftScope,defaultRevision,canvas,sourceRequest,clearSource,onContinue,onSaved,onAsk,latestRun,latestStatus,latestStudy,tableSources=[],files=[],openFiles,runId,setRunId,openStudy,focusRequest,openStudyInput}) {
  const [storageWarning,setStorageWarning]=useState(false);
  const shelf=useMemo(()=>new StudyDraftShelf(API_BASE_URL,caseId,draftScope,()=>setStorageWarning(true)),[caseId,draftScope]);
  const [entries,setEntries]=useState(()=>shelf.list());
  useEffect(()=>shelf.subscribe(()=>setEntries(shelf.list())),[shelf]);
  const mounted=useRef(false);
  useLayoutEffect(()=>{mounted.current=true;return ()=>{mounted.current=false;};},[]);
  const requestOrigin=studyDraftOrigin(sourceRequest);
  const navSlot=['selection',requestOrigin];
  const [start]=useState(()=>{
    const saved=shelf.read(sourceRequest?navSlot:['last-selection']);
    if (!saved || typeof saved.selected!=='string' || (sourceRequest?.method && sourceRequest.method!==saved.selected)) return {};
    return saved;
  });
  useEffect(()=>{
    const flush=()=>shelf.flushAll();
    const hidden=()=>{if(document.hidden)flush();};
    window.addEventListener('pagehide',flush); document.addEventListener('visibilitychange',hidden);
    return ()=>{window.removeEventListener('pagehide',flush); document.removeEventListener('visibilitychange',hidden); flush();};
  },[shelf]);
  const [formOpen,setFormOpen]=useState(false);
  const [history,setHistory]=useState(null), [historyError,setHistoryError]=useState('');
  const [attempt,retry]=useState(0), [refresh,setRefresh]=useState(0);
  const [selected,setSelected]=useState(start.selected || sourceRequest?.method || ''), [active,setActive]=useState(null);
  const [error,setError]=useState(''), [busy,setBusy]=useState(false), [more,setMore]=useState(false);
  const [seed,setSeed]=useState(null), [formKey,setFormKey]=useState(0);
  const [origin,setOrigin]=useState(null),[originError,setOriginError]=useState('');
  const [lastExecution,setLastExecution]=useState(null);
  const [sourceKeyChoice,setSourceKeyChoice]=useState(start.sourceKeyChoice || '');
  const [matchingFields,setMatchingFields]=useState(start.matchingFields || []), [chosenField,setChosenField]=useState(start.chosenField || '');
  const [originalInputs,setOriginalInputs]=useState(start.originalInputs || null);
  const [expectedMethod,setExpectedMethod]=useState(start.expectedMethod || null);
  const [restoredOrigin,setRestoredOrigin]=useState(start.formOrigin || null);
  const [originLabel,setOriginLabel]=useState(start.originLabel || '');
  const form=useRef(null), historyGeneration=useRef(0);
  useLayoutEffect(()=>{if(start.resume && !focusRequest)form.current.open=true;},[]);
  useEffect(()=>{if(tableSources.length && form.current)form.current.open=true;},[tableSources]);
  useEffect(()=>{
    if(!sourceRequest)return;
    form.current.open=true;form.current.scrollIntoView({block:'start'});
    if(sourceRequest.file || sourceRequest.reference || !sourceRequest.revision_id)return;
    let live=true;
    workbenchApi.projectOutputs(caseId,sourceRequest.revision_id).then(model=>{
      if(!live)return;
      const location=Object.entries(sourceRequest.match || {});
      const items=Object.entries(model.outputs).filter(([key,item])=>
        (!sourceRequest.port || key===sourceRequest.port) && (!sourceRequest.quantity || item.port.quantity===sourceRequest.quantity) &&
        location.every(([key,value])=>item[key]===value));
      setOrigin({request:sourceRequest,items});setOriginError('');
    }).catch(e=>{if(live)setOriginError(e.message);});
    return ()=>{live=false;};
  },[caseId,sourceRequest,attempt]);
  useEffect(()=>{
    const generation=++historyGeneration.current;let live=true;
    workbenchApi.studyRuns(caseId).then(v=>{if(live && historyGeneration.current===generation){setHistory(v);setHistoryError('');}})
      .catch(e=>{if(live)setHistoryError(e.message);});
    return ()=>{live=false;};
  },[caseId,attempt,refresh,latestRun,latestStatus,latestStudy]);
  useEffect(()=>{
    if(!runId)return;
    let live=true;
    workbenchApi.studyRun(caseId,runId).then(v=>{if(live){setActive(v);setError('');}}).catch(e=>{if(live)setError(e.message);});
    return ()=>{live=false;};
  },[caseId,runId,attempt]);
  const selectedMethod=useStudyDefinition(caseId,selected,attempt);
  const definition=selectedMethod?.data;
  const picked=sourceRequest && origin?.request===sourceRequest ? origin.items : null;
  const projectPort=picked?.length===1 ? picked[0][0] : sourceKeyChoice;
  const reference=sourceRequest?.reference || (projectPort && picked?.some(([key])=>key===projectPort)
    ? {revision_id:sourceRequest.revision_id,port:projectPort} : null);
  const field=matchingFields.length===1 ? matchingFields[0] : chosenField;
  const formOrigin=restoredOrigin || (reference ? ['binding',sourceKey(reference),field] : requestOrigin);
  const draftSlot=definition && studyFormSlot(definition,formOrigin);
  const referenceName=reference?.run_id?'计算 #'+reference.run_id:reference?.revision_id?'工程 #'+reference.revision_id:'';
  const draftSource=originLabel || sourceRequest?.file?.filename || (referenceName && referenceName+' · '+(sourceRequest.label || reference.port));
  const compatibleDraft=!expectedMethod || !definition || (expectedMethod.provider===definition.provider && expectedMethod.version===definition.version);
  const navigation={selected,sourceKeyChoice,matchingFields,chosenField,originalInputs,formOrigin,originLabel:draftSource,expectedMethod};
  const navigationText=JSON.stringify(navigation), navKey=JSON.stringify(navSlot);
  useLayoutEffect(()=>{
    if (!selected) return;
    const data=JSON.parse(navigationText);
    const resume=entries.find(entry=>entry.slot.method.name===selected && JSON.stringify(entry.slot.origin)===JSON.stringify(data.formOrigin));
    const selection={...data,expectedMethod:data.expectedMethod || resume?.slot.method,resume:Boolean(resume)};
    shelf.write(JSON.parse(navKey),selection);
    shelf.write(['last-selection'],selection);
  },[shelf,navKey,navigationText,selected,entries]);
  const chosenFile=sourceRequest?.file;
  const hasSource=chosenFile || sourceRequest?.reference || sourceRequest?.revision_id;
  const roles=chosenFile && definition ? (originalInputs ? Object.keys(originalInputs)
    : Object.keys(definition.inputs.properties).filter(k=>originalField(definition.inputs,definition.inputs.properties[k]))) : [];
  const originalRole=definition && roles.length===1 ? roles[0] : null;
  const originalKind=originalRole && definition.inputs.properties[originalRole] && originalField(definition.inputs,definition.inputs.properties[originalRole]);
  const initialFile=originalKind ? {[originalRole]:originalKind==='list'?[fileRef(chosenFile)]:fileRef(chosenFile)} : null;
  const record=active?.id===runId ? active : null;
  const readableBindings=record && record.project_basis?.invalid_bindings_at!==record.id;
  const historyName=record?.definition.name;
  const archivedMethod=useStudyDefinition(caseId,historyName && historyName!==selected ? historyName : '',attempt);
  const lookup=historyName===selected ? selectedMethod : archivedMethod;
  const installed=record && lookup?.data?.version===record.definition.version &&
    lookup.data.provider===record.definition.provider ? lookup.data : null;
  const successor=record && !installed && lookup?.data?.accepts_inputs_from?.some(([provider, version]) =>
    provider === record.definition.provider && version === record.definition.version) ? lookup.data : null;
  const lookupError=selectedMethod?.error || (lookup?.error?.status!==404 ? lookup?.error : null);
  const records=history?.items || [];
  const offeredRecords=sourceRequest?.reference?.run_id && !records.some(s=>s.id===reference.run_id)
    ? [...records,{id:reference.run_id,title:sourceRequest.title,available_outputs:{[reference.port]:sourceRequest.declaration}}] : records;
  function selectMethod(name,fields={},originals=null) {
    if (!name) {
      shelf.write(navSlot,{selected:''});
      shelf.write(['last-selection'],{selected:''});
    }
    setExpectedMethod(null);setRestoredOrigin(null);setOriginLabel('');setSelected(name);setSeed(null);setMatchingFields(Object.keys(fields));setOriginalInputs(originals);setChosenField('');setFormKey(n=>n+1);
  }
  const heading=useRef(null), lastFocus=useRef(null);
  useEffect(()=>{
    if(focusRequest && focusRequest!==lastFocus.current && record?.id===focusRequest.id) {
      lastFocus.current=focusRequest;
      if(!focusRequest.replay && !focusRequest.searchId)heading.current?.focus();
    }
  },[focusRequest,record?.id]);
  async function run(inputs,connections,method=definition,reuseFrom) {
    setBusy(true);
    try {
      const result=await workbenchApi.runStudy(caseId,method,inputs,connections,reuseFrom);
      onSaved?.(result.study_record.id);
      if (mounted.current) {
        setLastExecution({id:result.study_record.id, mode:result.study_record.execution});
        setRunId(result.study_record.id);
        setError('');setRefresh(n=>n+1);
      }
      return result;
    } finally {if(mounted.current)setBusy(false);}
  }
  function editRecordedInputs(layoutField) {
    const inputs=authoredInputs(record), connections={...record.connections};
    if (layoutField) {
      delete inputs[layoutField];
      connections[layoutField]={revision_id:canvas.sourceId,port:'v1/layout'};
    }
    clearSource();setSelected(record.definition.name);setSeed({inputs,connections});
    setExpectedMethod(null);setOriginLabel('计算 #'+record.id+' 的归档输入');setRestoredOrigin(['record',record.id,layoutField || null,localId()]);
    setFormKey(n=>n+1);form.current.open=true;form.current.scrollIntoView({block:'nearest'});
  }
  async function rerunWindow(field,value) {
    const connections={...record.connections};
    delete connections[field];
    try {await run({...authoredInputs(record),[field]:value},connections,installed);}
    catch(cause){setError(cause.message);}
  }
  async function older() {
    const generation=historyGeneration.current;
    setMore(true);
    try {const next=await workbenchApi.studyRuns(caseId,history.next_before_id);
      if(generation===historyGeneration.current){setHistory(prev=>({...next,items:[...prev.items,...next.items]}));setHistoryError('');}
    } catch(cause){setHistoryError(cause.message);} finally{setMore(false);}
  }
  return <section className="scientific-studies" aria-label="案例计算">
    {storageWarning && <p role="alert">此标签页无法写入浏览器草稿存储。请在离开前导出待用输入。</p>}
    <details ref={form} onToggle={e=>{if(e.target===e.currentTarget)setFormOpen(e.currentTarget.open);}}><summary ref={entryRef} data-study-entry>运行独立计算</summary>
      {!!tableSources.length && !sourceRequest && <p>已选原件：{tableLabel(tableSources[0])}。可将选区中的列用作计算输入。</p>}
      {Boolean(hasSource) && <div><p>{chosenFile ? '案例原件 · '+(chosenFile.relative_path || chosenFile.filename)
        : sourceRequest.reference ? <>计算 #{reference.run_id} · {sourceRequest.title} · {sourceRequest.label}</>
        : ['工程 #'+sourceRequest.revision_id,sourceRequest.label,sourceRequest.location].filter(Boolean).join(' · ')}</p>
        {sourceRequest.revision_id && !picked && !originError && <p role="status">正在读取这个版本的工程数据…</p>}
        {picked && !picked.length && <p>当前工程版本没有这个可用取值。</p>}
        {picked?.length===1 && !sourceRequest.quantity && <p>{picked[0][1].location} · {picked[0][1].port.label}</p>}
        {picked?.length>1 && <ProjectOutputPicker items={picked} value={sourceKeyChoice} methodChosen={!!selected}
          onChange={key=>{setSourceKeyChoice(key);selectMethod('');}}/>}
        {originError && <p role="alert">{originError} <button onClick={()=>retry(n=>n+1)}>重读工程取值</button></p>}
        <button type="button" className="btn btn-secondary" onClick={()=>{selectMethod('');clearSource();}}>查看全部方法</button></div>}
      {(!hasSource || chosenFile || reference) && <CapabilityBrowser key={reference ? sourceKey(reference) : chosenFile?'file':'all'}
        caseId={caseId} ready={formOpen} originals={!!chosenFile} attachmentId={chosenFile?.id} source={reference} selected={selected}
        selectedTitle={definition?.title} disabled={busy} onSelect={selectMethod}/>}
      {selected && !selectedMethod && <p role="status">正在读取计算方法…</p>}
      {reference && definition && !chosenField && matchingFields.length>1 && <label>将所选物理量用于哪个输入<select aria-label="将所选物理量用于哪个输入" value={chosenField} onChange={e=>setChosenField(e.target.value)}>
        <option value="">请选择输入</option>{matchingFields.map(k=><option key={k} value={k}>{definition.inputs.properties[k]?.title || k}</option>)}</select></label>}
      {roles.length>1 && <p>这个方法可使用多份原件。请按采集时的用途选择相应文件。</p>}
      {entries.length>0 && <details><summary>此标签页的计算草稿 · {entries.length}</summary><ul>{entries.map(entry=>{
        const {method,origin}=entry.slot, {name,version}=method;
        return <li key={JSON.stringify(entry.slot)}>{entry.title} · v{version}{entry.source && ' · '+entry.source}{' '}
          <button type="button" disabled={busy} onClick={()=>{
            clearSource();selectMethod(name);setRestoredOrigin(origin);setOriginLabel(entry.source);
            setExpectedMethod(method);
          }}>继续编辑</button>{' '}
          <button type="button" onClick={()=>downloadJSON(name+'-draft.json',{method,origin,case_id:caseId,source:entry.source,
            draft:shelf.read(entry.slot)})}>导出输入</button>{' '}
          <button type="button" disabled={busy} onClick={()=>{
            if (!window.confirm('放弃「'+entry.title+'」在此标签页保留的输入草稿？')) return;
            shelf.remove(entry.slot,shelf.read(entry.slot)?.token);
            if (JSON.stringify(entry.slot)===JSON.stringify(draftSlot)) setFormKey(n=>n+1);
          }}>放弃草稿</button>
        </li>;
      })}</ul></details>}
      {!compatibleDraft && <p role="alert">当前方法和这份草稿的版本或提供方不同。可导出原输入以供核对。{' '}
        <button type="button" onClick={()=>selectMethod(selected,Object.fromEntries(matchingFields.map(name=>[name,true])),originalInputs)}>按当前方法重新填写</button></p>}
      {definition && compatibleDraft && (!reference || field) && <StudyForm key={formKey+':'+JSON.stringify(draftSlot)} shelf={shelf} draftSlot={draftSlot} draftSource={draftSource}
        ready={formOpen} caseId={caseId} defaultRevision={sourceRequest?.revision_id || defaultRevision} definition={definition}
        initialFile={initialFile}
        initial={seed?.inputs} initialConnections={seed?.connections || (reference && field ? {[field]:reference} : {})}
        offeredState={sourceRequest} records={offeredRecords} onSubmit={run} pending={busy} tableSources={tableSources} openFiles={openFiles} files={files}/>}
    </details>
    {lookupError && <p role="alert">{lookupError.message} <button onClick={()=>retry(n=>n+1)}>重新读取计算方法</button></p>}
    {historyError && <p role="alert">{historyError} <button onClick={()=>retry(n=>n+1)}>重新读取计算记录</button></p>}
    {!!history?.items.length && <label>案例计算记录<select aria-label="案例计算记录" value={runId ?? ''} onChange={e=>{setRunId(Number(e.target.value) || null);setError('');}}>
      <option value="">选择已存结果</option>{record && !history.items.some(s=>s.id===record.id) &&
        <option value={record.id}>{record.definition.title} · #{record.id} · v{record.definition.version}</option>}
      {history.items.map(s=><option key={s.id} value={s.id}>{s.title} · #{s.id} · v{s.version}</option>)}</select></label>}
    {history?.next_before_id!=null && <button disabled={more} onClick={older}>读取更早的计算</button>}
    {error && <p role="alert">{error} <button onClick={()=>retry(n=>n+1)}>重新读取</button></p>}
    {record && <article><h4 ref={heading} tabIndex={-1}>{record.definition.title} · #{record.id}</h4>
      {focusRequest?.id===record.id && focusRequest.returnTo && <button type="button" className="bench-text-btn"
        onClick={()=>openStudy(focusRequest.returnTo.baselineId,null,focusRequest.returnTo.searchId,null,focusRequest.returnTo.searchView)}>返回候选比较 #{focusRequest.returnTo.searchId}</button>}
      <small>{providerLabel(record.definition.provider)} · v{record.definition.version} · {({completed:'已计算',partial:'部分结果',needs_input:'需补充条件',failed:'计算失败',unsupported:'超出适用范围'})[record.output.status] || '已返回结果'}</small>
      {lastExecution?.id===record.id && lastExecution.mode && <p role="status">{lastExecution.mode==='reused'
        ? '本次请求沿用已存计算 #'+record.id+'。' : '本次已执行计算并保存为 #'+record.id+'。'}</p>}
      <SolverReceipt saved={record.solver_receipt} current={record.installed_solver_receipt ?? null}/>
      <StudyProjectBasis record={record} canvas={canvas} onReplay={openStudy}/>
      <StudyLayoutActions key={'layout-inputs-'+record.id} record={record} canvas={canvas} onOpen={openStudyInput}
        onUseCanvas={readableBindings && installed && !busy ? editRecordedInputs : null}/>
      <p>{record.definition.scope}</p>
      {!!record.originals?.length && <section className="study-origins" aria-label="计算使用的原件"><h5>计算使用的原件</h5>
        {record.originals.map(file=><div key={file.attachment_id}><strong>{fileUses(record,file)}</strong><button type="button" onClick={()=>openFiles?.(file.attachment_id)}>
          {file.path} · #{file.attachment_id}</button><small>SHA-256 · {file.sha256}</small></div>)}</section>}
      {readableBindings && !!Object.keys(record.connections || {}).length && <section className="study-origins" aria-label="输入来源"><h5>输入来源</h5>
        {Object.entries(record.connections).map(([key,ref])=>{
          const target=record.definition.input_ports?.[key];
          if(ref.revision_id) {
            const item=record.project_evidence?.[key];
            return <ProjectOrigin key={record.id+':'+key} caseId={caseId} revision={ref.revision_id} item={item} target={target} value={record.inputs[key]}/>;
          }
          if(ref.table){
            const ev=record.file_evidence?.find(group=>group.fields.includes(key));
            const title=record.definition.inputs.properties[key]?.title || target?.label || key;
            return <div key={key}><strong>{title}</strong><button type="button" onClick={()=>openFiles?.(ref.table.attachment_id,ref.table)}>
              {ev?.source.path || ('原件 #'+ref.table.attachment_id)}</button>
              <span>第 {ref.column} 列 · {record.definition.text_column_inputs?.[key]?'原件文本':ref.unit===target?.unit?ref.unit:ref.unit+' → '+target?.unit}</span>
              {ref.calibration && <small>校准：读数 × {ref.calibration.gain} ＋ {ref.calibration.offset ?? 0} · {ref.calibration.note}</small>}
              {Object.entries(ref.condition_values || {}).map(([key,value])=><small key={key}>{target?.conditions?.[key]?.label || key} · {value}</small>)}
              {ref.condition_note && <small>{ref.condition_note}</small>}{ref.registration && <small>{ref.registration}</small>}</div>;
          }
          const parent=history?.items.find(item=>item.id===ref.run_id);
          const source=parent?.available_outputs?.[ref.port];
          return <div key={key}><strong>{target?.label || key}</strong>
            {target?.shape==='scalar' && <span>{display(record.inputs[key])} {target.unit}</span>}
            <button type="button" title={ref.port} onClick={()=>setRunId(ref.run_id)}>计算 #{ref.run_id}</button>
            <span>{[parent?.title, source?.label || ref.port].filter(Boolean).join(' · ')}</span>{source && target && source.unit!==target.unit && <span>{source.unit} → {target.unit}</span>}
            {target?.shape==='state' && <StudyStateValue title="查看本次采用的状态" port={target} value={record.inputs[key]}/>}
            {ref.registration && <small>{ref.registration}</small>}</div>;
        })}{record.file_evidence?.map((group,i)=><DeferredDetails key={record.id+':file:'+i}
          title={group.source.path+' · 采用 '+group.row_numbers.length+' 条，排除 '+group.omitted_rows.length+' 条'}>
          {()=> <><small>SHA-256 · {group.source.sha256}</small><pre>采用记录：{group.row_numbers.join(', ')}{'\n'}排除记录：{group.omitted_rows.join(', ') || '无'}{'\n'}横轴范围外：{group.outside} 条</pre></>}
        </DeferredDetails>)}</section>}
      <ScientificStudyOutput key={record.id} caseId={caseId} recordId={record.id} definition={record.definition} output={record.output} fileEvidence={record.file_evidence}
        onWindow={installed && readableBindings ? rerunWindow : undefined} pending={busy}/>
      {record.input_origin && <p>本次沿用 <button type="button" className="bench-text-btn"
        onClick={()=>setRunId(record.input_origin.run_id)}>原记录 #{record.input_origin.run_id}（v{record.input_origin.version}）</button>
        的输入与来源，使用 v{record.definition.version} 计算。</p>}
      {onContinue && <ContinueFromResult key={'continue-'+record.id} record={record} onContinue={onContinue} disabled={busy}/>}
      <JsonDetails key={'inputs-'+record.id} title="本次输入" value={record.inputs}/>
      <button type="button" className="btn btn-secondary" onClick={()=>downloadJSON(record.definition.name+'-'+record.id+'.json',record)}>下载计算记录 JSON</button>
      <StudyUsage key={'uses-'+record.id} caseId={caseId} recordId={record.id} onOpen={openStudy}/>
      {readableBindings && <StudySearch key={'search-'+record.id} caseId={caseId} record={record} files={files} openFiles={openFiles} onOpen={openStudy} onAsk={onAsk} installed={installed} onSaved={()=>setRefresh(n=>n+1)}
        initialId={focusRequest?.id===record.id ? focusRequest.searchId : null}
        initialView={focusRequest?.id===record.id ? focusRequest.searchView : null}/>}
      {readableBindings && <StudyReplay key={'replay-'+record.id} caseId={caseId} record={record} initial={focusRequest?.id===record.id ? focusRequest.replay : null} onOpen={setRunId} onSaved={()=>setRefresh(n=>n+1)}/>}
      {successor && <p>这条记录使用 v{record.definition.version}。当前 v{successor.version} 可以读取它的输入与引用，重算会另存结果。</p>}
      {(lookup?.error?.status===404 || (lookup?.data && !installed && !successor)) &&
        <p>复算这条记录需要 {providerLabel(record.definition.provider)} 中的「{record.definition.title}」v{record.definition.version}。当前可查看当时的输入和结果。</p>}
      <div className="action-row">{readableBindings && installed && compareReceipts(record.solver_receipt,record.installed_solver_receipt)!=='same' &&
        <button type="button" disabled={busy} className="btn btn-secondary" onClick={()=>run({}, {}, installed, record.id).catch(cause=>setError(cause.message))}>用已存输入在当前环境计算</button>}
      {readableBindings && successor && <button type="button" disabled={busy} className="btn btn-secondary"
        onClick={()=>run({}, {}, successor, record.id).catch(cause=>setError(cause.message))}>
        用 v{successor.version} 重算原输入</button>}{readableBindings && installed &&
        <button type="button" disabled={busy} className="btn btn-secondary" onClick={()=>editRecordedInputs()}>用这些输入再算</button>}
      <button type="button" className="btn btn-secondary" onClick={()=>onAsk('关于「'+record.definition.title+'」计算记录 #'+record.id+'，',{artifact_ids:[record.id]})}>引用这次计算问 Agent</button></div>
    </article>}
  </section>;
}
