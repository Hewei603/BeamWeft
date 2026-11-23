import React from 'react';

export default function ExclusiveStudyInput({fields,schema,choice,onChange}) {
  if(!fields.length)return null;
  return <label>本次使用的输入<select aria-label="本次使用的输入" value={choice} onChange={event=>onChange(event.target.value)}>
    <option value="">请选择一种输入</option>{fields.map(key=><option key={key} value={key}>{schema.properties[key].title || key}</option>)}
  </select></label>;
}
