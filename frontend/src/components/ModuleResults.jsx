import React, { useLayoutEffect, useRef, useState } from 'react';
import { useLanguage } from '../LanguageContext';
import { productName } from '../brand';

/** Typed, human-readable renderers for physics/measurement module results.
 *  Falls back to nothing when the result shape is missing — the caller keeps
 *  the collapsible raw-JSON view for full detail. */

function StatChips({ items }) {
  return (
    <div className="stat-row">
      {items.filter(([, v]) => v !== undefined && v !== null && v !== '').map(([label, value]) => (
        <div key={label} className="stat-chip">
          <span className="stat-label">{label}</span>
          <span className="stat-value">{String(value)}</span>
        </div>
      ))}
    </div>
  );
}

/** Fitted power-curve line as a small SVG: threshold marker + linear region. */
function PowerCurveChart({ fit, units, data }) {
  if (!fit || fit.threshold_pump == null || !fit.fit_line) return null;
  const pumpMax = Math.max(fit.at_max_pump || 0, fit.threshold_pump * 1.2, 1e-9);
  const outMax = Math.max(fit.max_output || 0, 1e-9);
  const W = 420; const H = 190; const PAD = 40;
  const x = (p) => PAD + (p / pumpMax) * (W - PAD - 10);
  const y = (o) => H - PAD + 10 - (o / outMax) * (H - PAD - 22);
  const thX = x(Math.max(fit.threshold_pump, 0));
  const endY = y(fit.fit_line.a * pumpMax + fit.fit_line.b);
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  return (
    <svg className="power-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="power curve">
      <line x1={PAD} y1={H - PAD + 10} x2={W - 6} y2={H - PAD + 10} className="axis" />
      <line x1={PAD} y1={8} x2={PAD} y2={H - PAD + 10} className="axis" />
      {ticks.map((f) => (
        <g key={`xt${f}`}>
          <line x1={x(f * pumpMax)} y1={H - PAD + 10} x2={x(f * pumpMax)} y2={H - PAD + 14} className="axis" />
          <text x={x(f * pumpMax)} y={H - PAD + 26} textAnchor="middle" className="chart-note">{(f * pumpMax).toFixed(1)}</text>
        </g>
      ))}
      {ticks.map((f) => (
        <g key={`yt${f}`}>
          <line x1={PAD - 4} y1={y(f * outMax)} x2={PAD} y2={y(f * outMax)} className="axis" />
          <text x={PAD - 7} y={y(f * outMax) + 3.5} textAnchor="end" className="chart-note">{(f * outMax).toFixed(1)}</text>
        </g>
      ))}
      <line x1={x(0)} y1={y(0)} x2={thX} y2={y(0)} className="fit-flat" />
      <line x1={thX} y1={y(0)} x2={x(pumpMax)} y2={endY} className="fit-line" />
      {(data?.pump || []).map((p, i) => (
        <circle key={i} cx={x(p)} cy={y(data.output[i])} r="2.6" className="data-point" />
      ))}
      <line x1={thX} y1={y(0)} x2={thX} y2={16} className="threshold-mark" />
      <text x={thX + 4} y={26} className="chart-note">P_th={fit.threshold_pump}</text>
      <text x={W - 8} y={H - 4} textAnchor="end" className="chart-note">{units?.pump || ''}</text>
      <text x={PAD + 4} y={14} className="chart-note">{units?.output || ''}</text>
    </svg>
  );
}

