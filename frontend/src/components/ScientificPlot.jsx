import React, { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';

let library;
const loadPlotly = () => library ||= import('plotly.js-cartesian-dist-min').then(module => module.default).catch(error => { library = null; throw error; });

const ScientificPlot = forwardRef(function ScientificPlot({ data, layout, onPoint, onXRange, onViewport, onResetViewport, wheelZoom=false, showToolbar=true, filename, ariaLabel, defer = false }, ref) {
  const host = useRef(null);
  const plotly = useRef(null);
  const draw = useRef(null);
  const callback = useRef(onPoint);
  const rangeCallback = useRef(onXRange);
  const viewportCallback = useRef(onViewport);
  const resetCallback = useRef(onResetViewport);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  callback.current = onPoint;
  rangeCallback.current = onXRange;
  viewportCallback.current = onViewport;
  resetCallback.current = onResetViewport;
  useImperativeHandle(ref, () => ({
    async exportImage(format) {
      const current = draw.current;
      await current?.();
      if (current !== draw.current) throw new Error('图表已切换，请从当前结果重新导出。');
      if (!plotly.current || !host.current?._fullLayout) throw new Error('图表尚未加载完成，请稍后导出。');
      await plotly.current.downloadImage(host.current, { format, filename, width: 1600, height: 850, scale: 2 });
    },
  }), [filename]);
  useEffect(() => {
    const element = host.current;
    let alive = true;
    let render;
    const start = () => render ||= loadPlotly().then(async Plotly => {
      if (!alive) return;
      plotly.current = Plotly;
      await Plotly.react(element, data, layout, {
        responsive: true, displaylogo: false, scrollZoom: wheelZoom, showTips: false,
        displayModeBar: showToolbar, doubleClick: 'reset',
        modeBarButtonsToRemove: ['select2d', 'lasso2d', 'toImage'],
        edits: {},
      });
      if (!alive) return;
      setError('');
      element.removeAllListeners('plotly_click');
      element.on('plotly_click', event => {
        const point = event.points?.[0];
        if (point) callback.current?.(point.x, point.customdata, point.y);
      });
      element.removeAllListeners('plotly_relayout');
      element.removeAllListeners('plotly_doubleclick');
      element.on('plotly_doubleclick',()=>{
        if(resetCallback.current){resetCallback.current();return false;}
      });
      element.on('plotly_relayout', event=>{
        if (viewportCallback.current && Object.keys(event).some(key=>/^[xy]axis\.(range|autorange)/.test(key))) {
          const {xaxis,yaxis}=element._fullLayout;
          viewportCallback.current(event['xaxis.autorange'] || event['yaxis.autorange'] ? null : {x:[...xaxis.range],y:[...yaxis.range]});
        }
        if(!rangeCallback.current) return;
        const bounds=event['xaxis.range'] || [event['xaxis.range[0]'],event['xaxis.range[1]']];
        if(bounds.every(Number.isFinite)) rangeCallback.current([Math.min(...bounds),Math.max(...bounds)]);
        else if(event['xaxis.autorange']) rangeCallback.current(null);
      });
    });
    draw.current = start;
    const paint = () => start().catch(err => { if (alive) setError(`绘图加载失败：${err.message}`); });
    let observer;
    if (defer && !element._fullLayout && typeof IntersectionObserver !== 'undefined') {
      observer = new IntersectionObserver(entries => {
        if (entries.some(entry=>entry.isIntersecting)) { observer.disconnect(); paint(); }
      }, {rootMargin:'200px'});
      observer.observe(element);
    } else paint();
    return () => { alive = false; observer?.disconnect(); draw.current = null; };
  }, [data, layout, attempt, defer, wheelZoom, showToolbar]);
  useEffect(() => {
    const element = host.current;
    const observer = new ResizeObserver(() => {
      if (element._fullLayout && element.clientWidth) plotly.current?.Plots.resize(element);
    });
    observer.observe(element);
    return () => { observer.disconnect(); plotly.current?.purge(element); };
  }, []);
  return <div className="oa-plot-host">{error && <p role="alert">{error} <button className="btn btn-secondary" onClick={() => { setError(''); setAttempt(value => value + 1); }}>重新加载图表</button></p>}<div ref={host} className="oa-plot" role="img" aria-label={ariaLabel || (onPoint ? '可缩放光学分析图，点击曲线选择精确游标位置' : '可缩放光学分析图')} /></div>;
});

export default ScientificPlot;
