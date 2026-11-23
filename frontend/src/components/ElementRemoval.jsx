import React from 'react';
import {elementRemoval, removeOptic} from './elementRemovalPlan.js';

export default function ElementRemoval({project, index, pathUid = 'primary', unlocked, disabled, onEdit}) {
  const plan = elementRemoval(project,index,pathUid,unlocked);
  if (!plan) return null;
  if (plan.blockers.length) return <details className="bench-insert">
    <summary>移除前需处理 {plan.blockers.length} 项关联</summary>
    <ul>{plan.blockers.map((message,i)=><li key={i}>{message}</li>)}</ul>
  </details>;
  const apply = geometry => onEdit(removeOptic(project,index,{pathUid,unlocked,geometry,assemblyUid:plan.group?.uid}));
  const object = plan.group ? '整件「' + plan.group.name + '」' : '此元件';
  const subject = plan.group ? '移除' + object : '移除';
  let closeLabel = plan.group ? subject : '移除元件';
  if (plan.length) closeLabel = subject + '并缩短光路 ' + plan.length + ' mm';
  if (plan.endMirror) closeLabel = (plan.group ? subject : '移除端镜') + '（腔体待补全）';
  const close = <button type="button" className="bench-text-btn" disabled={disabled} onClick={()=>apply('close')}>
    {closeLabel}</button>;
  return <>
    {plan.group && <small>{plan.group.name} · 整件含 {plan.pieces.length} 个光学元素。</small>}
    {plan.stockIds.length>0 && <small>从这处安装位卸下库存 #{plan.stockIds.join('、#')} 的实物。</small>}
    {plan.laws > 0 && <small>移除时一并撤下此元件的 {plan.laws} 项功率参数。</small>}
    {plan.calibrated > 0 && <small>另有 {plan.calibrated} 条支路登记了这件介质的折射率设置，将随它撤下。</small>}
    {plan.canKeep && <>
      {plan.keepAir && <small>原占位将留作 {plan.length} mm 空气间距。</small>}
      <button type="button" className="bench-text-btn" disabled={disabled} onClick={()=>apply('keep')}>移除{object}并保留其余位置</button>
    </>}
    {plan.canClose && (plan.canKeep ? plan.keepAir && <details className="bench-insert"><summary>其他拆除方式</summary>
      <p>{plan.primary ? '下游元件向入口移动。' : '支路接入口保持固定，上游元件和源面向接入口移动。'}</p>{close}</details> : close)}
  </>;
}
