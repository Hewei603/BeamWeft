import React, { useState } from 'react';
import { localId } from '../api/localId.js';

export default function AssemblyEditor({project, pathUid, rows, index, disabled, onEdit}) {
  const [name, setName] = useState('');
  const [end, setEnd] = useState('');
  if (index == null || !rows[index]) return null;
  const groups = project.assemblies || [];
  const onPath = groups.filter(g=>g.path_uid===pathUid);
  const members = new Set(onPath.flatMap(g=>g.element_uids));
  const uid = rows[index].element.uid;
  const group = onPath.find(g=>g.element_uids.includes(uid));
  if (group) {
    const root = project.component_context || {};
    const ctx = pathUid==='primary' ? root : root.path_contexts?.[pathUid] || {};
    const frozen = [ctx.crystal_element_uids, ...(ctx.crystal_groups || []).map(g=>g.element_uids),
      ...Object.values(ctx.components || {}).map(c=>c?.medium_projection?.element_uids)]
      .some(ids=>ids?.some(id=>group.element_uids.includes(id)));
    return <details className="bench-assembly"><summary>{group.name} · {group.element_uids.length} 个元素</summary>
      <p>{rows.filter(r=>group.element_uids.includes(r.element.uid)).map(r=>r.element.name).join(' → ')}</p>
      {group.basis==='legacy_inferred' && <p>旧工程按邻接关系推定了这件实物的边界。若实验中是独立器件，可解除组合。</p>}
      {frozen ? <small>实物边界来自已选库存的分段记录。</small> :
        <button type="button" disabled={disabled} onClick={()=>onEdit({...project, assemblies:groups.filter(g=>g.uid!==group.uid)})}>解除实物组合</button>}
    </details>;
  }
  const candidates = [rows[index]];
  const parts = pathUid==='primary' ? project.parts : project.incident_paths?.find(p=>p.uid===pathUid)?.parts || [];
  const stock = new Map(parts.map(p=>[p.element_index,p.inventory_id]));
  const stockIds = new Set(stock.get(index)==null ? [] : [stock.get(index)]);
  for (const row of rows.slice(index+1)) {
    if (members.has(row.element.uid)) break;
    if (stock.get(row.index)!=null) stockIds.add(stock.get(row.index));
    if (stockIds.size>1) break;
    candidates.push(row);
  }
  const last = candidates.find(r=>r.element.uid===end) || candidates[0];
  return <details className="bench-assembly"><summary>定义实物组合</summary>
    <p>「{rows[index].element.name}」可以独立代表一件实物。若它与后续元素固定在一起，选到最后一个。拖动这件实物时会保留各段长度。</p>
    <label>末元素<select aria-label="末元素" value={last.element.uid} onChange={e=>setEnd(e.target.value)}>{candidates.map(r=>
      <option key={r.element.uid} value={r.element.uid}>{r.index+1} · {r.element.name}{r.index===index?'（仅此元素）':''}</option>)}</select></label>
    <label>实物名称<input value={name} maxLength={120} onChange={e=>setName(e.target.value)}/></label>
    <button type="button" disabled={disabled || !name.trim()} onClick={()=>onEdit({...project,assemblies:[...groups,{
      uid:localId(), name:name.trim(), path_uid:pathUid, element_uids:rows.slice(index,last.index+1).map(r=>r.element.uid),
    }]})}>组成一件实物</button>
  </details>;
}
