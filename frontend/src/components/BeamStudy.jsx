import React, { useEffect, useState } from 'react';
import {useParams} from 'react-router-dom';
import { workbenchApi } from '../api/workbench';
import { FilePreview } from './CaseFiles';
import { FitCheckResult, HoldoutResult, PredictAt, PredictedWidths } from './BeamFitCheck';
import './BeamStudy.css';

const valueText = (v, digits=5) => Number.isFinite(v) ? String(Number(v.toPrecision(digits))) : '—';
const rmsText = v => v < 1e-6 ? '小于 0.001 μm' : '为 '+valueText(v*1000)+' μm';
const units = {mm:'mm', cm:'cm', um:'μm', m:'m'};
const widths = {diameter_1e2:'1/e² 全直径', radius_1e2:'1/e² 半径', d4sigma_diameter:'D4σ 二阶矩全直径'};

function FitPlot({points, fit, model, holdout}) {
  const training=points.map(p=>p.z_mm), checked=holdout?.points || [];
  const trainLeft=Math.min(...training), trainRight=Math.max(...training);
  const z = [...training,...checked.map(p=>p.z_mm)];
  const left = Math.min(...z), right = Math.max(...z);
  const lowX=62, highX=651, lowY=144, highY=19;
  const maximum=Math.max(...points.map(p=>Math.max(p.radius_mm,p.fit_radius_mm,p.model_radius_mm||0)),
    ...checked.map(p=>Math.max(p.radius_mm,p.predicted_radius_mm))) * 1.06;
  const x = v=>lowX+(v-left)/(right-left)*(highX-lowX);
  const y = v=>lowY-v/maximum*(lowY-highY);
  const curve=(parameters,start=trainLeft,end=trainRight)=>Array.from({length:121}, (_,i)=>{
    const at=start+(end-start)*i/120;
    const radius=Math.hypot(parameters.waist_radius_mm,parameters.divergence_half_angle_rad*(at-parameters.waist_z_mm));
    return [x(at),y(radius)].join(',');
  }).join(' ');
  return <figure className="fit-figure"><svg viewBox="0 0 684 184" role="img" aria-label="选定测点的半径与拟合曲线">
    {[0,.5,1].map(f=><g key={f}><line x1={lowX} x2={highX} y1={y(f*maximum)} y2={y(f*maximum)} stroke="#38505b"/>
      <text x={lowX-5} y={y(f*maximum)+4} textAnchor="end">{valueText(f*maximum*1000,3)}</text></g>)}
    {[0,.5,1].map(f=><text key={f} x={x(left+f*(right-left))} y="164" textAnchor="middle">{valueText(left+f*(right-left),4)}</text>)}
    {model && <polyline points={curve(model)} stroke="#c2a8ee" strokeWidth="2" strokeDasharray="6 4" fill="none"/>}
    <polyline points={curve(fit)} stroke="#67cfc2" strokeWidth="2.5" fill="none"/>
    {[[left,trainLeft],[trainRight,right]].filter(([a,b])=>a<b).map(([a,b])=><polyline key={a} points={curve(fit,a,b)}
      stroke="#67cfc2" strokeWidth="2" strokeDasharray="3 4" fill="none"/>)}
    {points.map((p,i)=><circle key={i} cx={x(p.z_mm)} cy={y(p.radius_mm)} r="3.2" fill="#f6b079"><title>z {valueText(p.z_mm)} mm · {valueText(p.radius_mm*1000)} μm</title></circle>)}
    {checked.map((p,i)=><path key={i} transform={`translate(${x(p.z_mm)} ${y(p.radius_mm)})`} d="M0 -4.5L4.5 0 0 4.5 -4.5 0Z"
      fill="#0f1c26" stroke="#c3d6ff" strokeWidth="1.8"><title>留出 · z {valueText(p.z_mm)} mm · 实测 {valueText(p.radius_mm*1000)} μm</title></path>)}
    <text x={lowX} y="13">半径 / μm</text><text x={highX} y="181" textAnchor="end">位置 / mm</text>
  </svg><figcaption>橙色圆点参与拟合，实线显示拟合曲线。{checked.length>0 && '空心菱形是留出读数。'}{(left<trainLeft || right>trainRight) && '青色点线是采样区间外的外推。'}{model && '紫色虚线是这个版本的工程预测。'}</figcaption></figure>;
}

const kindName = type => ({beam_caustic_check:'对照',beam_caustic_prediction:'预测',beam_width_summary:'重复测量'})[type] || '拟合';

