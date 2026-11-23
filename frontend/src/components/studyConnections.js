export function fitsPort(target,source) {
  const shapes=source.connects_as ?? (source.shape==='scalar' ? ['scalar','interval'] : [source.shape]);
  return source.quantity===target.quantity && shapes.includes(target.shape) && (source.unit!==target.unit || source.scale===target.scale)
    && (target.shape!=='state' || source.unit===target.unit);
}

export function compatiblePorts(target, records) {
  return records.flatMap(record=>Object.entries(record.available_outputs || {})
    .filter(([,source])=>fitsPort(target,source))
    .map(([port,source])=>{
      const units=target.shape==='state' ? '' : source.unit===target.unit ? source.unit : source.unit+' → '+target.unit;
      return {run_id:record.id,port,declaration:source,label:[`#${record.id} ${record.title}`,source.label,units].filter(Boolean).join(' · ')};
    }));
}

export const sourceKey=ref=>ref.table ? JSON.stringify([ref.sha256,Object.entries(ref.table).sort(([a],[b])=>a.localeCompare(b))])
  : ref.revision_id ? JSON.stringify(['project',ref.revision_id,ref.port]) : JSON.stringify([ref.run_id,ref.port]);

export function exclusiveFields(schema) {
  // More complex JSON Schema alternatives must retain every control for manual editing.
  const fields=[];
  for(const option of schema.oneOf || []) {
    if(option.required?.length!==1 || Object.keys(option).some(k=>k!=='required' && k!=='properties'))return [];
    const key=option.required[0];
    if(!Object.hasOwn(schema.properties,key) || fields.includes(key))return [];
    if(option.properties) {
      const constraint=option.properties[key];
      if(Object.keys(option.properties).length!==1 || !constraint || Object.keys(constraint).length!==1 ||
        !constraint.not || Object.keys(constraint.not).length!==1 || constraint.not.type!=='null')return [];
    }
    fields.push(key);
  }
  return fields.length>1?fields:[];
}
