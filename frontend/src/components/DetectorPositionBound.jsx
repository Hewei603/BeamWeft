import React from 'react';

export default function DetectorPositionBound({value, onChange}) {
  return <div className="section-fields">
    <label>轴向位置 ± / mm<input type="number" min="0" step="any" placeholder="已知时填写"
      value={value.mm} onChange={event=>onChange({...value, mm:event.target.value})}/></label>
    <label>定位界限依据<input maxLength={500} value={value.source} required={value.mm!==''}
      placeholder="如位移台校准记录中的绝对界限" onChange={event=>onChange({...value, source:event.target.value})}/></label>
  </div>;
}