function RepeatReadings({stats}) {
  const [expanded,setExpanded]=useState(false);
  if (!stats) return null;
  if (!stats.repeated_positions) return <p>每个位置只有一次读数，同位置的波动还需重复测量。</p>;
  return <section aria-label="同位置重复读数"><p>{stats.repeated_positions} 个位置有重复读数。
    合并的组内标准差为 <strong>{valueText(stats.pooled_within_sd_mm*1000)} μm</strong>（半径）。
    {stats.station_mean_fit_rms_mm!=null && <>各位置平均半径与拟合曲线的均方根差{rmsText(stats.station_mean_fit_rms_mm)}。</>}</p>
    <details onToggle={e=>{if(e.target===e.currentTarget)setExpanded(e.currentTarget.open);}}><summary>查看各位置的重复读数</summary>
      {expanded && <div className="fit-data-table"><table><caption>同一轴向位置所报半径的样本标准差</caption>
        <thead><tr><th>位置 / mm</th><th>读数个数</th><th>平均半径 / μm</th><th>标准差 / μm</th></tr></thead>
        <tbody>{stats.stations.map(row=><tr key={row.z_mm}><td>{valueText(row.z_mm)}</td><td>{row.count}</td>
          <td>{valueText(row.mean_radius_mm*1000)}</td><td>{valueText(row.sample_sd_mm*1000)}</td></tr>)}</tbody></table>
        <p>合并统计共 {stats.degrees_of_freedom} 个组内自由度。{stats.station_mean_fit_rms_mm!=null && <>各位置等权时，平均半径的 R² 为 {valueText(stats.r_squared_station_mean_radius)}，
          单条半径读数的 R² 为 {valueText(stats.r_squared_individual_radius)}。</>}</p>
        {stats.station_mean_fit_rms_mm!=null && <p>组内离散超过所选测点半径总变差的 2% 时，工作台提示单次读数可能偏离。实验所需的绝对精度还取决于用途和标定。</p>}</div>}</details>
  </section>;
}

function OriginalWidths({item}) {
  const [all,setAll]=useState(false);
  const {metadata,source,points,fit}=item;
  return <>{metadata.files.map(f=><p key={f.attachment_id}>{f.path} · SHA-256 {f.sha256}</p>)}
    {source.kind==='table' && <p>{source.sheet || '文本表格'} · 位置第 {source.position_column} 列（{units[source.z_unit]}） · 宽度第 {source.width_column} 列（{units[source.width_unit]}，{widths[source.width_definition]}）</p>}
    {item.duplicate_readings?.length>0 && <p>已合并对同一原读数的重复引用：
      {item.duplicate_readings.map(row=>'#'+row.reading_id+' 与 #'+row.counted_reading_id).join('、')}。</p>}
    <div className="fit-data-table"><table><caption>{item.holdout?'参与拟合的读数及换算结果':'各测点的原始宽度与换算结果'}</caption><thead><tr><th>来源</th><th>仪器宽度</th><th>位置 / mm</th><th>半径 / μm</th>{fit && <th>实测减拟合 / μm</th>}</tr></thead>
      <tbody>{(all?points:points.slice(0,25)).map((p,i)=><tr key={i}><td>{p.row?'第 '+p.row+' 行':'#'+p.reading_id+' · '+p.locator}</td>
        <td>{valueText(p.raw_width,8)} {units[p.width_unit||source.width_unit]} · {widths[p.definition||source.width_definition]}</td>
        <td>{valueText(p.z_mm)}</td><td>{valueText(p.radius_mm*1000)}</td>{fit && <td>{valueText(p.residual_mm*1000)}</td>}</tr>)}</tbody></table></div>
    {points.length>25 && <button type="button" onClick={()=>setAll(!all)}>{all?'收起其余读数':'查看全部 '+points.length+' 条'}</button>}</>;
}