// The kernel supplies both coordinates in mm. The two display axes have
// independent scales, so an optical radius stays readable in a long cavity.
export function CavityLayoutDiagram({ placement, envelope }) {
  const { t, te } = useLanguage();
  const host = useRef(null);
  const [width, setWidth] = useState(560);
  const points = envelope?.points || [];
  const zEnd = points.at(-1)?.z_mm;
  const maxRadius = points.reduce((max, point) => Math.max(max, point.w_mm), 0);
  const drawable = Boolean(placement?.length && points.length >= 2 && zEnd > 0 && maxRadius > 0);

  useLayoutEffect(() => {
    if (!drawable) return;
    const element = host.current;
    const measure = () => {
      const next = Math.round(element.getBoundingClientRect().width);
      if (next > 0) setWidth(next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [drawable]);
  if (!drawable) return null;

  const left = 57;
  const right = width - 15;
  const middle = 97;
  const half = 43;
  const tickStep = 0.5 * 10 ** Math.floor(Math.log10(maxRadius));
  const radiusLimit = Number((Math.ceil(maxRadius / tickStep) * tickStep).toPrecision(8));
  const x = (z) => left + z / zEnd * (right - left);
  const dy = (w) => w / radiusLimit * half;
  const upper = points.map((p) => `${x(p.z_mm).toFixed(2)},${(middle - dy(p.w_mm)).toFixed(2)}`);
  const lower = [...points].reverse().map((p) => `${x(p.z_mm).toFixed(2)},${(middle + dy(p.w_mm)).toFixed(2)}`);
  const outline = `M${upper.join(' L')} L${lower.join(' L')} Z`;
  const mirrors = placement.filter((p) => /mirror/i.test(p.element));
  const faces = placement.filter((p) => /crystal/i.test(p.element));
  const waist = envelope.waist;
  const tickLabel = (value) => Number(value.toPrecision(8)).toString();
  const zTicks = width >= 460 ? [0, 0.25, 0.5, 0.75, 1] : [0, 0.5, 1];
  return (
    <figure className="cavity-figure">
      <h4>{t('caseDetail.physics.envelopeTitle')}</h4>
      <div className="cavity-plot">
        <div ref={host}>
          <svg className="cavity-diagram" viewBox={`0 0 ${width} 214`} role="img"
            aria-label={t('caseDetail.physics.envelopeTitle')}>
            <text className="chart-note" x="4" y="15">{t('caseDetail.physics.transverseAxis')}</text>
            {[-1, 0, 1].map((sign) => (
              <g key={sign}>
                <line className={sign ? 'guide' : 'axis'} x1={left} x2={right}
                  y1={middle - sign * half} y2={middle - sign * half} />
                <text className="chart-note tick" x={left - 8} y={middle - sign * half + 4}
                  textAnchor="end">{tickLabel(sign * radiusLimit)}</text>
              </g>
            ))}
            {faces.length === 2 && (
              <rect className="crystal" x={x(faces[0].position_mm)} y={middle - half - 9}
                width={Math.max(0, x(faces[1].position_mm) - x(faces[0].position_mm))} height={(half + 9) * 2}>
                <title>{t('caseDetail.physics.crystalLabel')}</title>
              </rect>
            )}
            <path className="beam" d={outline} />
            {mirrors.map((mirror, i) => (
              <g key={mirror.element}>
                <line className="mirror" x1={x(mirror.position_mm)} x2={x(mirror.position_mm)}
                  y1={middle - half - 8} y2={middle + half + 8} />
                <text className="chart-note element-label" x={x(mirror.position_mm)} y={middle - half - 17}
                  textAnchor={i === 0 ? 'start' : 'end'}>{te(mirror.element)}</text>
              </g>
            ))}
            {waist && <g>
              <line className="waist-mark" x1={x(waist.z_mm)} x2={x(waist.z_mm)}
                y1={middle - half - 9} y2={middle + half + 9} />
              <path className="waist-tip" d={`M${x(waist.z_mm)},${middle + half + 9} l-4,6 h8 Z`} />
            </g>}
            <line className="distance-axis" x1={left} x2={right} y1="164" y2="164" />
            {zTicks.map((fraction) => (
              <g key={fraction}>
                <line className="distance-axis" x1={x(fraction * zEnd)} x2={x(fraction * zEnd)} y1="164" y2="169" />
                <text className="chart-note tick" x={x(fraction * zEnd)} y="184"
                  textAnchor={fraction === 1 ? 'end' : fraction === 0 ? 'start' : 'middle'}>
                  {fraction === 1 ? zEnd : tickLabel(fraction * zEnd)}
                </text>
              </g>
            ))}
            <text className="chart-note" x={(left + right) / 2} y="207" textAnchor="middle">{t('caseDetail.physics.distanceAxis')}</text>
          </svg>
        </div>
        {waist && <p className="cavity-legend"><i aria-hidden="true" />{t('caseDetail.physics.waistMark')}
          <span>w₀ = {waist.w0_mm} mm</span><span>z = {waist.z_mm} mm</span></p>}
        {faces.length === 2 && <p className="cavity-legend"><i className="crystal-key" aria-hidden="true" />
          {t('caseDetail.physics.crystalLabel')}<span>{faces[0].position_mm} – {faces[1].position_mm} mm</span></p>}
      </div>
      <figcaption className="diagram-caption">{t('caseDetail.physics.layoutScale')}</figcaption>
      {!waist && <p className="diagram-caption">{t(envelope.waist_location === 'outside'
        ? 'caseDetail.physics.outsideWaist' : 'caseDetail.physics.unlocatedWaist')}</p>}
    </figure>
  );
}

// All cavity result entry points use the same chart, quantities and positions.
export function PlacementBlock({ placement, envelope, readings = [], thermal = [] }) {
  const { t, te } = useLanguage();
  const rows = readings.filter(([, value]) => value != null);
  const hasPlot = Boolean(placement?.length && envelope?.points?.length >= 2);
  if (!placement?.length && !rows.length && thermal[1] == null) return null;
  const hasSlab = placement?.filter((p) => /crystal/i.test(p.element)).length === 2;
  const positions = hasPlot ? placement.filter((p) =>
    !/mirror/i.test(p.element) && !(hasSlab && /crystal/i.test(p.element))) : placement;
  return (
    <div className="cavity-report">
      <div className={hasPlot ? 'cavity-composition with-plot' : 'cavity-composition'}>
        {hasPlot && <div className="cavity-drawing">
          <CavityLayoutDiagram placement={placement} envelope={envelope} />
        </div>}
        {(rows.length > 0 || thermal[1] != null) && <div className="cavity-quantities">
          <h4>{t('caseDetail.physics.readingsTitle')}</h4>
          <dl className="cavity-readings">{rows.map(([label, value, unit]) => (
            <div key={label}><dt>{label}</dt><dd>{value}{unit && <small> {unit}</small>}</dd></div>
          ))}</dl>
          {thermal[1] != null && <dl className="cavity-thermal">
            <dt>{thermal[0]}</dt><dd>{thermal[1]}</dd>
            <dd className="cavity-thermal-method">{t('caseDetail.physics.thermalMethod')}</dd>
          </dl>}
        </div>}
      </div>
      {positions?.length > 0 && <div className="cavity-positions">
        <strong>{t('caseDetail.physics.placement')}</strong>
        <dl>{positions.map((p) => <div key={p.element}><dt>{te(p.element)}</dt><dd>{p.position_mm} mm</dd></div>)}</dl>
      </div>}
    </div>
  );
}

export function PowerCurveResult({ result, showComparison = true }) {
  const { t } = useLanguage();
  const fit = result.fit;
  if (!fit) return null;
  return (
    <div className="generated-section">
      <StatChips items={[
        [t('caseDetail.modulesTab.threshold'), `${fit.threshold_pump} ${result.units?.pump || ''}`],
        [t('caseDetail.modulesTab.slopeEff'), `${fit.slope_efficiency_pct}%`],
        ['R²', fit.r_squared],
        ['T_oc', result.output_coupler_T_pct != null ? `${result.output_coupler_T_pct}%` : null],
      ]} />
      <PowerCurveChart fit={fit} units={result.units} data={result.data} />
      {(fit.warnings || []).map((w) => <div key={w} className="disclaimer">{w}</div>)}
      {showComparison && result.comparison && result.comparison.length > 1 && (
        <div>
          <h3>{t('caseDetail.modulesTab.seriesCompare')}</h3>
          <table className="mini-table">
            <thead><tr><th>{t('caseDetail.modulesTab.colSeries')}</th><th className="num">T_oc</th><th className="num">{t('caseDetail.modulesTab.threshold')}</th><th className="num">{t('caseDetail.modulesTab.slopeEff')}</th></tr></thead>
            <tbody>
              {result.comparison.map((s) => (
                <tr key={s.module_id}>
                  <td>{s.label}</td>
                  <td className="num">{s.output_coupler_T_pct != null ? `${s.output_coupler_T_pct}%` : '-'}</td>
                  <td className="num">{s.threshold_pump}</td>
                  <td className="num">{s.slope_efficiency_pct != null ? `${s.slope_efficiency_pct}%` : '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {result.findlay_clay?.applicable && (
            <StatChips items={[
              [`${t('caseDetail.modulesTab.cavityLoss')} (Findlay-Clay)`, `${result.findlay_clay.round_trip_loss_pct}%`],
              ['Caird η₀', result.caird?.applicable ? `${result.caird.intrinsic_slope_pct}%` : null],
            ]} />
          )}
          {/* The two-point-fit warning ("R²=1 is meaningless, measure a third
              curve") exists precisely to stop this number going into a paper
              unqualified — swallowing it defeats the analysis. */}
          {result.findlay_clay?.applicable === false && result.findlay_clay?.note && (
            <div className="disclaimer">{t('caseDetail.modulesTab.fcNotApplicable')}{result.findlay_clay.note}</div>
          )}
          {[result.findlay_clay?.warning, result.caird?.warning].filter(Boolean).map((w) => (
            <div key={w} className="disclaimer">{w}</div>
          ))}
          {result.caird?.applicable === false && result.caird?.note && (
            <div className="disclaimer">Caird: {result.caird.note}</div>
          )}
        </div>
      )}
    </div>
  );
}

function thermalMarginChip(record, t) {
  const power = record.thermal_lens_tolerance_dioptre;
  const kind = record.thermal_lens_limit_kind || 'sampled';
  const label = t('caseDetail.physics.thermalLimit.' + kind);
  if (power == null) return [label, null];
  const value = Number(power.toPrecision(7)) + ' D';
  const focal = record.thermal_lens_tolerance_min_f_mm;
  return [label, kind === 'critical' && focal ? `≈ ${value} · f ≈ ${Number(focal.toPrecision(7))} mm` : value];
}

export function CavityDesignResult({ result }) {
  const { t } = useLanguage();
  const rec = result.recommended || result.analysis;
  // An unstable cold cavity cannot supply a confined Gaussian beam envelope.
  const unstable = rec && rec.stable === false;
  const k = (key) => t('caseDetail.physics.' + key);
  return (
    <div className="generated-section cavity-result">
      {unstable && <div className="error">{t('caseDetail.modulesTab.unstableWarn')}</div>}
      {rec && <header className="cavity-result-header">
        <h3>{t(result.recommended ? 'caseDetail.modulesTab.recommended' : 'caseDetail.modulesTab.fixedGeometry')}</h3>
        {rec.stable === true && <span className="cavity-stable">{k('stableCold')}</span>}
      </header>}
      {result.scan?.stable_windows_mm && (
        <StatChips items={[[t('caseDetail.modulesTab.stableWindows'),
          result.scan.stable_windows_mm.length
            ? result.scan.stable_windows_mm.map(([a, b]) => `${a} – ${b} mm`).join('；')
            : '—']]} />
      )}
      {rec && <PlacementBlock placement={unstable ? null : result.placement} envelope={result.beam_envelope}
        readings={[
          [k('cavityLength'), rec.length_mm, 'mm'],
          [k('wavelength'), result.wavelength_nm, 'nm'],
          [k('stabilityTrace'), rec.stability_m],
          [k('waistRadius'), !unstable ? rec.waist_w0_mm : null, 'mm'],
          [k('mirror1Radius'), !unstable ? rec.w_on_mirror1_mm : null, 'mm'],
          [k('mirror2Radius'), !unstable ? rec.w_on_mirror2_mm : null, 'mm'],
          [k('crystalRadius'), !unstable ? rec.w_in_crystal_mm : null, 'mm'],
        ]} thermal={thermalMarginChip(rec, t)} />}
      {rec?.criterion && <p className="cavity-model-note"><strong>{t('caseDetail.modulesTab.criterion')}</strong> {rec.criterion}</p>}
      {Boolean(result.assumptions?.length || result.disclaimer) && <div className="cavity-model-notes">
        {result.assumptions?.length > 0 && <p><strong>{t('caseDetail.modulesTab.assumptions')}</strong> {result.assumptions.join('；')}</p>}
        {result.disclaimer && <p>{result.disclaimer}</p>}
      </div>}
      {rec && result.summary ? <details className="cavity-original-summary"><summary>{k('calculationSummary')}</summary>
        <p>{result.summary}</p>{result.beam_envelope?.note && <p>{result.beam_envelope.note}</p>}
      </details> : result.summary && <p className="result-summary">{result.summary}</p>}
    </div>
  );
}

export function PhaseMatchResult({ result }) {
  const { t, te } = useLanguage();
  if (!result.solutions) return null;
  const matched = result.solutions.filter((s) => s.theta_deg != null);
  return (
    <div className="generated-section">
      {result.summary && <p className="result-summary">{result.summary}</p>}
      <h3>{t('caseDetail.modulesTab.pmSolutions')}</h3>
      <table className="mini-table">
        <thead><tr><th>{t('caseDetail.modulesTab.colPlane')}</th><th className="num">θ (°)</th><th className="num">φ (°)</th><th className="num">λ₃ (nm)</th><th className="num">{t('caseDetail.modulesTab.colWalkoff')}</th><th>{t('caseDetail.modulesTab.colConfidence')}</th></tr></thead>
        <tbody>
          {matched.map((s, i) => (
            <tr key={i}>
              <td>{s.plane || 'uniaxial'}</td>
              <td className="num">{s.theta_deg}</td>
              <td className="num">{s.phi_deg ?? '-'}</td>
              <td className="num">{s.lambda3_nm}</td>
              <td className="num">{s.walkoff_mrad != null ? `${s.walkoff_mrad} mrad` : '-'}</td>
              <td><span className={`meta-pill conf-${s.confidence}`}>{te(s.confidence)}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
      {/* The principal-plane caveat and "verify the cut with the vendor" line
          are the guard against ordering a mis-cut crystal — always visible. */}
      {result.disclaimer && <div className="disclaimer">{result.disclaimer}</div>}
    </div>
  );
}

export function CoatingTmmResult({ result }) {
  const { t, lang, te } = useLanguage();
  const sb = result.stopband;
  const summary = lang === 'zh' && sb?.edges_nm
    ? `原型阻带约 ${Math.round(sb.edges_nm[0])} – ${Math.round(sb.edges_nm[1])} nm（中心 ${sb.center_nm} nm）`
    : result.summary;
  const spectral = ['exact_stack','vendor_curve'].includes(result.mode_used);
  const quantities = ['R','T','A'].filter(key=>(result.query_results || []).some(row=>row[key]!=null));
  return (
    <div className="generated-section">
      {summary && <p className="result-summary">{summary}</p>}
      {(result.warnings || []).map(note=><p className="disclaimer" key={note}>{note}</p>)}
      {spectral ? <table className="data-table">
        <thead><tr><th>{lang==='zh'?'波长 / nm':'Wavelength / nm'}</th>
          {quantities.map(key=><th key={key}>{key} / %</th>)}<th>{lang==='zh'?'适用范围':'Validity'}</th></tr></thead>
        <tbody>{(result.query_results || []).map((row,i)=><tr key={i}><td>{row.wavelength_nm}</td>
          {quantities.map(key=><td key={key} className="num">{row[key]==null?'—':Number((row[key]*100).toFixed(5))}</td>)}
          <td>{row.reason || (row.in_measured_range===false ? (lang==='zh'?'超出测量范围':'Outside measured range') :
            row.is_representative_only ? (lang==='zh'?'代表性模型':'Representative model') : '—')}</td></tr>)}</tbody>
      </table> : <div className="stat-row">
        {(result.query_results || []).map((q) => (
          <div key={q.wavelength_nm} className={`stat-chip band-${q.band_position}`}>
            <span className="stat-label">{q.wavelength_nm} nm</span>
            <span className="stat-value">{te(q.band_position)}{q.margin_to_edge_nm != null ? ` (${q.margin_to_edge_nm}nm)` : ''}</span>
          </div>
        ))}
      </div>}
      {result.stopband && (
        <StatChips items={[
          [t('caseDetail.modulesTab.stopbandLabel'), `${result.stopband.edges_nm?.[0]} – ${result.stopband.edges_nm?.[1]}`],
          [t('caseDetail.modulesTab.confidenceLabel'), result.confidence],
        ]} />
      )}
      {result.disclaimer && <div className="disclaimer">{lang === 'zh' ? t('caseDetail.modulesTab.coatingDisclaimer') : result.disclaimer}</div>}
    </div>
  );
}

export function ComponentMatchResult({ result }) {
  const { t } = useLanguage();
  if (!result.candidates) return null;
  return (
    <div className="generated-section">
      {result.summary && <p className="result-summary">{result.summary}</p>}
      <h3>{t('caseDetail.modulesTab.matchCandidates')}</h3>
      {result.candidates.map((c) => (
        <div key={c.item_id} className="source-row">
          <div>
            <strong>#{c.item_id} {c.name}</strong>
            <p className="muted">{c.raw_spec}</p>
            {(c.must_measure || []).length > 0 && (
              <p className="muted">⚠ {t('caseDetail.modulesTab.mustMeasure')}: {c.must_measure.join('；')}</p>
            )}
          </div>
          <span className={`meta-pill ${c.frontier ? 'pill-frontier' : ''}`}>
            {c.frontier ? t('caseDetail.modulesTab.frontier') : t('caseDetail.modulesTab.eligible')}
          </span>
        </div>
      ))}
      {result.rejection_reasons && Object.keys(result.rejection_reasons).length > 0 && (
        <div>
          <h3>{t('caseDetail.modulesTab.matchRejected')}</h3>
          <div className="stat-row">
            {Object.entries(result.rejection_reasons).map(([reason, count]) => (
              <div key={reason} className="stat-chip"><span className="stat-label">{reason}</span><span className="stat-value">×{count}</span></div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function LiteratureSearchResult({ result }) {
  const { t } = useLanguage();
  if (!result.papers) return null;
  const downloadBibtex = () => {
    // Client-side .bib assembly: entries were rendered by the backend from
    // real index records, never by a model.
    const blob = new Blob([result.papers.map((p) => p.bibtex).filter(Boolean).join('\n\n')],
      { type: 'text/plain;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `${productName.toLowerCase()}_references.bib`;
    link.click();
    URL.revokeObjectURL(link.href);
  };
  return (
    <div className="generated-section">
      {result.summary && <p className="result-summary">{result.summary}</p>}
      {(result.source_errors || []).map((err) => (
        <div key={err} className="disclaimer">{err}</div>
      ))}
      {result.papers.length > 0 && (
        <button type="button" className="btn btn-secondary no-print" onClick={downloadBibtex}>
          {t('caseDetail.modulesTab.litExportBibtex')}
        </button>
      )}
      {result.papers.map((paper) => (
        <div key={paper.doi || paper.arxiv_id || paper.title} className="source-row">
          <div>
            <strong>
              {paper.url
                ? <a href={paper.url} target="_blank" rel="noreferrer">{paper.title}</a>
                : paper.title}
            </strong>
            <p className="muted">
              {(paper.authors || []).slice(0, 3).join(', ')}
              {(paper.authors || []).length > 3 ? t('caseDetail.modulesTab.litEtAl') : ''}
            </p>
            {paper.abstract && (
              <details>
                <summary className="muted">{t('caseDetail.modulesTab.litAbstract')}</summary>
                <p className="muted">{paper.abstract}</p>
              </details>
            )}
          </div>
          <div className="loan-cell">
            {paper.year && <span className="meta-pill">{paper.year}</span>}
            {paper.venue && <span className="meta-pill">{paper.venue}</span>}
            {paper.cited_by != null && (
              <span className="meta-pill">{t('caseDetail.modulesTab.litCitedBy')} {paper.cited_by}</span>
            )}
            <span className="meta-pill">{paper.source}</span>
            {paper.pdf_url && (
              <a className="meta-pill" href={paper.pdf_url} target="_blank" rel="noreferrer">PDF</a>
            )}
          </div>
        </div>
      ))}
      {result.disclaimer && <div className="disclaimer">{result.disclaimer}</div>}
    </div>
  );
}

export function ModuleResult({ moduleType, result, showComparison = true }) {
  // needs_input AND failed both carry their explanation in `message` — a
  // failed run must show WHY, not just a status badge over an empty card.
  if (!result || result.status === 'needs_input' || result.status === 'failed') {
    if (!result?.message) return null;
    return (
      <div className={result.status === 'failed' ? 'error' : 'disclaimer'}>
        {result.message}
      </div>
    );
  }
  if (moduleType === 'power_curve') return <PowerCurveResult result={result} showComparison={showComparison} />;
  if (moduleType === 'cavity_design') return <CavityDesignResult result={result} />;
  if (moduleType === 'phase_match') return <PhaseMatchResult result={result} />;
  if (moduleType === 'coating_tmm') return <CoatingTmmResult result={result} />;
  if (moduleType === 'component_match') return <ComponentMatchResult result={result} />;
  if (moduleType === 'literature_search') return <LiteratureSearchResult result={result} />;
  return result.summary ? <p className="result-summary">{result.summary}</p> : null;
}

/** Kernel results injected into a generated plan (content.computed_physics).
 *
 * This is the line between "deterministically computed" and "model prose":
 * without it the student cannot tell which numbers were calculated and which
 * were written by the LLM — the core promise of v2.
 */
export function PhysicsFactsBlock({ physics }) {
  const { t } = useLanguage();
  if (!physics) return null;

  if (!physics.available) {
    return (
      <div className="error" style={{ marginBottom: '1rem' }}>
        <strong>⚠ {t('caseDetail.physics.notRun')}</strong>
        <p>{t('caseDetail.physics.notRunHint')}</p>
        {(physics.skipped || []).length > 0 && (
          <p className="muted">{t('caseDetail.physics.skippedTitle')}: {physics.skipped.join('；')}</p>
        )}
      </div>
    );
  }

  const results = physics.results || {};
  const search = results.cavity_design_search;
  const design = results.cavity_design;
  const pm = results.phase_matching;
  const coating = results.coating_check;
  const rec = search?.recommended;

  return (
    <div className="generated-section" style={{ border: '1px solid #2f5d3a', borderRadius: '8px', padding: '0.9rem' }}>
      <div className="generated-card-title">
        <h3>{t('caseDetail.physics.title')}</h3>
        <span className="meta-pill">{(physics.tools_run || []).join(' · ')}</span>
      </div>

      {(physics.skipped || []).length > 0 && (
        <div className="disclaimer">{t('caseDetail.physics.skippedTitle')}: {physics.skipped.join('；')}</div>
      )}

      {search && (
        <div>
          {search.note && <p className="muted">{search.note}</p>}
          {rec && <PlacementBlock placement={rec.element_placement} envelope={rec.beam_envelope}
            readings={[
              ['R1', rec.R1], ['R2', rec.R2],
              [t('caseDetail.physics.cavityLength'), rec.length_mm, 'mm'],
              [t('caseDetail.physics.wavelength'), search.wavelength_nm, 'nm'],
              [t('caseDetail.physics.stabilityTrace'), rec.stability_m],
              [t('caseDetail.physics.waistRadius'), rec.waist_w0_mm, 'mm'],
              [t('caseDetail.physics.mirror1Radius'), rec.spot_on_mirror1_mm, 'mm'],
              [t('caseDetail.physics.mirror2Radius'), rec.spot_on_mirror2_mm, 'mm'],
              [t('caseDetail.physics.crystalRadius'), rec.spot_in_crystal_mm, 'mm'],
            ]} thermal={thermalMarginChip(rec, t)} />}
          {(search.alternatives || []).length > 0 && (
            <details>
              <summary className="muted">{t('caseDetail.physics.alternatives')} ({search.alternatives.length})</summary>
              <table className="mini-table">
                <tbody>
                  {search.alternatives.map((c, i) => (
                    <tr key={i}>
                      <td>{c.R1} + {c.R2}</td>
                      <td className="num">L={c.length_mm} mm</td>
                      <td className="num">w0={c.waist_w0_mm} mm</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
          {search.search_space && (
            <p className="muted">
              {t('caseDetail.physics.searchSpace')}:
              {' '}{search.search_space.geometries_evaluated} {t('caseDetail.physics.evaluated')} ·
              {' '}{search.search_space.stable_configurations_found} {t('caseDetail.physics.stableFound')} ·
              {' '}{t('caseDetail.physics.rocSource')}: {search.search_space.roc_source === 'lab inventory'
                ? t('caseDetail.physics.rocInventory') : t('caseDetail.physics.rocCatalog')}
            </p>
          )}
          {(search.caveats || []).map((c) => <div key={c} className="disclaimer">{c}</div>)}
        </div>
      )}

      {design && (
        <div>
          {design.summary && <p className="result-summary">{design.summary}</p>}
          <PlacementBlock placement={design.element_placement} envelope={design.beam_envelope}
            readings={[
              [t('caseDetail.physics.cavityLength'), design.recommended_length_mm, 'mm'],
              [t('caseDetail.physics.stabilityTrace'), design.stability_m],
              [t('caseDetail.physics.waistRadius'), design.waist_w0_mm, 'mm'],
              [t('caseDetail.physics.mirror1Radius'), design.spot_on_mirror1_mm, 'mm'],
              [t('caseDetail.physics.mirror2Radius'), design.spot_on_mirror2_mm, 'mm'],
              [t('caseDetail.physics.crystalRadius'), design.spot_in_crystal_mm, 'mm'],
            ]} thermal={thermalMarginChip(design, t)} />
        </div>
      )}

      {pm && Object.entries(pm).map(([stage, entry]) => (
        <div key={stage}>
          <h3>{t('caseDetail.physics.phaseTitle')} — {stage}</h3>
          <table className="mini-table">
            <thead><tr><th>{t('caseDetail.modulesTab.colPlane')}</th><th className="num">θ (°)</th><th className="num">φ (°)</th><th className="num">λ₃ (nm)</th></tr></thead>
            <tbody>
              {(entry.solutions || []).map((s, i) => (
                <tr key={i}>
                  <td>{s.plane || 'uniaxial'}</td>
                  <td className="num">{s.theta_deg}</td>
                  <td className="num">{s.phi_deg ?? '-'}</td>
                  <td className="num">{s.lambda3_nm}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {entry.note && <div className="disclaimer">{entry.note}</div>}
        </div>
      ))}

      {coating && (
        <div>
          <h3>{t('caseDetail.physics.coatingTitle')}</h3>
          {coating.summary && <p className="result-summary">{coating.summary}</p>}
        </div>
      )}

      {/* physics.note is the instruction addressed to the LLM, not to the
          reader — the block title already states these values are computed. */}
    </div>
  );
}
