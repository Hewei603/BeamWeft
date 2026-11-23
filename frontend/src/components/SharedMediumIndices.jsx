import React from 'react';
import {sharedIndexReading, withSharedIndex} from './sharedMedium.js';

export function SharedIndexRow({element, path, trace, sameWavelength, change}) {
  const uid = element.uid;
  const entry = uid === path.target_uid;
  const value = entry ? path.target_n : path.downstream?.find(row=>row.target_uid === uid)?.n;
  const independent = path.independent_index_uids?.includes(uid) || Number.isFinite(value);
  const automatic = Boolean(path.through_uid) && path.inherit_index_at_same_wavelength !== false;
  const snap = trace?.shared_route?.find(row=>row.element_uid === uid);
  const restore = !element.index_reference?.model && sameWavelength && Number.isFinite(element.n)
    ? '此段沿用主路 n = '+Number(element.n.toPrecision(7)) : '恢复自动取值';
  const label = entry ? '介质在入射波长的 n' : element.name+' · 入射波长折射率';
  return <div className="shared-index-row" data-element-uid={uid} role="group" aria-label={element.name+' · 共享折射率'}>
    <div className="shared-index-summary">
      <strong>{element.name}</strong>
      <span className="shared-index-result" title={snap?.catalog_source}>{sharedIndexReading(snap)}</span>
      {snap?.catalog_source && <small className="shared-index-citation">{snap.catalog_source}
        {snap.valid_range_nm?.length===2 && ' · '+snap.valid_range_nm.map(n=>Number(n.toPrecision(7))).join('–')+' nm'}</small>}
      {Number.isFinite(snap?.reference_axis_opl_mm) && <small>本段轴上单程相位光程 · {Number(snap.reference_axis_opl_mm.toPrecision(8))} mm</small>}
    </div>
    <div className="shared-index-edit">
      <label>支路 n{path.wavelength_nm!=null && ' · '+path.wavelength_nm+' nm'}
        <input type="number" step="any" min="1" max="5" aria-label={label} value={value??''}
          disabled={!Number.isFinite(path.wavelength_nm)} placeholder={independent || !automatic ? '待填写' : '自动'}
          onChange={e=>change(withSharedIndex(path,uid,e.target.value==='' ? null : Number(e.target.value)))}/>
      </label>
      {automatic && (independent ?
        <button type="button" onClick={()=>change(withSharedIndex(path,uid,null,false))}>{restore}</button> :
        <button type="button" onClick={()=>change(withSharedIndex(path,uid,null))}>独立标定</button>)}
    </div>
  </div>;
}

export function SharedMediumReadings({routes, uid, traces, open}) {
  const connected = routes.filter(route=>route.shared.some(row=>row.element.uid === uid));
  if (!connected.length) return null;
  return <section className="bench-shared-indices" aria-label="共享实物的支路折射率"><h5>各支路在此处的折射率</h5>
    {connected.map(({path})=>{
      const trace = traces?.find(t=>t.uid===path.uid);
      const row = trace?.shared_route?.find(r=>r.element_uid===uid);
      return <button type="button" key={path.uid} onClick={()=>open(path.uid,uid)}
        aria-label={'编辑'+path.name+'在此处的折射率'}>
        <strong>{path.name} · {path.wavelength_nm==null?'波长待填':path.wavelength_nm+' nm'} ↘</strong>
        <span>{sharedIndexReading(row)}</span>
      </button>;
    })}</section>;
}