export function FitOriginRecord({origin, prefix}) {
  const {id:caseId} = useParams();
  const [open,setOpen] = useState(false);
  const [attempt,setAttempt] = useState(0);
  const [result,setResult] = useState(null);
  useEffect(() => {
    if (!open) return;
    let active = true;
    workbenchApi.readBeamStudies(caseId, origin.study_id).then(item => {
      if (!item.fit || (origin.study_key && item.study_key !== origin.study_key) ||
          item.metadata.axis !== origin.measured_axis || item.metadata.width_basis !== origin.width_basis ||
          item.metadata.wavelength_nm !== origin.wavelength_nm || (origin.medium_n != null && item.metadata.medium_n !== origin.medium_n)) throw Error('工程中的拟合引用与当前案例记录不符，请核对原件。');
      if (active) setResult({item});
    }).catch(cause => { if (active) setResult({error:cause.message}); });
    return () => { active=false; };
  }, [caseId, origin, open, attempt]);
  return <details className="fit-source-record" onToggle={event => { if(event.target===event.currentTarget)setOpen(event.currentTarget.open); }}>
    <summary>{prefix}源自实测拟合 #{origin.study_id}</summary>
    <p>原测量 {origin.measured_axis} 向，源面 z = {origin.coordinate_mm} mm，波长 {origin.wavelength_nm} nm。{origin.registration}</p>
    {origin.medium_n != null && <p>测量介质 n = {origin.medium_n}。以垂轴平面折射到空气，接入处的束宽连续，空气侧曲率为介质侧曲率除以 n。</p>}
    <p>接入时半径 {valueText(origin.radius_mm,6)} mm，等效 M² = {valueText(origin.m2,6)}。宽度拟合描述光斑传播，复振幅与指向还需单独测量。</p>
    {origin.warnings.map(w=><p key={w}>{w}</p>)}
    {open && (result?.item ? <><HoldoutResult item={result.item}/><OriginalWidths item={result.item}/></> : result?.error ?
      <p role="alert">{result.error} <button type="button" onClick={()=>{setResult(null);setAttempt(n=>n+1);}}>重读原始拟合</button></p> :
      <p role="status">正在读取这份拟合的原始测点…</p>)}
  </details>;
}

