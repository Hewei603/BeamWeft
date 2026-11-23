import React, { useEffect, useState } from 'react';
import { workbenchApi } from '../api/workbench';
import { FilePreview } from './CaseFiles';
import MeterPlot from './MeterPlot';
import './PowerMeasurements.css';

const units=['W','mW','uW','nW','dBm'];
const sourceRoles={unspecified:'独立性未说明',held_out:'独立留出测量',model_input:'这份数据也用于建模'};
const n=v=>Number.isFinite(v)?String(Number(v.toPrecision(6))):'—';
const optional=v=>v===''?null:Number(v);
const interval=(v,scale=1)=>!v?'—':v[0]===v[1]?n(v[0]*scale):n(v[0]*scale)+' 至 '+n(v[1]*scale);
const key=t=>JSON.stringify(t);
const unitOptions=units.map(u=><option value={u} key={u}>{u==='uW'?'μW':u}</option>);

function Summary({title,data,model,unit,scale=1}){
  if(!data)return null;
  const outside=data.below+data.above;
  const delta=data.compared===1 && data.count===1 && model && outside ? (data.above?data.mean-model[1]:model[0]-data.mean) : null;
  return <div className="meter-stat"><strong>{title}</strong><div>{data.count===1?'实测':'读数均值'} {n(data.mean*scale)} {unit}{data.count>1?' · '+data.count+' 次':''}</div>
    <div>版本模型 {model?interval(model,scale)+' '+unit:'待补参数'}</div>
    {data.compared>0 && <div>{delta!=null?'实测'+(data.above?'高于模型上界 ':'低于模型下界 ')+n(delta*scale)+' '+(unit==='%'?'个百分点':unit):
      outside?outside+' / '+data.compared+' 次落在模型范围外':'全部 '+data.compared+' 次读数都在模型范围内'}。
      {data.bounded===0?'仪器误差界限尚未完整给出。':data.disjoint?'计入所填误差后，仍有 '+data.disjoint+' / '+data.bounded+' 次无交集。':'已给出的 '+data.bounded+' 次误差区间均与模型有交集。'}</div>}</div>;
}

export function MeterResult({record,file}){
  const [limit,setLimit]=useState(20),[open,setOpen]=useState(false);
  if(!record?.points)return null;
  const {recipe,location,source_file,points,absolute,normalized}=record;
  const source=recipe.source, paired=normalized?.compared>0;
  const both=paired && normalized.compared===absolute.compared;
  const constrained=record.transmission_model && (record.transmission_model[0]>0 || record.transmission_model[1]<1);
  const drift=both && constrained && absolute.above+absolute.below>0 && normalized.above+normalized.below===0;
  return <article className="meter-result" aria-label="实测功率对照结果">
    <h4>{record.path_name} · {location.label} · 工程 #{record.revision_id}</h4>
    <p>{recipe.conditions} · 实测 {n(recipe.wavelength_nm)} nm · 模型 {n(record.model_wavelength_nm)} nm · {sourceRoles[recipe.independence]}</p>
    <div className="meter-stats"><Summary title="测点功率" data={absolute} model={location.model?.watts} unit="W"/>
      <Summary title={'测点 / '+record.reference_label} data={normalized} model={record.transmission_model} scale={100} unit="%"/></div>
    {drift && <p>按固定源功率计算时有读数超出范围。采用同时刻的上游读数后，所有可比比例都落入模型范围。参考通道记录的输入功率可以解释这组差异。</p>}
    {record.notes.map(note=><p key={note}>{note}</p>)}
    {recipe.independence==='model_input' && <p>这里显示模型对建模数据的符合程度。检验预测能力需要另一次独立测量。</p>}
    <MeterPlot record={record}/>
    <details onToggle={event=>setOpen(event.currentTarget.open)}><summary>查看原件、逐行读数与偏差</summary>
      <p>{source_file.path} · {source.kind==='table'?(source.sheet||'文本表格')+'，第 '+source.first_row+' 行起，测点第 '+source.signal_column+' 列'+(source.reference_column?'，参考第 '+source.reference_column+' 列':''):source.locator}
        {' · '+(record.origin==='agent'?'Agent 登记':'手工登记')}</p>
      <p>测点原单位 {recipe.signal_unit}{recipe.reference_unit?'，参考原单位 '+recipe.reference_unit:''}。测点误差界限 {n(recipe.signal_bound_w)} W{normalized?'，参考误差界限 '+n(recipe.reference_bound_w)+' W':''}。</p>
      {[[recipe.signal_correction,'测点'],[recipe.reference_correction,'参考']].map(([c,name])=>c && <p key={name}>{name}通道按（仪器瓦数 − {n(c.dark_w)} W）× {n(c.scale)} 换算。依据为 {c.source}。</p>)}
      <div className="meter-scroll"><table><caption>偏差取实测减去版本模型。{recipe.reference_unit && '同步预测采用当行参考功率。'}</caption>
        <thead><tr><th>原件行</th><th>测点原数</th>{recipe.reference_unit && <th>参考原数</th>}<th>测点 / W</th><th>固定源偏差 / W</th>{recipe.reference_unit && <><th>同步预测 / W</th><th>比例偏差 / 百分点</th></>}</tr></thead>
        <tbody>{points.slice(0,limit).map((p,i)=><tr key={i}><td>{p.row??'转录'}</td><td>{n(p.raw_signal)}</td>{recipe.reference_unit && <td>{n(p.raw_reference)}</td>}<td>{n(p.watts)}</td>
          <td>{interval(p.absolute?.difference)}</td>{recipe.reference_unit && <><td>{interval(p.prediction_at_reference_w)}</td><td>{interval(p.normalized?.difference,100)}</td></>}</tr>)}</tbody></table></div>
      {points.length>limit && <button type="button" onClick={()=>setLimit(limit+50)}>再显示 50 行</button>}
      <p className="meter-hash">原件 SHA-256 · {source_file.sha256}</p>
      {open && file && <FilePreview key={file.id} file={file}/>}
    </details>
  </article>;
}

