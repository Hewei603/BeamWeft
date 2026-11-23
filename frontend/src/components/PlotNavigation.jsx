import React, {useRef, useState} from 'react';
import {fitInterval, minimumSpan, revealPosition, zoomInterval} from './plotViewport';

function IntervalFields({view, bounds, onChange}) {
  const [start, setStart] = useState(String(view.start));
  const [end, setEnd] = useState(String(view.end));
  const [error, setError] = useState('');
  function apply(event) {
    event.preventDefault();
    const a = Number(start), b = Number(end);
    if (!start.trim() || !end.trim() || !Number.isFinite(a) || !Number.isFinite(b) || b <= a) {
      setError('请输入两个有效坐标，终点须大于起点。');
    } else if (a < bounds.start || b > bounds.end) {
      setError('当前光路范围为 '+bounds.start+' 至 '+bounds.end+' mm。');
    } else if (b-a < minimumSpan(bounds)) {
      setError('该间距小于当前坐标能分辨的范围，请扩大区间。');
    } else {
      setError('');
      onChange({start:a, end:b});
    }
  }
  return <form onSubmit={apply} noValidate>
    <label>起点 / mm<input type="number" step="any" value={start} onChange={e=>{setStart(e.target.value);setError('');}}/></label>
    <label>终点 / mm<input type="number" step="any" value={end} onChange={e=>{setEnd(e.target.value);setError('');}}/></label>
    <button type="submit">查看区间</button>
    {error && <span role="alert">{error}</span>}
  </form>;
}

export default function PlotNavigation({view, bounds, focusZ, focusName, onChange,transverseScale,onTransverseScale}) {
  const details = useRef(null);
  const span = view.end - view.start;
  if (bounds.end - bounds.start <= minimumSpan(bounds)) return null;
  const fit = view.start === bounds.start && view.end === bounds.end;
  const focusVisible = Number.isFinite(focusZ) && focusZ>=view.start && focusZ<=view.end;
  const pan = direction => onChange(fitInterval(view.start + direction*span/2, span, bounds));
  return <div className="bench-viewport" role="group" aria-label="光路视野">
    <div className="bench-view-actions">
      <button type="button" aria-label="向上游平移视野" title="向上游平移半个视野" disabled={view.start<=bounds.start} onClick={()=>pan(-1)}>←</button>
      <button type="button" aria-label="放大轴向视野" title={focusVisible?'围绕所选位置放大':'围绕当前视野中心放大'} disabled={span<=minimumSpan(bounds)} onClick={()=>onChange(zoomInterval(view,bounds,.5,focusZ))}>放大</button>
      <button type="button" aria-label="缩小轴向视野" disabled={fit} onClick={()=>onChange(zoomInterval(view,bounds,2))}>缩小</button>
      <button type="button" aria-label="向下游平移视野" title="向下游平移半个视野" disabled={view.end>=bounds.end} onClick={()=>pan(1)}>→</button>
      {!fit && <button type="button" disabled={!Number.isFinite(focusZ) || focusVisible} aria-label="定位所选位置"
        title={'在当前尺度查看'+focusName} onClick={()=>onChange(revealPosition(view,bounds,focusZ))}>定位所选</button>}
      <button type="button" disabled={fit} onClick={()=>onChange(null)}>全图</button>
    </div>
    <details ref={details}><summary>轴向区间 / mm</summary>
      <IntervalFields key={view.start+' '+view.end} view={view} bounds={bounds} onChange={next=>{
        onChange(next);
        details.current.open=false;
        details.current.querySelector('summary').focus({preventScroll:true});
      }}/>
      <small>区间边界按相邻的计算采样点插值。</small>
    </details>
    {!fit && <select value={transverseScale} onChange={e=>onTransverseScale(e.target.value)} aria-label="纵轴范围"
      title="按此区间的可见曲线与测量值缩放纵轴，也可沿用全程尺度比较不同位置">
      <option value="window">纵轴 · 当前区间</option><option value="full">纵轴 · 全程尺度</option>
    </select>}
  </div>;
}