export function StudyResult({item,caseId,onForecast,onUseFit}) {
  if(item.check) return <FitCheckResult item={item}/>;
  if(item.predictions) return <PredictedWidths result={item}/>;
  if(!item.fit) return <><p role="status">{item.message}</p>{item.repeatability && <article aria-label="已保存的光斑重复测量">
    <h4>分析 #{item.id} · {item.metadata.conditions} · {item.metadata.axis} 向</h4><HoldoutResult item={item}/><RepeatReadings stats={item.repeatability}/>
    <details><summary>核对原始读数</summary><OriginalWidths item={item}/></details></article>}</>;
  const {fit,metadata,points}=item;
  const range=fit.leave_one_position_out_z_mm;
  const comparison=item.comparison, next=comparison?.next_plane;
  return <article className="beam-fit-result" aria-label="光斑拟合结果">
    <h4>分析 #{item.id} · {metadata.conditions} · {metadata.axis} 向</h4>
    <p>{metadata.reference} · {metadata.wavelength_nm==null?'波长待补':metadata.wavelength_nm+' nm'} · {metadata.medium_n==null?'折射率待补':'n = '+metadata.medium_n} · {fit.distinct_positions} 个测量位置</p>
    <p><strong>{fit.bracketed?'拟合束腰':'区间外的束腰估计'} z = {valueText(fit.waist_z_mm)} mm</strong>，半径 {valueText(fit.waist_radius_mm*1000)} μm。
      发散半角 {valueText(fit.divergence_half_angle_rad*1000)} mrad。{fit.bpp_mm_mrad!=null && <>束腰半径与发散半角乘积 {valueText(fit.bpp_mm_mrad)} mm·mrad。</>}
      {fit.m2!=null && <>{metadata.width_basis==='second_moment'?'二阶矩 M²':'按高斯口径估算的等效 M²'} = {valueText(fit.m2)}。</>}</p>
    {fit.warnings.map(w=><p className="beam-fit-caution" key={w}>{w}</p>)}{fit.next_steps.map(v=><p key={v}>{v}</p>)}
    <FitPlot points={points} fit={fit} model={comparison?.model} holdout={item.holdout}/>
    <HoldoutResult item={item}/>
    <RepeatReadings stats={fit.repeatability}/>
    {onUseFit && <button type="button" className="btn btn-secondary" onClick={()=>onUseFit(item.id)}>将这份拟合带入光路</button>}
    {caseId && <PredictAt caseId={caseId} study={item} onSaved={onForecast}/>}
    <p>半径残差均方根{rmsText(fit.radius_rms_mm)}。{range && <>逐个去掉一个测量位置，用剩余数据重新拟合。{fit.tested_omissions} 次复算有 {fit.valid_omissions} 次得到物理解，束腰{valueText(range[0])===valueText(range[1])?'均约为 '+valueText(range[0]):'位于 '+valueText(range[0])+' 至 '+valueText(range[1])} mm。</>}</p>
    {comparison && <section aria-label="对照版本模型"><h4>工程模型能解释这组读数吗？</h4>
      {comparison.nominal_rms_mm!=null && <><p>按原坐标和标定比较，半径误差均方根{rmsText(comparison.nominal_rms_mm)}。</p>
        <p>若只改变横向标定，模型半径乘以 {valueText(comparison.width_scale)}，剩余误差{rmsText(comparison.scale_rms_mm)}。</p>
        {comparison.shift_rms_mm!=null && <p>若只改变位置基准，把模型坐标取为读数位置加 {valueText(comparison.aligned_waist_shift_mm)} mm，两处束腰对齐，剩余误差{rmsText(comparison.shift_rms_mm)}。</p>}</>}
      {comparison.message && <p>{comparison.message}</p>}
      {next && <p>可复核 z = <strong>{valueText(next.z_mm)} mm</strong> 附近。两个解释在这里分别预测 {valueText(next.scale_radius_mm*1000)} 和 {valueText(next.shift_radius_mm*1000)} μm。这个位置取自可用区间的未测间隙中点，预测差为 {valueText(next.difference_mm*1000)} μm。请对照仪器重复性和该截面的观测条件。</p>}
    </section>}
    <details><summary>核对原数与拟合依据</summary>
      {fit.leave_one_position_out && Object.keys(fit.leave_one_position_out).length>0 && <div className="fit-data-table"><table>
        <caption>每次少用一个测量位置时，拟合结果的变化范围</caption>
        <thead><tr><th>量</th><th>最低值</th><th>此时略去的位置 / mm</th><th>最高值</th><th>此时略去的位置 / mm</th></tr></thead>
        <tbody>{[['waist_z_mm','束腰位置 / mm',1],['waist_radius_mm','束腰半径 / μm',1000],
          ['divergence_half_angle_rad','发散半角 / mrad',1000],['bpp_mm_mrad','束腰半径 × 发散半角 / mm·mrad',1]].map(([key,label,scale])=>{
            const r=fit.leave_one_position_out[key];
            return r && <tr key={key}><td>{label}</td><td>{valueText(r.min*scale)}</td><td>{valueText(r.omitted_at_min_mm)}</td>
              <td>{valueText(r.max*scale)}</td><td>{valueText(r.omitted_at_max_mm)}</td></tr>;
          })}</tbody></table></div>}
      <p>计算时各位置等权，最小化实测半径与模型半径的平方差。{fit.squared_radius_m2!=null && <>如果先平方半径再拟合，传播因子为 {valueText(fit.squared_radius_m2)}。两种结果的差异表明估计对误差口径有多敏感。</>}</p>
      <p>坐标零点的整体偏移会等量移动所报束腰。仪器的宽度标定比例会同时改变半径和发散角。区分这两项需要独立的位置基准和横向标定。</p>
      <OriginalWidths item={item}/>
    </details>
  </article>;
}

