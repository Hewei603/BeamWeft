import React, {useEffect, useRef, useState} from 'react';
import {useNavigate} from 'react-router-dom';
import {designApi} from '../api/design';
import {ScientificStudyOutput} from './StudyPanels';

const policyLabels = {
  evidence_only: '仅采用有依据的输入',
  exploratory: '条件设计探索',
};
const measurement = (value, digits = 3) => value == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits: digits});
const stabilityChart = {name:'cold-cavity-stability', title:'冷腔稳定性', chart:['后间隙 / mm', '稳定性 (A+D)/2', [['冷腔稳定性', 'rows.*.output_gap_mm', 'rows.*.stability_m', 'lines+markers']]]};
const radiusChart = {name:'pump-beam-radius', title:'泵浦二阶矩半径', chart:['位置 z / mm', '泵浦二阶矩半径 / mm', [['光束半径', 'points.*.z_mm', 'points.*.w_mm', 'lines']]]};

function TargetComparison({comparison, rows}) {
  const depth = comparison.depth_from_crystal_front_mm;
  // Older saved assessments recorded array indices instead of the number used to open a sample.
  const closest = comparison.closest_rows ?? comparison.closest_row_indices?.map(index => index + 1) ?? [];
  return <section aria-label="目标模半径比较"><h5>目标模半径比较</h5>
    <p>目标为增益段中心的模半径 {measurement(comparison.target_radius_um, 6)} μm。
      {depth == null ? '需要确定增益段中心的位置才能比较。' : `目标面距晶体前端面 ${measurement(depth, 6)} mm。`}</p>
    {closest.length > 0 ? <div className="design-table-scroll"><table>
      <caption>{closest.length === 1 ? '最接近目标的试算点' : `并列最接近目标的 ${closest.length} 个试算点`}</caption>
      <thead><tr><th>试算点</th><th>后间隙 / mm</th><th>目标面模半径 / μm</th><th>绝对误差 / μm</th></tr></thead>
      <tbody>{closest.map(number => {
        const row = rows[number - 1];
        return row && <tr key={number}><th scope="row">#{number}</th><td>{measurement(row.output_gap_mm, 6)}</td>
          <td>{measurement(row.target_plane_radius_um, 6)}</td><td>{measurement(row.target_error_um, 6)}</td></tr>;
      })}</tbody>
    </table></div> : depth != null && <p>此范围内没有可用于比较的稳定腔模。</p>}
  </section>;
}

const geometryLabels = {cold_cavity:'冷腔', pump_path:'泵浦光路', coupled_layout:'泵浦与冷腔（共用晶体）'};

function editablePump(design, candidate) {
  if (design.assumption_policy === 'evidence_only') {
    const pump = design.evidence_assessment?.pump_imaging;
    return pump?.editable_geometry ?? (pump?.status === 'modeled' && pump.beam?.points?.length ? 'propagation' : null);
  }
  if (!candidate) return null;
  if ('editable_pump' in candidate) return candidate.editable_pump;
  const pump = candidate.pump;
  if (!pump || !['geometry_only','geometric_model','assumed_beam'].includes(pump.status)) return null;
  const inputs = design.resolved_inputs || {};
  const f1 = pump.collimator_f_mm ?? inputs.collimator_f_mm, f2 = pump.focus_f_mm ?? inputs.focus_f_mm;
  if (!(f1 > 0 && f2 > 0) || !['source','collimator','focus_lens'].every(key=>Number.isFinite(pump.positions?.[key]))) return null;
  return pump.status === 'geometry_only' ? 'geometry' : 'propagation';
}

function OpenDesignGeometry({caseId, contentId, target, selection, selectTarget, targets, pumpKind}) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const pending = useRef(null);
  useEffect(() => () => pending.current?.abort(), []);
  const open = async () => {
    const request = new AbortController();
    pending.current = request;
    setBusy(true);
    setError('');
    try {
      const model = await designApi.importGeometry(caseId, contentId, selection, target, request.signal);
      if (!request.signal.aborted) navigate(`/cases/${caseId}?model_revision=${model.revision_id}`);
    } catch (cause) {
      if (!request.signal.aborted) setError(cause.message);
    } finally {
      if (!request.signal.aborted) setBusy(false);
    }
  };
  return <div className="design-open-geometry">
    {targets.length > 1 && <label>打开的光路 <select aria-label="打开的光路" value={target} onChange={e => selectTarget(e.target.value)} disabled={busy}>
      {targets.map(key => <option value={key} key={key}>{geometryLabels[key]}</option>)}
    </select></label>}
    <button className="btn btn-secondary" type="button" disabled={busy} onClick={open}>
      {busy ? '正在建立独立方案…' : targets.length > 1 ? '作为独立方案打开' : `将此${geometryLabels[target]}作为独立方案打开`}
    </button>
    {pumpKind === 'geometry' && target !== 'cold_cavity' && <p>可打开已记录的泵浦元件与位置。补齐缺少的传播参数后，可在画布上查看泵浦光斑。</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}

