import React, { useMemo, useRef, useState } from 'react';
import ScientificPlot from './ScientificPlot';
import {SolverReceipt} from './SolverReceipt';
import {scanSamples, scanCsv} from './scanSamples';

export const opticalParameterLabels = {
  length_mm: '长度 / mm', focal_mm: '空气等效焦距 f / mm', radius_mm: '曲率半径 R / mm',
  aoi_deg: '入射角 / °', n: '折射率 n', gradient_per_mm: 'GRIN 梯度 / mm⁻¹',
  front_radius_mm: '前表面曲率 R / mm', back_radius_mm: '后表面曲率 R / mm',
};
const axisName = (axis, system) => {
  const name = system?.elements?.[axis.element_index]?.name || `元件 ${axis.element_index + 1}`;
  return { name, parameter: opticalParameterLabels[axis.parameter] || axis.parameter };
};

function exportSamples(scan, rows, label) {
  const url = URL.createObjectURL(new Blob(['\uFEFF', scanCsv(scan,rows)], {type:'text/csv;charset=utf-8'}));
  const anchor = document.createElement('a');
  anchor.href = url; anchor.download = (scan.maps?'stability-':'scan-')+label+'.csv'; anchor.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}

function SampleFeedback({rows, grid, xLabel, yLabel}) {
  const invalid = rows.filter(r=>r.status==='invalid');
  const partial = rows.filter(r=>r.status==='partial');
  const stable = rows.filter(r=>r.stable===1).length;
  const checked = rows.filter(r=>r.stable!=null).length;
  const issues = new Map();
  for (const row of [...invalid,...partial]) {
    if (!row.reason) continue;
    const group = issues.get(row.reason);
    if (group) group.count += 1;
    else issues.set(row.reason,{count:1,row});
  }
  return <>
    <p className="field-caption" role="status">{invalid.length===rows.length ? '需要调整输入' : invalid.length || partial.length ? '部分完成' : '扫描完成'}
      {' · 共 '+rows.length+' 点，有效 '+(rows.length-invalid.length)+' 点，无效 '+invalid.length+' 点。'}
      {partial.length>0 && '有效点中有 '+partial.length+' 点仅得到部分物理量。'}
      {checked>0 && '双轴稳定 '+stable+' 点，临界或失稳 '+(checked-stable)+' 点。'}</p>
    {issues.size>0 && <details open={invalid.length===rows.length || undefined}>
      <summary>采样问题与参数位置（{issues.size} 类）</summary>
      <ul>{[...issues].slice(0,12).map(([reason,{count,row}])=><li key={reason}>
        {reason} · {count} 点 · {xLabel} = {row.x}{grid && <>，{yLabel} = {row.y}</>}
      </li>)}</ul>
      <small>每类列出一个采样位置。CSV 含逐点坐标、状态及已记录的原因。</small>
    </details>}
  </>;
}

function LinearScan({scan, recordLabel, rows, notes}) {
  const plot=useRef(null), [error,setError]=useState('');
  const xLabel=(scan.element ? scan.element+' · ' : '')+(opticalParameterLabels[scan.parameter] || scan.parameter);
  const cavity=scan.mode ? scan.mode!=='single_pass' : rows.some(r=>r.mt!=null || r.ms!=null);
  const curves=(cavity ? ['mt','ms'] : ['rt','rs']).map((key,i)=>({
    x:rows.map(r=>r.x),y:rows.map(r=>r[key] ?? null),type:'scatter',mode:'lines+markers',connectgaps:false,
    name:i ? '弧矢 S' : '切向 T',line:{color:i?'#d5aa73':'#80d6c5',dash:i?'dash':'solid'},
  }));
  const hasData=curves.some(c=>c.y.some(Number.isFinite));
  return <section className="field-view" aria-label="单参数光学扫描">
    <div className="field-heading"><h3>单参数扫描{recordLabel && ' · '+recordLabel}</h3></div>
    <SolverReceipt saved={scan.solver_receipt}/>
    <p className="field-caption">{xLabel}</p>
    <SampleFeedback rows={rows} xLabel={xLabel}/>
    <div className="field-export"><button onClick={()=>exportSamples(scan,rows,recordLabel)}>导出扫描 CSV</button>
      {hasData && <button onClick={()=>plot.current?.exportImage('svg').catch(e=>setError(e.message))}>导出 SVG</button>}</div>
    {error && <p role="alert">{error}</p>}
    {hasData && <ScientificPlot ref={plot} data={curves} layout={{autosize:true,height:370,
      margin:{l:78,r:28,t:48,b:80},paper_bgcolor:'#131d2a',plot_bgcolor:'#131d2a',font:{color:'#d0deeb'},
      xaxis:{title:{text:xLabel}},yaxis:{title:{text:cavity?'往返矩阵 m = (A+D)/2':'输出面光束半径 / mm'}},
      legend:{orientation:'h'},hovermode:'x unified',
      shapes:cavity?[-1,1].map(y=>({type:'line',xref:'paper',x0:0,x1:1,y0:y,y1:y,line:{color:'#d1ad75',dash:'dot'}})):[],
    }} filename={'scan-'+recordLabel}/>}
    {notes.map(note=><p key={note} role="note">{note}</p>)}
  </section>;
}