function TableMapping({files,form,set}) {
  const [preview,setPreview]=useState(false);
  const file=files.find(f=>f.id===Number(form.attachment_id));
  const workbook=/\.xls[xm]$/i.test(file?.filename||'');
  const field=(key,label,options)=><label>{label}<select aria-label={label} required value={form[key]} onChange={e=>set(key,e.target.value)}>
    <option value="">请选择</option>{Object.entries(options).map(([k,v])=><option value={k} key={k}>{v}</option>)}</select></label>;
  const number=(key,label,props={})=><label>{label}<input type="number" min="1" required value={form[key]} onChange={e=>set(key,e.target.value)} {...props}/></label>;
  return <>
    <div className="beam-fit-fields"><label>仪器原件<select aria-label="仪器原件" required value={form.attachment_id} onChange={e=>set('attachment_id',e.target.value)}><option value="">选择数据文件</option>
      {files.map(f=><option value={f.id} key={f.id}>{f.relative_path||f.filename}</option>)}</select></label>
      {workbook?<label>工作表名<input required value={form.sheet} onChange={e=>set('sheet',e.target.value)}/></label>:field('delimiter','分隔符',{comma:'逗号',tab:'制表符',semicolon:'分号',whitespace:'连续空白',single:'单列原值'})}</div>
    {file && <button type="button" className="btn btn-secondary" onClick={()=>setPreview(!preview)}>{preview?'收起表格':'查看原始表格'}</button>}
    {preview && file && <FilePreview key={file.id} file={file}/>}
    <div className="beam-fit-fields">{number('first_row','数据起始行')}{number('last_row','末行（可留空）',{required:false})}
      {number('position_column','位置列序号')}{field('z_unit','位置单位',units)}{number('width_column','宽度列序号')}
      {field('width_unit','宽度单位',{um:'μm',mm:'mm'})}{field('width_definition','仪器宽度定义',widths)}
      {field('decimal_mark','原表小数符号',{'.':'点（1.25）',',':'逗号（1,25）'})}
      {number('acquisition_column','采集标识列（可留空）',{required:false,max:16384,placeholder:'如逐次取像的文件名列'})}
      {field('axis','测量方向',{X:'X',Y:'Y',T:'T',S:'S'})}{number('wavelength_nm','真空波长 / nm',{required:false,min:0,step:'any',placeholder:'求 M² 时填写'})}
      {number('medium_n','该波长下的折射率',{required:false,min:1,max:5,step:'any',placeholder:'求 M² 时填写'})}
      <label>坐标零点和正方向<input required placeholder="如透镜后表面，沿传播方向为正" value={form.reference} onChange={e=>set('reference',e.target.value)}/></label>
      <label>测量批次与工况<input required value={form.conditions} onChange={e=>set('conditions',e.target.value)}/></label></div>
    <label className="fit-checkbox"><input type="checkbox" checked={form.homogeneous_interval} required onChange={e=>set('homogeneous_interval',e.target.checked)}/>所选行来自同一工况、同一束光和固定测量方向，测点间为连续均匀介质。</label>
  </>;
}

const initial={attachment_id:'',sheet:'',delimiter:'comma',decimal_mark:'.',acquisition_column:'',first_row:2,last_row:'',position_column:1,width_column:2,z_unit:'mm',
  width_unit:'',width_definition:'',axis:'',wavelength_nm:'',medium_n:'',reference:'',conditions:'',homogeneous_interval:false};

