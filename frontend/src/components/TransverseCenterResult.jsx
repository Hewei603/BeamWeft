import React from 'react';
import RegistrationDetails from './RegistrationDetails';
import './TransverseCenter.css';

const number = value => Number.isFinite(value) ? Number(value.toPrecision(6)).toString() : '—';
const quality = {usable:'可比较',conditional:'有条件可比',limited:'比较受限',unavailable:'缺少比较条件'};
const axes = [['tangential','T'], ['sagittal','S']];

function CenterPlot({observed,reference}) {
  const values=[observed.tangential,observed.sagittal,reference.tangential,reference.sagittal];
  if(!values.every(Number.isFinite))return null;
  // Both axes share a physical scale, with the optical axis always in view.
  const extent=Math.max(...values.map(Math.abs));
  if(extent===0)return <p>实测与预测都位于 T/S 原点。</p>;
  const point=p=>({x:100+73*(p.tangential/extent),y:94-73*(p.sagittal/extent)});
  const m=point(observed),p=point(reference);
  return <figure className="center-plot"><svg viewBox="0 0 202 183" role="img" aria-label="实测与预测在观察面 T/S 坐标中的位置">
    <path d="M19 94H185M100 173V11" className="center-axis"/>
    <path d="M173 91v6M97 21h6" className="center-axis"/>
    <text x="187" y="98">T</text><text x="105" y="14">S</text>
    <line x1={p.x} x2={m.x} y1={p.y} y2={m.y} className="center-vector"/>
    <g transform={'translate('+m.x+' '+m.y+')'}><circle r="5" className="center-observed"/></g>
    <g transform={'translate('+p.x+' '+p.y+')'}><path d="M-6 0H6M0 -6V6" className="center-reference"/></g>
  </svg><figcaption>○ 实测 · ＋ 预测<br/>两轴同尺度，刻线距原点 {number(extent)} mm。</figcaption></figure>;
}

export default function TransverseCenterResult({output, showWarnings=true, headingRef, children}) {
  const {observed_mm:observed={},reference_mm:reference={},residual_mm:residual={}}=output;
  const warnings=new Set(output.warnings || []),notes=new Map();
  for(const [key,label] of axes){
    const note=output.axis_notes?.[key];
    if(note && !warnings.has(note))notes.set(note,[...(notes.get(note) || []),label]);
  }
  const comparable=axes.filter(([key])=>Number.isFinite(residual?.[key]));
  return <section className="center-result" aria-label="实测与预测束心">
    <h5 ref={headingRef} tabIndex={headingRef?-1:undefined}>实测与预测束心</h5><p>{quality[output.comparison_quality] || '查看比较条件'}
      {comparable.length===1 && ' · 仅 '+comparable[0][1]+' 轴有差值'}
      {Number.isFinite(output.distance_mm) && ' · 平面距离 '+number(output.distance_mm)+' mm'}</p>
    <div className="center-readout"><div><table aria-label="观察面坐标与位置差，单位毫米">
      <caption>观察面坐标 / mm</caption>
      <thead><tr><th scope="col">轴</th><th scope="col">配准实测</th><th scope="col">工程预测</th><th scope="col">实测 − 预测</th></tr></thead>
      <tbody>{axes.map(([key,label])=><tr key={key}><th scope="row">{label}</th><td>{number(observed?.[key])}</td>
        <td>{number(reference?.[key])}</td><td>{number(residual?.[key])}</td></tr>)}</tbody>
    </table>{comparable.length<2 && <p>“—”表示该项缺少可比较的读数。</p>}
    {[...notes].map(([note,labels])=><p key={note}>{labels.join('/')} 轴 · {note}</p>)}</div>
      {comparable.length===2 && <CenterPlot observed={observed} reference={reference}/>}</div>
    {showWarnings && [...warnings].map(w=><p key={w}>{w}</p>)}
    {output.registration && <details><summary>坐标标定与点位检验</summary>
      <RegistrationDetails registration={output.registration} frame="工程 T/S 平面"/></details>}
    {children}
  </section>;
}