function GridScan({ scan, system, recordLabel, rows, notes }) {
  const [metric, setMetric] = useState('stable_both'), [error, setError] = useState('');
  const plot = useRef(null);
  const x = axisName(scan.x_axis, system), y = axisName(scan.y_axis, system);
  const binary = metric === 'stable_both';
  const values = scan.maps[metric];
  const title = binary ? '双轴稳定性' : (metric === 'tangential' ? '切向 T' : '弧矢 S') + ' · m=(A+D)/2';
  const invalid=rows.filter(r=>r.status==='invalid');
  return <section className="field-view" aria-label="二维稳定性参数扫描">
    <div className="field-heading"><h3>二维稳定图{recordLabel && ' · ' + recordLabel}</h3></div>
    <SolverReceipt saved={scan.solver_receipt}/>
    {notes.map(note=><p key={note} role="note">{note}</p>)}
    <p className="field-caption">X：{x.name} · {x.parameter}<br />Y：{y.name} · {y.parameter}</p>
    <SampleFeedback rows={rows} grid xLabel={'X · '+x.parameter} yLabel={'Y · '+y.parameter}/>
    <div className="field-toolbar"><label>颜色表示<select aria-label="稳定图颜色指标" value={metric} onChange={event => setMetric(event.target.value)}>
      <option value="stable_both">双轴稳定 / 非双轴稳定</option><option value="tangential">切向 T 稳定参数 m</option><option value="sagittal">弧矢 S 稳定参数 m</option>
    </select></label><div className="field-export"><button onClick={()=>exportSamples(scan,rows,recordLabel)}>导出扫描 CSV</button>{['svg', 'png'].map(format => <button key={format} onClick={() => plot.current?.exportImage(format).catch(e => setError(e.message))}>{format.toUpperCase()}</button>)}</div></div>
    {error && <p role="alert" className="wb-error">{error}</p>}
    <ScientificPlot ref={plot} data={[{ type: 'heatmap', x: scan.x, y: scan.y, z: values, showscale:invalid.length!==rows.length,
      zmin: binary ? 0 : -1, zmax: 1,
      colorscale: binary ? [[0, '#664452'], [.499, '#664452'], [.5, '#68bda8'], [1, '#68bda8']] : [[0, '#9876c7'], [.5, '#e2eedc'], [1, '#cf8b5f']],
      colorbar: { title: { text: binary ? '' : 'm' }, tickvals: binary ? [0, 1] : [-1, 0, 1],
        ...(binary ? { ticktext: ['非双轴稳定', '双轴稳定'] } : {}), thickness: 14 },
      hoverongaps: false,
      hovertemplate: x.parameter + ' = %{x:.4f}<br>' + y.parameter + ' = %{y:.4f}<br>' + title + ' = %{z:.5g}<extra></extra>',
    }, ...(invalid.length ? [{type:'heatmap',name:'无效输入',showscale:false,hoverongaps:false,
      x:scan.x,y:scan.y,z:scan.maps.stable_both.map(line=>line.map(s=>s==null?1:null)),zmin:0,zmax:1,
      xgap:scan.x.length<=40?1:0,ygap:scan.y.length<=40?1:0,
      text:scan.y.map((_,i)=>rows.slice(i*scan.x.length,(i+1)*scan.x.length).map(r=>r.reason)),
      colorscale:[[0,'#705f40'],[1,'#705f40']],hovertemplate:'X = %{x} · Y = %{y}<br>无效输入 · %{text}<extra></extra>',
    }] : [])]} layout={{ title: { text: title + (recordLabel ? ' · ' + recordLabel : ''), x: .04, font: { size: 14 } },
      height: 450, autosize: true, margin: { l: 110, r: 115, t: 50, b: 90 },
      paper_bgcolor: '#111c28', plot_bgcolor: '#111c28', font: { color: '#d6e2ef', size: 12 },
      xaxis: { title: { text: 'X · ' + x.name.slice(0, 24) + '<br>' + x.parameter, standoff: 14 }, zeroline: false },
      yaxis: { title: { text: 'Y · ' + y.name.slice(0, 24) + '<br>' + y.parameter, standoff: 14 }, zeroline: false }, hovermode: 'closest',
      uirevision: recordLabel + ':' + metric,
    }} filename={'stability-' + recordLabel} />
    <p className="field-caption">{invalid.length>0 && '棕色网格表示输入无效，悬停可查原因。'}|m|&lt;1为稳定；m=±1为临界。连续色图超出±1的数值在端点颜色截断，悬停查看原值。</p>
    <p className="field-note">这是离散参数网格的几何稳定性。空气等效薄透镜焦距与介质内物理焦距需按模型换算；图中不包含输出功率、热负载或效率排名。</p>
  </section>;
}

export default function OpticalScanView({scan,system,recordLabel=''}) {
  const rows=useMemo(()=>scanSamples(scan),[scan]);
  const notes=[...new Set([...(scan.warnings || []),...(scan.scope_warnings || [])])];
  return scan.maps ? <GridScan {...{scan,system,recordLabel,rows,notes}}/> : <LinearScan {...{scan,recordLabel,rows,notes}}/>;
}