export default function BeamStudy({caseId,files=[],readings,revision,disabled=false,refreshToken,onUseFit}) {
  const [open,setOpen]=useState(false), [form,setForm]=useState(initial), [axis,setAxis]=useState('tangential');
  const [fitId,setFitId]=useState(''),[registration,setRegistration]=useState(''),[offset,setOffset]=useState('0');
  const [selected,setSelected]=useState([]), [history,setHistory]=useState([]), [item,setItem]=useState(null);
  const [heldPositions,setHeldPositions]=useState('');
  const [busy,setBusy]=useState(false), [error,setError]=useState(''), [historyError,setHistoryError]=useState(''), [generation,refresh]=useState(0);
  useEffect(()=>{
    if(!open)return;
    let live=true;
    workbenchApi.readBeamStudies(caseId).then(data=>{if(live){setHistory(data);setHistoryError('');}}).catch(e=>{if(live)setHistoryError(e.message);});
    return ()=>{live=false;};
  },[caseId,open,generation,refreshToken]);
  const tables=files.filter(f=>/\.(csv|tsv|txt|xlsx|xlsm)$/i.test(f.filename) || f.read_mode==='archive' || (f.read_mode==='text' && !/\.(pdf|md|json)$/i.test(f.filename)));
  const references=history.filter(h=>['beam_caustic_study','beam_caustic_prediction'].includes(h.type));
  function edit(key,value) {
    if(key==='attachment_id')setHeldPositions('');
    setForm(old=>{
      if(key==='attachment_id') {
        const file=tables.find(f=>f.id===Number(value));
        return {...initial,attachment_id:value,delimiter:/\.tsv$/i.test(file?.filename)?'tab':'comma'};
      }
      return {...old,[key]:value};
    });
    setItem(null);setError('');
  }
  async function request(action) {
    setBusy(true);setError('');
    try {const next=await action();setItem(next);if(next.id)refresh(n=>n+1);}
    catch(e){setItem(null);setError(e.message);} finally{setBusy(false);}
  }
  const analyze=event=>{
    event.preventDefault();
    const source=readings ? {kind:'registered',revision_id:revision,reading_ids:selected,axis} : {...form,kind:'table'};
    if(!readings){
      for(const k of ['attachment_id','first_row','last_row','position_column','width_column','acquisition_column','wavelength_nm','medium_n'])
        source[k]=source[k]===''?null:Number(source[k]);
    }
    let holdout;
    if(!fitId && heldPositions.trim()){
      const values=heldPositions.trim().split(/[\s,，;；]+/).filter(Boolean).map(Number);
      if(!values.length || values.some(v=>!Number.isFinite(v))){
        setError('留出位置应填写有限数值，以空格或逗号分隔，小数用点表示。');return;
      }
      const toMM=readings?1:({mm:1,cm:10,um:.001,m:1000})[source.z_unit];
      holdout={positions_mm:[...new Set(values.map(v=>v*toMM))]};
    }
    request(()=>workbenchApi.runBeamStudy(caseId,source,fitId?{study_id:Number(fitId),note:registration.trim(),z_offset_mm:Number(offset)}:undefined,holdout));
  };
  return <details className="beam-study" onToggle={event=>{if(event.target===event.currentTarget)setOpen(event.currentTarget.open);}}>
    <summary>{readings?'比较多个观察面的测量':'用原件拟合光斑传播'}</summary>
    {open && <><form onSubmit={analyze}><fieldset disabled={busy || disabled}>
      {references.length>0 && <label>计算依据<select aria-label="计算依据" value={fitId}
        onChange={e=>{setFitId(e.target.value);setItem(null);setRegistration('');setOffset('0');}}>
        <option value="">从所选读数拟合</option>{references.map(h=>
          <option value={h.id} key={h.id}>沿用{kindName(h.type)} #{h.id} · {h.conditions} · {h.axis} 向</option>)}</select></label>}
      {readings ? <><p>选用本版本中同一光束、同一方向和同一工况的读数。每个位置可以有多次测量。</p>
        <label>拟合方向<select aria-label="拟合方向" value={axis} onChange={e=>{setAxis(e.target.value);setSelected([]);setItem(null);}}>
          <option value="tangential">T</option><option value="sagittal">S</option></select></label>
        <div className="fit-checklist">{readings.filter(r=>r.revision_id===revision && r[axis]!=null).map(r=><label key={r.id} className="fit-checkbox">
          <input type="checkbox" checked={selected.includes(r.id)} onChange={e=>{setSelected(old=>e.target.checked?[...old,r.id]:old.filter(id=>id!==r.id));setItem(null);}}/>
          #{r.id} · {r.conditions} · z {valueText(r.z_mm)} mm · {r.source_path} · {r.locator}</label>)}</div>
      </> : <TableMapping files={tables} form={form} set={edit}/>}
      {!fitId && <details className="fit-holdout-edit"><summary>保留部分位置检验预测{heldPositions.trim()?' · 已指定位置':''}</summary>
        <p>填写只用于检验的轴向位置。其余位置用于拟合。同一位置的重复测量会一起留出。</p>
        <div className="beam-fit-fields"><label>留出位置 / {readings?'mm':units[form.z_unit]}<input value={heldPositions} placeholder="用空格或逗号分隔，小数用点"
          onChange={e=>{setHeldPositions(e.target.value);setItem(null);setError('');}}/></label>
        </div>{!readings && <p>若原表用一列标识每次取像或采集，可填写列序号。相同标识关联的位置会一起留出。</p>}
      </details>}
      {fitId && <div className="beam-fit-fields"><label>坐标与工况的对应依据<input required maxLength={800} value={registration} onChange={e=>{setRegistration(e.target.value);setItem(null);}}/></label>
        <label>新坐标加多少得到拟合坐标 / mm<input required type="number" step="any" value={offset} onChange={e=>{setOffset(e.target.value);setItem(null);}}/></label></div>}
      <button type="submit" className="btn btn-primary" disabled={readings ? !selected.length : !tables.length}>{busy?'计算中…':fitId?'与已存拟合对照':'拟合这组读数'}</button>
    </fieldset></form>
    {error && <p role="alert">{error}</p>}{historyError && <p role="alert">{historyError} <button type="button" onClick={()=>refresh(n=>n+1)}>重新读取分析</button></p>}
    {history.length>0 && <label>已保存的案例分析<select aria-label="已保存的案例分析" disabled={busy} value={item?.id||''}
      onChange={e=>{if(e.target.value)request(()=>workbenchApi.readBeamStudies(caseId,Number(e.target.value)));else setItem(null);}}>
      <option value="">选择历史结果</option>{history.map(h=><option key={h.id} value={h.id}>{kindName(h.type)} #{h.id} · {h.conditions} · {h.axis} 向</option>)}</select></label>}
    {item && <StudyResult key={item.id||'insufficient'} item={item} caseId={caseId} onUseFit={onUseFit} onForecast={()=>refresh(n=>n+1)}/>}</>}
  </details>;
}