const defaults={path:'',target:'',file:'',mode:'table',signalUnit:'W',refUnit:'W',paired:false,refTarget:'',sheet:'',delimiter:'comma',decimal:'.',
  first:'2',last:'',signalCol:'1',refCol:'2',signal:'',ref:'',locator:'',wavelength:'',conditions:'',aligned:false,
  role:'unspecified',signalBound:'',refBound:'',signalCal:false,signalDark:'0',signalScale:'1',signalBasis:'',
  refCal:false,refDark:'0',refScale:'1',refBasis:''};

function MeterEditor({caseId,revision,files,selected,refreshToken,readOnly}){
  const [initialSelection]=useState(selected);
  const [form,setForm]=useState(defaults),[catalog,setCatalog]=useState(null),[reload,setReload]=useState(0);
  const [recordId,setRecordId]=useState(''),[record,setRecord]=useState(null),[preview,setPreview]=useState(false);
  const [loadError,setLoadError]=useState(''),[recordError,setRecordError]=useState(''),[saveError,setSaveError]=useState(''),[busy,setBusy]=useState(false);
  const update=(k,v)=>setForm(f=>({...f,[k]:v}));
  useEffect(()=>{
    let live=true;
    workbenchApi.readPower(caseId,revision).then(data=>{if(live){setCatalog(data);setLoadError('');}}).catch(e=>{if(live)setLoadError(e.message);});
    return ()=>{live=false;};
  },[caseId,revision,refreshToken,reload]);
  useEffect(()=>{
    let live=true;
    if(recordId)workbenchApi.readPower(caseId,revision,recordId).then(r=>{if(live){setRecord(r);setRecordError('');}}).catch(e=>{if(live)setRecordError(e.message);});
    return ()=>{live=false;};
  },[caseId,revision,recordId,reload]);
  const file=files.find(f=>f.id===Number(form.file));
  const paths=catalog?.paths||[],path=paths.find(p=>p.uid===(form.path || initialSelection?.pathUid))||paths[0];
  const targets=path?.targets||[];
  const defaultScope = initialSelection?.pathUid === 'primary' ? 'host' : 'arm';
  const initialTarget = (!form.path || form.path === initialSelection?.pathUid) &&
    targets.find(t=>t.target.kind==='after' && t.target.scope===defaultScope && t.target.uid===initialSelection?.elementUid);
  const target=targets.find(t=>key(t.target)===form.target)||initialTarget||targets.at(-1);
  const referenceOptions=targets.filter(t=>target && t.order.some((v,i)=>v<target.order[i] && t.order.slice(0,i).every((x,j)=>x===target.order[j])));
  const reference=referenceOptions.find(t=>key(t.target)===form.refTarget);
  const table=form.mode==='table';
  const save=async event=>{
    event.preventDefault();
    if(!file || !path || !target || busy || readOnly)return;
    setBusy(true);setSaveError('');
    const source=table?{kind:'table',attachment_id:file.id,sheet:form.sheet,delimiter:form.delimiter,decimal_mark:form.decimal,first_row:Number(form.first),last_row:optional(form.last),
        signal_column:Number(form.signalCol),reference_column:form.paired?Number(form.refCol):null}:
      {kind:'transcribed',attachment_id:file.id,locator:form.locator,signal:Number(form.signal),reference:form.paired?Number(form.ref):null};
    const correction=prefix=>form[prefix+'Cal']?{dark_w:Number(form[prefix+'Dark']),scale:Number(form[prefix+'Scale']),source:form[prefix+'Basis']}:null;
    try {
      const result=await workbenchApi.comparePower(caseId,{revision_id:revision,path_uid:path.uid,target:target.target,reference_target:form.paired && reference?reference.target:null,source,signal_unit:form.signalUnit,
        reference_unit:form.paired?form.refUnit:null,wavelength_nm:optional(form.wavelength),conditions:form.conditions,comparable_conditions:form.aligned,
        independence:form.role,signal_correction:correction('signal'),reference_correction:form.paired?correction('ref'):null,
        signal_bound_w:optional(form.signalBound),reference_bound_w:form.paired?optional(form.refBound):null});
      setRecord(result);setRecordId(String(result.id));setRecordError('');setReload(x=>x+1);
    }catch(e){setSaveError(e.message);}finally{setBusy(false);}
  };
  const input=(label,field,type='text',attrs={})=><label>{label}<input aria-label={label} type={type} value={form[field]} onChange={e=>update(field,e.target.value)} {...attrs}/></label>;
  const select=(label,field,options)=><label>{label}<select aria-label={label} value={form[field]} onChange={e=>update(field,e.target.value)}>{options}</select></label>;
  const col={min:1,step:1,required:true}, numeric={step:'any',required:true};
  return <>
    {loadError && <p role="alert">{loadError} <button type="button" onClick={()=>setReload(x=>x+1)}>重读可用测点</button></p>}
    {!!catalog?.items.length && <label>已有功率对照<select aria-label="已有功率对照" value={recordId} onChange={e=>{setRecord(null);setRecordId(e.target.value);setRecordError('');}}>
      <option value="">选择记录</option>{catalog.items.map(r=><option key={r.id} value={r.id}>#{r.id} · {r.path} · {r.location} · {r.conditions}</option>)}</select></label>}
    {recordError && <p role="alert">{recordError} <button type="button" onClick={()=>setReload(x=>x+1)}>重读记录</button></p>}
    {record && <MeterResult key={record.id} record={record} file={files.find(f=>f.id===record.source_file.attachment_id)}/>}
    {readOnly?<p>当前有未保存修改。保存工程后可以为新版本登记测量。</p>:!files.length?<p>请先在案例的“实验资料”中归档仪器原件。</p>:
      !catalog?<p>正在读取工程中的测点…</p>:!targets.length && !paths.some(p=>p.targets.length)?<p>可在独立单程光路或腔的入射支路上登记前向功率。</p>:
      <form onSubmit={save}><fieldset disabled={busy}><div className="section-fields">
        <label>测量光束<select aria-label="测量光束" value={path?.uid||''} onChange={e=>setForm(f=>({...f,path:e.target.value,target:'',refTarget:'',aligned:false}))}>
          {paths.map(p=><option key={p.uid} value={p.uid}>{p.name} · {n(p.wavelength_nm)} nm</option>)}</select></label>
        <label>功率测点<select aria-label="功率测点" value={target?key(target.target):''} onChange={e=>setForm(f=>({...f,target:e.target.value,refTarget:'',aligned:false}))}>
          {targets.map(t=><option value={key(t.target)} key={key(t.target)}>{t.label}{t.z_mm!=null?' · z = '+n(t.z_mm)+' mm':''}</option>)}</select></label>
        <label>功率计原件<select aria-label="功率计原件" required value={form.file} onChange={e=>{
          const id=e.target.value,f=files.find(v=>v.id===Number(id));
          const t=/\.(csv|tsv|txt|xlsx|xlsm)$/i.test(f?.filename||'') || (f?.read_mode==='text' && !/\.(pdf|md|json)$/i.test(f.filename));
          setForm(v=>({...v,file:id,mode:t?'table':'transcribed',delimiter:/\.tsv$/i.test(f?.filename||'')?'tab':'comma',sheet:'',locator:'',signal:'',ref:'',
            signalCal:false,refCal:false,signalBasis:'',refBasis:'',aligned:false}));
        }}><option value="">选择案例原件</option>{files.map(f=><option key={f.id} value={f.id}>{f.relative_path||f.filename}</option>)}</select></label>
        {select('读取方式','mode',<><option value="table">按表格行列提取</option><option value="transcribed">从照片、文档或记录转录</option></>)}
      </div>
      {file && <button type="button" onClick={()=>setPreview(!preview)}>{preview?'收起仪器原件':'查看仪器原件'}</button>}
      {preview && file && <FilePreview key={file.id} file={file}/>}
      <div className="section-fields">{table?<>{/\.xls[xm]$/i.test(file?.filename||'')?input('工作表名称','sheet','text',{required:true}):
        select('分隔符','delimiter',<><option value="comma">逗号</option><option value="tab">制表符</option><option value="semicolon">分号</option><option value="whitespace">连续空白</option><option value="single">单列原值</option></>)}
        {select('文本数字的小数点','decimal',<><option value=".">句点（1.25）</option><option value=",">逗号（1,25）</option></>)}
        {input('数据首行','first','number',col)}{input('数据末行（可留空）','last','number',{min:1,step:1})}{input('测点列序号','signalCol','number',col)}</>:
        <>{input('原件内的位置','locator','text',{required:true,placeholder:'页码、图像区域或记录编号'})}{input('测点原始读数','signal','number',numeric)}</>}
        {select('测点功率单位','signalUnit',unitOptions)}</div>
      <label className="section-inline"><input type="checkbox" checked={form.paired} onChange={e=>setForm(f=>({...f,paired:e.target.checked,aligned:false}))}/>原件含同时刻的上游参考功率</label>
      {form.paired && <div className="section-fields"><label>参考测点<select aria-label="参考测点" value={reference?key(reference.target):''} onChange={e=>setForm(f=>({...f,refTarget:e.target.value,aligned:false}))}>
        <option value="">光路源面</option>{referenceOptions.map(t=><option key={key(t.target)} value={key(t.target)}>{t.label} · {n(t.z_mm)} mm</option>)}</select></label>{table?input('参考列序号','refCol','number',col):input('参考通道原数','ref','number',numeric)}
        {select('参考通道单位','refUnit',unitOptions)}</div>}
      <div className="section-fields">{input('实测光波长 / nm','wavelength','number',{min:0,step:'any',placeholder:'未知可留空'})}
        {input('采样批次和工况','conditions','text',{required:true,placeholder:'例如批次、探头位置及采样窗口'})}</div>
      <label className="section-inline"><input type="checkbox" checked={form.aligned} onChange={e=>update('aligned',e.target.checked)}/>
        已核对测点和工况，读数代表对应的整束前向功率。配对通道使用相同采样时段</label>
      <details><summary>计量误差与数据用途</summary>
        {(form.paired?[['signal','测点'],['ref','参考']]:[['signal','测点']]).map(([prefix,label])=><div key={prefix}>
          <label className="section-inline"><input type="checkbox" checked={form[prefix+'Cal']} onChange={e=>update(prefix+'Cal',e.target.checked)}/>{label}通道需要扣除零偏或换算取样比例</label>
          {form[prefix+'Cal'] && <div className="section-fields">{input(label+'零偏 / W',prefix+'Dark','number',numeric)}
            {input(label+'换算倍数',prefix+'Scale','number',{...numeric,min:0})}{input(label+'标定依据',prefix+'Basis','text',{required:true})}</div>}
        </div>)}
        <div className="section-fields">
        {input('测点绝对误差界限 / W','signalBound','number',{min:0,step:'any'})}{form.paired && input('参考绝对误差界限 / W','refBound','number',{min:0,step:'any'})}
        {select('数据与模型的关系','role',Object.entries(sourceRoles).map(([k,label])=><option key={k} value={k}>{label}</option>))}</div>
        <p>先把原读数换成瓦，扣除零偏，再乘以换算倍数。仅填写仪器尚未补偿的部分。误差界限指完成换算后整束功率的绝对界限，可留空。</p></details>
      <button className="btn btn-primary" disabled={!file||!target||busy}>{busy?'正在登记…':'登记并对照功率'}</button>
      {saveError && <p role="alert">{saveError}</p>}
    </fieldset></form>}
  </>;
}

export default function PowerMeasurements({caseId,revision,files,selected,refreshToken,readOnly}){
  const [open,setOpen]=useState(false);
  return <details className="meter-analysis" onToggle={e=>{if(e.target===e.currentTarget && e.currentTarget.open)setOpen(true);}}>
    <summary>与功率计原件对照{revision?' · 工程 #'+revision:''}</summary>
    {open && (revision?<MeterEditor caseId={caseId} revision={revision} files={files} selected={selected} refreshToken={refreshToken} readOnly={readOnly}/>:
      <p>保存工程后可将仪器原件中的读数关联到具体光束和元件位置。</p>)}
  </details>;
}