export default function DesignBasis({design, caseId, contentId, selectedRow, onSelectRow, openingTarget}) {
  const {cold_cavity: cavity, pump_imaging: pump} = design.evidence_assessment || {};
  const assumptions = design.assumptions || [];
  const scan = cavity?.mode === 'scan';
  const rows = cavity?.rows || [];
  const [localRow, setLocalRow] = useState(design.selected_rank || 1);
  const selection = selectedRow ?? localRow;
  const selectRow = onSelectRow || setLocalRow;
  const [preferredTarget, selectTarget] = useState(openingTarget);
  const candidate = design.candidates?.find(item => item.rank === selection);
  const pumpKind = editablePump(design, candidate);
  const hasCavity = !!rows[selection - 1] || !!candidate;
  const targets = [...(pumpKind && hasCavity ? ['coupled_layout'] : []),
    ...(hasCavity ? ['cold_cavity'] : []), ...(pumpKind ? ['pump_path'] : [])];
  const target = targets.includes(preferredTarget) ? preferredTarget : targets[0];
  const entry = design.assumption_policy === 'evidence_only' && target === 'pump_path' ? 1 : selection;
  const table = !!rows.length && <div className="design-table-scroll"><table>
    <thead><tr><th>前间隙 / mm</th><th>后间隙 / mm</th><th>腔长 / mm</th><th>稳定性 (A+D)/2</th><th>模式半径 / mm</th><th>取样位置 z / mm</th></tr></thead>
    <tbody>{rows.map((row, i) => <tr key={i}>
      <td>{measurement(row.input_gap_mm)}</td><td>{measurement(row.output_gap_mm)}</td><td>{measurement(row.length_mm)}</td>
      <td>{measurement(row.stability_m, 5)} · {row.marginal ? '稳定边界' : row.stable ? '稳定' : '不稳定'}</td>
      <td>{measurement(row.mode_radius_mm, 5)}</td><td>{measurement(row.evaluation_z_mm)}</td>
    </tr>)}</tbody>
  </table></div>;
  return <section className="design-section" aria-label="本轮计算依据">
    <h3>{policyLabels[design.assumption_policy] || '历史版本未记录计算取值方式'}</h3>
    {!!assumptions.length && <details><summary>本次计算采用的假设与约定 · {assumptions.length} 项</summary>
      <ul>{assumptions.map((item, index) => <li key={index}>
        <strong>{item.name}</strong> {item.value} {item.unit}<br/>{item.basis}
      </li>)}</ul>
    </details>}
    {cavity && <section><h4>冷腔几何</h4>
      <p>{cavity.summary}</p>
      {cavity.geometry && <p>{cavity.geometry.scope}</p>}
      {cavity.target_comparison && <TargetComparison comparison={cavity.target_comparison} rows={rows}/>}
      {scan ? <>
        <ScientificStudyOutput definition={stabilityChart} output={{rows}}/>
        <details><summary>查看 {rows.length} 个试算点的坐标和腔模</summary>{table}</details>
        {contentId && target !== 'pump_path' && <label>要打开的试算点 <select value={selection} onChange={event => selectRow(Number(event.target.value))}>
          {rows.map((row, i) => <option value={i + 1} key={i}>#{i + 1} · 后间隙 {measurement(row.output_gap_mm)} mm · 腔长 {measurement(row.length_mm)} mm</option>)}
        </select></label>}
      </> : table}
    </section>}
    {pump && <section><h4>泵浦几何成像</h4>
      <p>{pump.summary}</p>
      {pump.status === 'modeled' && pump.magnification != null && <p>设计共轭倍率 {measurement(pump.magnification)} 倍</p>}
      {pump.geometric_core_image_radius_mm != null && <p>名义共轭处的纤芯几何像半径 {measurement(pump.geometric_core_image_radius_mm, 5)} mm</p>}
      {!!pump.beam?.points?.length && <><ScientificStudyOutput definition={radiusChart} output={pump.beam}/><p>{pump.scope}</p></>}
    </section>}
    {!onSelectRow && contentId && design.candidates?.length > 1 && <label>要打开的候选 <select value={selection} onChange={e => selectRow(Number(e.target.value))}>
      {design.candidates.map(candidate => <option key={candidate.rank} value={candidate.rank}>#{candidate.rank} · 腔长 {measurement(candidate.length_mm)} mm</option>)}
    </select></label>}
    {contentId && target && <OpenDesignGeometry key={`${target}-${entry}`} caseId={caseId} contentId={contentId} target={target}
      selection={entry} targets={targets} selectTarget={selectTarget} pumpKind={pumpKind}/>}
  </section>;
}
