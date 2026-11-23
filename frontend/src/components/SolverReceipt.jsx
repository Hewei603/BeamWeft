import {providerLabel} from '../brand';
import React, {useEffect, useRef, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import {DeferredDetails, JsonDetails} from './DeferredDetails';
import {downloadJSON} from './downloadFile';
import './SolverReceipt.css';

const unknown = '未记录';
const states = {completed:'已完成', partial:'部分结果', needs_input:'需补输入', unsupported:'超出模型范围'};
const knownHash = value => typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value);

export function compareReceipts(a,b) {
  if (!knownHash(a?.sha256) || !knownHash(b?.sha256)) return 'unknown';
  if (a.scope==='method_only' || b.scope==='method_only') return 'unknown';
  if (a.sha256 !== b.sha256) return 'different';
  return a.complete === true && b.complete === true ? 'same' : 'unknown';
}

function fields(receipt) {
  if (!receipt) return {};
  const deviceRows = (receipt.devices || []).flatMap(device=>{
    const name = device.method+' · '+providerLabel(device.provider)+' · v'+device.version;
    return [
      ['元件模型 · '+name, device.implementation_sha256 || '实现指纹未记录'],
      ['元件接口 · '+name, device.contract_sha256],
    ];
  });
  return {
    '方法':receipt.method, '提供方':providerLabel(receipt.provider), '方法版本':receipt.version,
    '软件版本':receipt.application?.version,
    '应用源码指纹':receipt.application?.source_sha256, '方法实现指纹':receipt.implementation_sha256,
    '计算接口指纹':receipt.contract_sha256,
    ...Object.fromEntries(Object.entries(receipt.runtime || {}).map(([key,value])=>[
      ({python:'Python',numpy:'NumPy',scipy:'SciPy',platform:'操作系统平台',machine:'处理器架构'})[key] || key, value])),
    ...Object.fromEntries(deviceRows),
    '记录完整性':receipt.scope==='method_only' ? '仅核对已安装方法，旧输入所用的元件实现未知'
      : receipt.complete ? '已记录所列实现' : '部分实现信息缺失',
    '记录指纹':receipt.sha256,
  };
}

function ReceiptValue({value}) {
  if (value == null) return <span className="solver-unknown">{unknown}</span>;
  if (knownHash(value)) return <code title={value}>{value.slice(0,12)}…</code>;
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

export function ReceiptFields({saved, current, left='保存时', right='当前安装'}) {
  const paired = current !== undefined;
  const a = fields(saved), b = fields(current);
  const changed = key=>Boolean(saved && current && a[key]!==b[key]);
  const rows = [...new Set([...Object.keys(a), ...Object.keys(b)])]
    .filter(key=>a[key]!=null || b[key]!=null)
    .sort((x,y)=>Number(changed(y))-Number(changed(x)));
  const outcome = paired && compareReceipts(saved,current);
  return <div className="solver-info">
    {!saved && <p>{left}缺少求解器记录。</p>}
    {paired && !current && <p>{right}没有可核对的方法记录。</p>}
    {saved && current && <p>{outcome === 'same' ? '两份记录所列的实现和运行环境一致。' :
      outcome === 'different' ? '两份求解器记录有差异。表中标出了变化的项目。' :
      '现有指纹不足以确认两次的实现和运行环境一致。'}</p>}
    {!!rows.length && <table><thead><tr><th scope="col">项目</th><th scope="col">{left}</th>
      {paired && <th scope="col">{right}</th>}</tr></thead><tbody>{rows.map(key=>
        <tr key={key} data-diff={changed(key) || undefined}>
          <th scope="row">{key}{changed(key) && <small className="solver-changed">有变化</small>}</th><td><ReceiptValue value={a[key]}/></td>
          {paired && <td><ReceiptValue value={b[key]}/></td>}</tr>)}</tbody></table>}
    {saved && <JsonDetails title={left+'的完整求解器记录'} value={saved}/>}
    {current && <JsonDetails title={right+'的完整求解器记录'} value={current}/>}
  </div>;
}

export function SolverReceipt({saved,current,left,right}) {
  return <DeferredDetails className="solver-receipt" title="求解器记录">
    <ReceiptFields saved={saved} current={current} left={left} right={right}/>
  </DeferredDetails>;
}

const quantityNames = {
  system:'主光路', n:'折射率', planes:'计算平面', tangential:'切向', sagittal:'弧矢', evaluated_devices:'已求值元件',
  matrix:'传输矩阵', matrix_t:'切向矩阵', matrix_s:'弧矢矩阵', elements:'光路元件',
  downstream_waist_in_air:'出射后空气中的束腰', radius_mm:'光束半径 (mm)', curvature_mm:'波前曲率半径 (mm)',
  q_imag_mm:'Im(q) (mm)', q_real_mm:'Re(q) (mm)', distance_from_output_mm:'距出射面 (mm)',
  collimated_focus_from_exit_mm:'平行入射焦点距出射面 (mm)', collimated_focus_note:'平行入射焦点说明',
};

function describePath(path) {
  return path.map((part,i)=>{
    if (typeof part !== 'number') return quantityNames[part] || part;
    if (['matrix_t','matrix_s'].includes(path[i-1]) && part<4) return ['A','B','C','D'][part];
    return String(part);
  }).join(' / ');
}

function outputValue(diff,key) {
  if (!Object.hasOwn(diff,key)) return '无此字段';
  const value=diff[key];
  if (value===null) return '空值';
  if (typeof value==='object') return value.items!=null ? value.items+' 项' : value.fields+' 个字段';
  if (typeof value==='number') {
    const other = diff[key==='saved' ? 'recomputed' : 'saved'];
    const short = x=>String(Number(x.toPrecision(8)));
    const label = typeof other==='number' && other!==value && short(value)===short(other) ? String(value) : short(value);
    return <span className="solver-number" title={String(value)}>{label}</span>;
  }
  return String(value);
}

function DifferenceTable({changes, heading, saved, current}) {
  return <div className="solver-info solver-values"><table>
    <thead><tr><th scope="col">{heading}</th><th scope="col">{saved}</th><th scope="col">{current}</th></tr></thead>
    <tbody>{changes.map(d=><tr key={JSON.stringify(d.path)}>
      <th scope="row" title={d.path.join(' / ')}>{describePath(d.path)}</th>
      <td>{outputValue(d,'saved')}</td><td>{outputValue(d,'recomputed')}</td>
    </tr>)}</tbody></table></div>;
}

export function RecheckResult({result}) {
  const counts=result.difference_counts;
  const parsed=result.input_interpretation;
  const status=s=>states[s] || s || unknown;
  return <div className="solver-recheck-result">
    <p role="status"><strong>{result.total_differences ? '已存输出与本次复算有差异' : '本次复算与已存输出逐项一致'}</strong>
      {' · 版本 #'+result.revision_id}</p>
    <p>{'保存时为「'+status(result.saved_status)+'」，本次为「'+status(result.recomputed_status)+'」。已逐值比对 '+result.compared_numbers+' 个数值。'}</p>
    {result.total_differences>0 && <><p>{'数值差异 '+counts.numeric+' 项，结构差异 '+counts.structure+' 项，其他值差异 '+counts.other+' 项。'}
      {result.total_differences>result.differences.length && '下面列出前 '+result.differences.length+' 项。'}</p>
      <DifferenceTable changes={result.differences} heading="输出字段" saved="保存时" current="本次复算"/>
      <small>数值按完整精度比较。导出文件保留所列差异的原始字段和值。</small></>}
    {parsed && <DeferredDetails title={'本次如何解释旧输入（'+parsed.total_differences+' 处）'}>
      <p>下表列出原工程和本次实际输入之间的区别，包括当前软件补入的默认值、转换或未采用的字段。</p>
      <DifferenceTable changes={parsed.differences} heading="输入字段" saved="工程中保存的值" current="本次解析值"/>
      {parsed.total_differences>parsed.differences.length && <small>列出前 {parsed.differences.length} 处。</small>}
    </DeferredDetails>}
    <SolverReceipt saved={result.saved_solver_receipt} current={result.recomputed_solver_receipt} right="本次复算"/>
    <button type="button" className="btn btn-secondary" onClick={()=>downloadJSON('revision-'+result.revision_id+'-recheck.json',result)}>导出核对结果</button>
  </div>;
}

export function ProjectRecheck({caseId,revision,analysis,edited}) {
  const [result,setResult]=useState(null), [error,setError]=useState(''), [busy,setBusy]=useState(false);
  const active=useRef(null);
  useEffect(()=>()=>active.current?.abort(),[]);
  const run=async()=>{
    active.current?.abort();
    const request=new AbortController();
    active.current=request;
    setBusy(true); setError(''); setResult(null);
    try {
      const value=await workbenchApi.recheckProject(caseId,revision,request.signal);
      if (!request.signal.aborted) setResult(value);
    } catch(cause) { if (!request.signal.aborted) setError(cause.message); }
    finally { if (!request.signal.aborted) setBusy(false); }
  };
  return <DeferredDetails title="求解器与复算" className="solver-receipt">
    <p>{'复核使用版本 #'+revision+' 保存的全部工程输入。'}
      {edited && '当前画布还有编辑，复核采用保存时的参数。'}</p>
    {!result && <ReceiptFields saved={analysis?.solver_receipt}/>}
    <button type="button" className="btn btn-secondary" disabled={busy || !analysis} onClick={run}>
      {busy?'正在复核已存输出…':'用已存输入复核'}</button>
    {!analysis && <p>该版本缺少可比对的已存输出。</p>}
    {error && <p role="alert">{error}</p>}
    {result && <RecheckResult result={result}/>}
  </DeferredDetails>;
}
