import {productName} from '../brand';
import { PredictionResult } from './PredictionComparison';
import {PredictionCheckResult} from './PredictionReadingCheck';
import {InputDifferences} from './ModelInputComparison';
import {RecheckResult, SolverReceipt} from './SolverReceipt';
import {JsonDetails} from './DeferredDetails';
import CapabilityBrowser from './CapabilityBrowser';
import ScientificStudies, { ScientificStudyOutput } from './StudyPanels';
import { MeasurementResult } from './MeasurementView';
import { ModeOverlapResult } from './ModeOverlap';
import {BeamComparisonResult} from './BeamComparison';
import { useTabDraftCache } from './useTabDraftCache';
import { MeterResult } from './PowerMeasurements';
import { ArchiveList, FileUpload } from './CaseFiles';
import {canvasModels, editableModels, singleCanvasCandidate} from './agentCanvasModels';
import StudyModelReceipt from './StudyModelReceipt';
import {StudyUsageResults} from './StudyUsage';
import {RefinementExecution} from './StudyTolerance';
import {adjustmentRoles, adjustmentView} from './studyAdjustmentRoles';
import React, { useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import { workbenchApi } from '../api/workbench';
import {deferredPanel} from './deferredPanel';
import DesignBasis from './DesignBasis';
import OpticalProject from './OpticalProject';
import ScientificPlot from './ScientificPlot';
import { PredictedWidths } from './BeamFitCheck';
import OpticalFieldView from './OpticalFieldView';
import OpticalScanView, { opticalParameterLabels } from './OpticalScanView';
import AgentText from './AgentText';
import {AgentRunProfile, AgentSettingsLauncher} from './AgentSettings';
import TaskHandoff, {HandoffMessage, ResumeTask} from './TaskHandoff';
import { appendDraft, distinctObjects, objectKey, objectLabel } from './draftComposer';
import {mergeLatestMessages, prependMessages} from './conversationHistory';
import { StudyResult } from './BeamStudy';
import {downloadJSON as saveJSON} from './downloadFile';
import { PowerResult } from './PathPower';
import './OpticalAnalysis.css';
import './ExperimentWorkbench.css';
import './BenchConversation.css';

const DesignWorkspace = deferredPanel(() => import('./DesignWorkspace'), '历史专用设计');

const states = { pending: '排队中', running: '正在工作', completed: '本轮已完成', partial: '已有部分结果', needs_input: '需要你补充', unsupported: '当前工具有边界', failed: '运行中断', cancelled: '已停止', unverified: '运行归属待确认' };
const receiptStates = {completed:'已执行', partial:'部分结果', failed:'失败', needs_input:'需补输入', cancelled:'已停止'};

const ConversationText = React.memo(function ConversationText({content, canExpand}) {
  const [expanded,setExpanded]=useState(false);
  const long=canExpand && content.length>500;
  return <><AgentText>{long && !expanded ? content.slice(0,500)+'…' : content}</AgentText>
    {long && <button type="button" aria-expanded={expanded} onClick={()=>setExpanded(!expanded)}>
      {expanded ? '收起全文' : '展开全文'}</button>}</>;
});

function messageAuthor(message, current) {
  if (message.role === 'user') return '你';
  const metadata = message.metadata || {};
  let origin = metadata.result_origin;
  if (!origin && current?.id === metadata.workbench_run_id && current?.final_content_id &&
      current.final_content_id === metadata.generated_content_id && current.result?.summary === message.content) {
    origin = current.result_origin;
  }
  if (origin === 'system_receipt') return '系统回执';
  if (origin === 'model' || origin === 'recorded_model_finish') return 'Agent';
  if (metadata.workbench_run_id) return '历史任务记录';
  return metadata.design_run_id ? '设计任务记录' : 'Agent';
}

const bullets = (items, className) => !!items?.length && <ul className={className}>{items.map((text, i) => <li key={i}>{text}</li>)}</ul>;

const projectCallNames = new Set(['read_optical_project', 'save_optical_project', 'import_design_project',
  'replace_component', 'replace_crystal', 'place_component_model', 'adopt_optical_snapshot','bind_measured_launch']);
function projectResult(call) {
  if (!projectCallNames.has(call.name)) return null;
  return call.name === 'read_optical_project' ? call.output?.project_revision : call.output;
}

function candidateComparison(output) {
  return output?.id && output.baseline_id && Array.isArray(output.recipe?.axes) && Array.isArray(output.rows);
}

function resultReferences(call) {
  const output=call.output || {};
  const revision=projectResult(call)?.revision_id ?? output.project_revision_id ?? output.revision_id ?? output.target?.revision_id;
  const reference=output.reference_revision_id ?? output.reference?.revision_id;
  const comparison = candidateComparison(output) ? output.id : ['inspect_study_tolerance','inspect_adjustment_policy'].includes(call.name) && output.search_id;
  const artifactIds = output.id && output.reading_id ? [output.id,output.prediction_id,output.reading_id].filter(Boolean)
    : comparison ? [comparison] : reference ? [reference] : [];
  return {project_revision_id:revision ?? null, artifact_ids:artifactIds, tool_call_ids:[call.id]};
}

function numericalView(call,caseId,onUseFit,onAsk) {
  if(call.name==='inspect_adjustment_policy' && Array.isArray(call.output?.designs)) {
    const {search_id,designs,roles,axes,distribution}=call.output;
    const passed=key=>designs.filter(d=>d[key].state==='pass').length;
    return <><p>比较 #{search_id} 的 {designs.length} 个方案中，采用固定设定时有 {passed('fixed')} 个方案在所有采样工况都达标。
      {roles.includes('observed') && roles.includes('control') && <>若能在调节前辨别所选工况，按工况调节时有 {passed('adaptive')} 个方案达标。</>}</p>
      {distribution && <p>另按{distribution.basis==='observed'?'观测频次':'假设权重'}评价 {distribution.scenarios.length} 个联合工况。依据为{distribution.source}。打开此比较可逐方案核对达标权重与缺失读数。</p>}
      {axes?.length>0 && <p>本次装调假设 · {axes.map((axis,i)=>adjustmentRoles[roles[i]]+'：'+axis.paths.map(p=>p.join(' › ')).join(' ＋ ')).join('，')}</p>}</>;
  }
  if(call.name==='inspect_study_tolerance' && call.output?.nominal) {
    const {search_id,nominal,scope}=call.output;
    const state={pass:'达标',outside:'超限',unknown:'依据不足'}[nominal.state];
    return <p>比较 #{search_id} · 名义取值 {nominal.position}。{scope==='all'?'核对所有已采样工况':'固定其他参数组'}时，名义点{state}。</p>;
  }
  if(call.name==='recheck_optical_project' && call.output?.difference_counts) return <RecheckResult result={call.output}/>;
  if(call.name==='design_linear_laser' && call.output?.engineering_design) {
    const design = call.output.engineering_design;
    return <>{!design.evidence_assessment && <p>{design.summary}</p>}
      <DesignBasis key={call.id} design={design} caseId={caseId} contentId={call.output.content_id}/></>;
  }
  if(call.name==='compare_project_inputs' && Array.isArray(call.output?.items)) return <InputDifferences result={call.output}/>;
  if(candidateComparison(call.output)){
    const report=call.output;
    const incomplete=report.incomplete ?? report.unavailable;
    return <p>{report.source_search_id ? '沿用比较 #'+report.source_search_id+' 的 '+report.rows.length+' 个候选' : '已比较 '+report.rows.length+' 个组合'}，
      {report.feasible} 个符合约束{report.recipe.measures.some(m=>m.direction) && <>，其中 {report.preferred} 个位于本次取值的前沿</>}。
      {incomplete>0 && <>有 {incomplete} 个候选缺少部分评价依据。</>}
      {report.refinement && <><br/><RefinementExecution report={report}/></>}</p>;
  }
  if (call.output?.study_record) {
    const {study_record, ...output} = call.output;
    return <><ScientificStudyOutput caseId={caseId} definition={study_record} output={output}/>
      {study_record.execution==='reused' && <p>此次调用沿用了已存计算 #{study_record.id}。</p>}
      <SolverReceipt saved={study_record.solver_receipt}/></>;
  }
  if(call.name==='inspect_measurement_table' && call.output?.selection) return <MeasurementResult item={call.output}/>;
  if(call.name==='compare_beam_predictions' && call.output?.points)return <PredictionResult item={call.output} caseId={caseId} onAsk={onAsk}/>;
  if(call.name==='check_prediction_reading' && Array.isArray(call.output?.outcomes))return <PredictionCheckResult item={call.output} caseId={caseId}/>;
  if(call.name==='analyze_mode_overlap' && call.output?.intensity && call.output.coherent)return <ModeOverlapResult result={call.output}/>;
  if(call.name==='compare_beam_reading' && call.output?.target_prediction)return <BeamComparisonResult item={call.output} caseId={caseId}/>;
  if(call.name==='compare_measured_power' && call.output?.points)return <MeterResult record={call.output}/>;
  if(call.name==='read_power_measurements' && call.output?.result?.points)return <MeterResult record={call.output.result}/>;
  if(call.name==='analyze_path_power' && call.output?.power_budgets) return <div>
    {call.output.unresolved_planes?.map(p=><p key={p.uid}>{p.name} · {p.message}</p>)}
    {call.output.power_budgets.map(b=><PowerResult key={b.path_uid} result={b}/>)}</div>;
  if (call.name === 'study_measured_beam' && call.output) return <StudyResult item={call.output} caseId={caseId} onUseFit={onUseFit}/>;
  if (call.name === 'read_beam_studies' && call.output?.result?.predictions) return <PredictedWidths result={call.output.result}/>;
  if (call.name === 'read_beam_studies' && (call.output?.result?.fit || call.output?.result?.check || call.output?.result?.repeatability)) return <StudyResult item={call.output.result} caseId={caseId} onUseFit={onUseFit}/>;
  if (call.name === 'analyze_observation_planes' && Array.isArray(call.output?.planes))
    return <ScientificStudyOutput definition={{name:call.name,provider:productName,version:'1'}} output={call.output}/>;
  const project = projectResult(call);
  const output=project || call.output;
  if (output?.analysis?.planes || output?.analysis?.trace || output?.maps || output?.planes ||
      (call.name==='scan_optical_system' && output?.points) ||
      (['fit_beam_caustic','fit_power_curve'].includes(call.name) && output?.data))
    return <OpticalResult call={project ? {...call, output:project} : call}/>;
  return null;
}

function OpticalResult({ call }) {
  const plot = useRef(null);
  const [plotError, setPlotError] = useState('');
  const output = call.output?.analysis?.planes ? call.output.analysis : call.output || {};
  const chart = useMemo(() => {
    if (call.name === 'fit_beam_caustic' && output.data) {
      const points=output.data;
      const traces=[{type:'scatter',mode:'markers',x:points.z_mm,y:points.radius_mm,name:'测量数据'}];
      if (points.curve_z_mm && points.curve_radius_mm) traces.push({type:'scatter',mode:'lines',x:points.curve_z_mm,y:points.curve_radius_mm,name:'焦散拟合'});
      else if (points.fit_radius_mm) {
        const fitted=points.z_mm.map((z,i)=>[z,points.fit_radius_mm[i]]).sort((a,b)=>a[0]-b[0]);
        traces.push({type:'scatter',mode:'lines',x:fitted.map(p=>p[0]),y:fitted.map(p=>p[1]),name:'焦散拟合'});
      }
      return {x:'测量位置 / mm',y:'光束半径 / mm',data:traces};
    }
    if (output.analysis?.trace) return { x: '沿光路物理位置 / mm', y: '1/e² 光束半径 / mm', data: [
      { x: output.analysis.trace.points.map(p => p.z_mm), y: output.analysis.trace.points.map(p => p.w_mm), type: 'scatter', mode: 'lines', name: output.analysis.mode === 'pump' ? '泵浦等效场景' : '腔模 T=S', line: { color: '#80d6c5', width: 2 } },
      { x: output.analysis.trace.waists.map(p => p.z_mm), y: output.analysis.trace.waists.map(p => p.w_mm), type: 'scatter', mode: 'markers', name: '解析束腰', marker: { color: '#d5aa73', size: 8 } },
    ] };
    if (call.name === 'fit_power_curve' && output.data) return { x: '泵浦功率 / W', y: '输出功率 / W', data: [{ x: output.data.pump_w, y: output.data.output_w, type: 'scatter', mode: 'markers', name: '输入测量', marker: { color: '#80d6c5', size: 8 } }, ...(output.fit_line ? [{ x: output.data.pump_w, y: output.data.pump_w.map(p => Math.max(0, output.fit_line.a * p + output.fit_line.b)), type: 'scatter', mode: 'lines', name: '分段拟合', line: { color: '#d5aa73' } }] : [])] };
    return null;
  }, [output, call.name]);
  const layout = useMemo(() => ({ autosize: true, height: 370, margin: { l: 75, r: 25, t: 25, b: 85 }, paper_bgcolor: '#131d2a', plot_bgcolor: '#131d2a', font: { color: '#d0deeb', size: 12 }, xaxis: { title: { text: chart?.x.length > 52 ? chart.x.slice(0, 42) + '… · ' + (opticalParameterLabels[output.parameter] || output.parameter) : chart?.x }, gridcolor: '#ffffff0d', zerolinecolor: '#ffffff20' }, yaxis: { title: { text: chart?.y }, gridcolor: '#ffffff0d', zerolinecolor: '#ffffff20' }, legend: { orientation: 'h', x: 0, y: 1.14 }, hovermode: 'x unified',
    shapes: chart?.y.startsWith('往返矩阵') ? [-1, 1].map(value => ({ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: value, y1: value, line: { color: '#d1ad75', width: 1, dash: 'dot' } })) : [],
  }), [chart, output.parameter]);
  if (output.planes) return <>
    {output.network && <ScientificStudyOutput definition={{table:['ports',{name:'元件',port:'端口',incoming_w:'入射 / W',outgoing_w:'出射 / W'}]}} output={output.network}/>}
    {output.planes.tangential && <OpticalFieldView analysis={output} system={call.output?.project?.system ?? output.system ?? call.input?.layout?.system ?? call.input?.system} parts={call.output?.project?.parts} componentContext={call.output?.project?.component_context} scopeWarnings={call.output?.project?.component_context?.scope_warnings} revisionLabel={'工具 #' + call.id} />}</>;
  if (output.maps || (call.name==='scan_optical_system' && output.points)) return <OpticalScanView scan={output} system={call.input?.system} recordLabel={'工具 #' + call.id} />;
  if (!chart || !chart.data.some(d => d.y?.some(v => v != null))) return null;
  return <section className="wb-numerical"><div className="wb-row"><h3>{call.label || call.name} <small>工具 #{call.id}</small></h3><button className="btn btn-secondary" onClick={() => plot.current?.exportImage('svg').catch(e => setPlotError(e.message))}>导出 SVG</button></div>
    {plotError && <p role="alert" className="wb-error">{plotError}</p>}
    {output.stable_both_planes !== undefined && <p className="wb-engine">{output.stable_both_planes ? 'T / S 双平面稳定' : '存在失稳或临界平面，无受限基模'}</p>}
    <ScientificPlot ref={plot} data={chart.data} layout={layout} filename={`task-tool-${call.id}`} />
    {chart.y.startsWith('往返矩阵') && <p className="field-caption">虚线为m=±1稳定边界；|m|&lt;1为几何稳定。扫描曲线不代表输出功率或效率。</p>}
    {output.analysis?.trace?.waists?.map((p, i) => <p className="wb-engine" key={i}>解析束腰 z={p.z_mm.toFixed(5)} mm · w={(p.w_mm * 1000).toFixed(3)} μm · 瑞利长度 {p.z_R_mm.toFixed(5)} mm</p>)}
    {call.name === 'fit_power_curve' && <div className="wb-metrics"><span>阈值 <b>{output.threshold_pump} W</b></span><span>斜率效率 <b>{output.slope_efficiency_pct}%</b></span><span>拟合 R² <b>{output.r_squared}</b></span></div>}
    {bullets(output.warnings)}<small className="wb-engine">{output.engine || (typeof output.scope === 'string' ? output.scope : '图表直接读取工具输出')}{output.engine && ' · 近轴模型，元件坐标按传播方向展开'}</small>
    {output.solver_receipt && <SolverReceipt saved={output.solver_receipt} left="执行时"/>}
  </section>;
}

function StudyLink({call,onOpenStudy}) {
  if(call.name==='find_study_usage' && Array.isArray(call.output?.items) && onOpenStudy) {
    const page=call.output, first=(call.input?.offset || 0)+1;
    return <><StudyUsageResults data={page} onOpen={onOpenStudy}/>
      {(first>1 || page.next_offset!=null) && <small>{page.items.length
        ? '本次查询返回第 '+first+' 至 '+(first+page.items.length-1)+' 条。' : '本次查询的这一页没有记录。'}</small>}</>;
  }
  const view=call.output?.workbench_view;
  if(view?.run_id && onOpenStudy) return <button type="button" className="btn btn-secondary"
    onClick={()=>onOpenStudy(view.run_id,null,view.comparison_id ?? null,null,view.selection ?? null)}>{view.label}</button>;
  if(call.name==='inspect_adjustment_policy' && Array.isArray(call.output?.roles) && onOpenStudy) {
    const {baseline_id,search_id}=call.output;
    return <button type="button" className="btn btn-secondary"
      onClick={()=>onOpenStudy(baseline_id,null,search_id,null,adjustmentView(call.output))}>查看比较 #{search_id} 的调节策略</button>;
  }
  if(call.name==='inspect_study_tolerance' && call.output?.nominal && onOpenStudy) {
    const {baseline_id,search_id,row_index,axis_index,coordinate_index,scope}=call.output;
    return <button type="button" className="btn btn-secondary"
      onClick={()=>onOpenStudy(baseline_id,null,search_id,null,{row_index,axis_index,coordinate_index,scope})}>查看比较 #{search_id} 的装调余量</button>;
  }
  if(candidateComparison(call.output) && onOpenStudy)return <button type="button" className="btn btn-secondary"
    onClick={()=>onOpenStudy(call.output.baseline_id,null,call.output.id)}>查看候选比较 #{call.output.id}</button>;
  const id=call.output?.study_record?.id;
  return id && onOpenStudy ? <button type="button" className="btn btn-secondary" onClick={()=>onOpenStudy(id)}>查看计算记录 #{id} 与输入来源</button> : null;
}

function ToolTrace({ caseId, call, onAsk, highlighted, onOpenStudy, onUseFit }) {
  const [open, setOpen] = useState(false);
  const node = useRef(null);
  const problem = call.error || (['failed','needs_input'].includes(call.status) && call.output?.message);
  useEffect(() => { if (highlighted) { node.current.open = true; node.current.scrollIntoView({ block: 'center' }); node.current.querySelector('summary')?.focus(); } }, [highlighted]);
  return <details ref={node} onToggle={event => { if (event.currentTarget === event.target) setOpen(event.currentTarget.open); }} className={`wb-tool ${call.status}`}><summary><span>{call.status === 'running' ? '◌' : call.status === 'failed' ? '!' : call.status === 'partial' || call.status === 'needs_input' ? '◐' : '✓'}</span><strong>{call.output?.study_record?.title || call.label || call.name}</strong><small>#{call.id} · {states[call.status] || call.status}{call.latency_ms != null ? ` · ${(call.latency_ms / 1000).toFixed(1)} s` : ''}</small></summary>
    {open && <><p>{call.input?.purpose}</p>{typeof problem === 'string' && problem && <p className="wb-error">{problem}</p>}<StudyLink call={call} onOpenStudy={onOpenStudy}/>{numericalView(call,caseId,onUseFit,onAsk)}
    <div className="wb-row"><small>输入、输出及来源均保留，可复核</small>{onAsk && <button className="btn btn-secondary" onClick={() => onAsk('关于这条执行记录：\n', resultReferences(call))}>引用并提问</button>}<button className="btn btn-secondary" onClick={() => saveJSON(`tool-${call.id}.json`, call)}>下载计算记录</button></div>
    <JsonDetails title="输入参数" value={call.input}/><JsonDetails title="完整工具输出" value={call.output}/>
  </>}</details>;
}

function NumericalResults({ caseId, calls, onAsk, onOpenStudy, onUseFit }) {
  const [selected, setSelected] = useState(null);
  const ready = calls.map(call => ({call, view:numericalView(call,caseId,onUseFit,onAsk)})).filter(item => item.view);
  const item = ready.find(item => item.call.id === selected) || ready.at(-1);
  if (!item) return <p className="wb-muted">本轮尚无可展示的计算结果。</p>;
  const current = item.call;
  const record = projectResult(current);
  const scene = record ? `${record.model_kind === 'draft' ? '试算快照' : '已保存工程'} #${record.revision_id} 的几何模型`
    : current.output?.study_record ? `计算记录 #${current.output.study_record.id} · ${current.output.study_record.title}`
    : current.name === 'inspect_measurement_table' ? '实验原件中的读数' : current.label || current.name;
  return <section className="wb-plots"><label>选择本轮计算结果<select aria-label="本轮计算曲线" value={current.id} onChange={event => setSelected(Number(event.target.value))}>
    {ready.map(({call}) => <option key={call.id} value={call.id}>{call.output?.study_record?.title || call.label || call.name} · #{call.id}{call.input?.purpose ? ' · ' + call.input.purpose.slice(0, 44) : ''}</option>)}
    </select></label><p className="field-note"><strong>当前显示：{scene}</strong><br />{current.input?.purpose || current.output?.change_note || '结果对应此条工具记录。'}</p>
    {onAsk && <button className="btn btn-secondary" onClick={() => onAsk('关于这条计算结果：\n', resultReferences(current))}>引用这条结果提问</button>}
    <StudyLink call={current} onOpenStudy={onOpenStudy}/><React.Fragment key={current.id}>{item.view}</React.Fragment></section>;
}

const emptyReferences = () => ({ project_revision_id: null, element_uids: [], project_objects: [], inventory_ids: [], attachment_ids: [], tool_call_ids: [], artifact_ids: [] });
const uniqueReferences = values => [...new Set(values || [])];
const referencesFor = (refs = {}) => ({ ...emptyReferences(), ...refs,
  project_objects: distinctObjects(refs.project_objects),
  element_uids: uniqueReferences(refs.element_uids), inventory_ids: uniqueReferences(refs.inventory_ids),
  attachment_ids: uniqueReferences(refs.attachment_ids), tool_call_ids: uniqueReferences(refs.tool_call_ids), artifact_ids: uniqueReferences(refs.artifact_ids) });
const systemModes = { single_pass: '单程传播', standing_wave: '驻波腔', ring: '环形腔', port_network:'端口光路' };
const factKinds = { user_stated: '用户提供', document: '资料读取', calculated: '工具计算', hypothesis: '待验证假设' };

function EvidenceView({ caseData, workspace, attachments, filesKnown, factsRun, latestRun, onAsk, selected, setSelected, disabled }) {
  const revision = workspace.project_revision_id;
  const facts = workspace.facts?.length ? workspace.facts : factsRun?.result?.facts || [];
  const questions = workspace.open_questions?.length ? workspace.open_questions : factsRun?.result?.questions || [];
  const parts = workspace.parts || [];
  const unknowns = [...new Set(parts.flatMap(part => part.unknowns || []))];
  const askRefs = { project_revision_id: factsRun?.result_model?.revision_id ?? factsRun?.model_basis?.revision_id ?? null,
    artifact_ids: factsRun?.final_content_id ? [factsRun.final_content_id] : [] };
  return <section className="wb-evidence-view" aria-label="实验依据与约束">
    <div className="wb-evidence-intro"><h3>任务依据与当前约束</h3><p>资料、用户确认、计算值和假设分别保留。引用资料或选择库存，不代表已经测量确认或安装到光路。</p></div>
    <section className="wb-evidence-card"><h4>任务目标</h4><strong>{caseData.title}</strong><p>{workspace.latest_goal || caseData.description || '直接在对话中说明希望解决的问题。'}</p>{workspace.latest_goal && caseData.description && <details><summary>查看案例原始描述</summary><p>{caseData.description}</p></details>}<button className="btn btn-secondary" onClick={() => onAsk('关于当前任务的目标与约束，我补充：\n', {project_revision_id:latestRun?.model_basis?.revision_id})}>补充目标或约束</button></section>
    <section className="wb-evidence-card"><h4>主工程与实物依据</h4>{revision ? <><p><strong>主工程 #{revision} · {workspace.project_title}</strong><br />{systemModes[workspace.mode] || workspace.mode} · {workspace.wavelength_nm == null ? '波长待定' : workspace.wavelength_nm+' nm'} · {(workspace.elements || []).length + (workspace.network_component_count || 0)} 个模型元素</p>
      <div className="wb-evidence-parts">{parts.map(part => { const element = workspace.elements?.[part.element_index]; return <details key={element?.uid || part.element_index}><summary>{element?.name || `元素 ${part.element_index + 1}`} · {part.inventory_id ? `库存 #${part.inventory_id}` : '未绑定库存'}{part.unknowns?.length ? ` · ${part.unknowns.length} 项待核实` : ''}</summary><p>{part.source}</p>{bullets(part.unknowns)}<button className="btn btn-secondary" onClick={() => onAsk(`关于「${element?.name || '这个工程对象'}」的资料与约束：\n`, { project_revision_id: revision, element_uids: element?.uid ? [element.uid] : [], inventory_ids: part.inventory_id ? [part.inventory_id] : [] })}>引用这个对象</button></details>; })}</div>
      {!!workspace.assumptions?.length && <details><summary>模型假设 · {workspace.assumptions.length} 项</summary>{bullets(workspace.assumptions)}</details>}{!!unknowns.length && <details><summary>待核实条件 · {unknowns.length} 项</summary>{bullets(unknowns)}</details>}{!!workspace.scope_warnings?.length && <details><summary>计算范围与边界</summary>{bullets(workspace.scope_warnings)}</details>}
      <button className="btn btn-secondary" onClick={() => onAsk('关于当前工程的已知条件与待核实项：\n', { project_revision_id: revision })}>引用当前工程</button></> : <p>尚未保存工程。已有的图片、测量数据和元件信息都可先交给 Agent，再决定如何建模。</p>}</section>
    <section className="wb-evidence-card"><h4>资料与测量文件{filesKnown && ' · ' + attachments.length}</h4><p>勾选后随下一条消息引用；也可从对话区继续上传图片或数据。</p>{attachments.length ? <ArchiveList files={attachments} label="选择工程原件">{attachment => <div className="wb-evidence-file" key={attachment.id}><label><input type="checkbox" checked={selected.includes(attachment.id)} disabled={disabled || (!selected.includes(attachment.id) && selected.length >= 8)} onChange={() => setSelected(previous => previous.includes(attachment.id) ? previous.filter(id => id !== attachment.id) : [...previous, attachment.id])} />{attachment.relative_path || attachment.filename}<small>附件 #{attachment.id}</small></label><button className="btn btn-secondary" disabled={disabled || (!selected.includes(attachment.id) && selected.length >= 8)} onClick={() => onAsk('关于这份资料：\n', { attachment_ids: [attachment.id] })}>引用并提问</button></div>}</ArchiveList> : <p>{filesKnown ? '还没有附件。' : '资料清单尚未读入。'}</p>}</section>
    <section className="wb-evidence-card"><h4>最近一轮整理的事实与来源{factsRun && ` · 任务 #${factsRun.id}`}</h4>{facts.length ? facts.map((fact, index) => <article className="wb-fact" key={index}><small>{factKinds[fact.kind] || fact.kind} · {fact.source}</small><p>{fact.statement}</p></article>) : <p>Agent 完成分析后，将在这里保留事实、来源和明确采用的假设。</p>}{facts.length > 0 && <button className="btn btn-secondary" onClick={() => onAsk('关于这些事实和来源，我需要核实或更正：\n', askRefs)}>引用并核实这些依据</button>}</section>
    {!!questions.length && <section className="wb-evidence-card"><h4>最近报告中的待补信息</h4>{questions.map((question, index) => <article className="wb-fact" key={index}><strong>{question.question}</strong><p>{question.why}</p><p>获取方式：{question.how_to_obtain}</p><small>{question.expected_format}</small><button className="btn btn-secondary" onClick={() => onAsk(`关于「${question.question}」：\n`, askRefs)}>补充这项信息</button></article>)}</section>}
  </section>;
}

export default function ExperimentWorkbench({ caseData, caseFiles = [], fileListReady, selected, setSelected, refreshFiles, transfers, onArtifactsChanged, openFiles, composerActions }) {
  const id = caseData.id;
  const composerCache = useTabDraftCache(id, 'message');
  const [composerLoaded, setComposerLoaded] = useState(false);
  const [restoredMessage, setRestoredMessage] = useState('');
  const [state, setState] = useState(null);
  const [message, setMessage] = useState('');
  const [contextRefs, setContextRefs] = useState(emptyReferences);
  const [sending, setSending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [composerError, setComposerError] = useState('');
  const [editorError, setEditorError] = useState('');
  const [tab, setTab] = useState('project');
  const [navigation, setNavigation] = useState(null);
  const deliveredInputFocus = useRef(null), resultBody = useRef(null);
  const openInputs = () => { setTab('analysis'); setNavigation({destination:'inputs'}); };
  const focusStudyEntry = useCallback(node => {
    if (!node || navigation?.destination !== 'inputs' || deliveredInputFocus.current === navigation) return;
    deliveredInputFocus.current = navigation;
    node.focus({preventScroll:true});
    node.scrollIntoView({block:'start'});
  }, [navigation]);
  const [studyRunId,setStudyRunId] = useState(null);
  const [savedStudyId,setSavedStudyId] = useState(null);
  const [studyFocus,setStudyFocus] = useState(null);
  const [studyEntry,setStudyEntry]=useState({source:null,sequence:0});
  const clearStudySource=()=>setStudyEntry(previous=>({...previous,source:null}));
  const useStudySource=source=>{
    setStudyEntry(previous=>({source,sequence:previous.sequence+1}));
    setStudyRunId(null);setStudyFocus(null);openInputs();
  };
  const openStudy=(recordId,replay,searchId,returnTo,searchView)=>{clearStudySource();setStudyRunId(recordId);setStudyFocus({id:recordId,replay,searchId,returnTo,searchView});setTab('analysis');};
  const [tableSources,setTableSources] = useState([]);
  const [benchHost, setBenchHost] = useState(null);
  const [conversationHost,setConversationHost] = useState(null);
  const [dockRequested,setDockRequested] = useState(false);
  const docked = dockRequested && conversationHost != null;
  const conversationTrigger = useRef(null);
  const [search] = useSearchParams();
  const {hash} = useLocation();
  const archiveRevision = search.get('design_revision');
  const archiveRow = search.get('design_row');
  const archiveTarget = search.get('design_target');
  const openingModel = search.get('model_revision');
  useEffect(() => {
    if (openingModel == null) return;
    const bench=benchHost?.querySelector('.optical-bench');
    bench?.scrollIntoView({block:'start'});
    bench?.focus({preventScroll:true});
  }, [openingModel, benchHost]);
  const [advancedOpen, setAdvancedOpen] = useState(archiveRevision != null);
  const [visitedDesign, setVisitedDesign] = useState(false);
  const [directoryOpen,setDirectoryOpen]=useState(false);
  const [evidenceTarget, setEvidenceTarget] = useState(null);
  const [runId, setRunId] = useState(null);
  const [runDetail, setRunDetail] = useState(null);
  const [detailFailure, setDetailFailure] = useState(null);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const input = useRef(null), scroll = useRef(null), alive = useRef(true);
  const followMessages = useRef(true), readingAnchor = useRef(null);
  const [historyPending,setHistoryPending] = useState(false), [historyError,setHistoryError] = useState('');
  const readSequence = useRef(0);
  const [modelView, setModelView] = useState(null);
  const fileActions = useRef(null);
  const useFit = fitId => fileActions.current?.useFit(fitId);
  const viewRevision = modelView?.sourceId;
  const lastSnapshot = useRef(null);
  const [workbenchChanges, setWorkbenchChanges] = useState(0);
  const receiveModel = useCallback(view => {
    setModelView(view);
    setEditorError('');
  }, []);
  const sourceLabel = (modelView?.trial ? modelView.variantName || '试算方案' : '工程') + ' #' + viewRevision;
  const viewedProject = modelView?.project;
  const networkOnly = !viewedProject?.system?.elements?.length && viewedProject?.network?.components?.length > 0;
  const viewedMode = networkOnly ? 'port_network' : viewedProject?.system.mode;
  const viewedWavelength = networkOnly ? viewedProject.network.wavelength_nm ?? viewedProject.system.wavelength_nm : viewedProject?.system.wavelength_nm;
  const canvasLabel = [viewRevision && sourceLabel, modelView?.dirty && '有未保存修改'].filter(Boolean).join(' · ') ||
    (modelView ? '空白光路' : '读取光路…');
  const latest = state?.runs?.[0];
  const availableModels = canvasModels(state);
  const agentActive = latest?.active || ['pending', 'running'].includes(latest?.status);
  const messageBasis = agentActive ? '下一条消息使用' : '本轮使用';
  const viewLabel = modelView?.rawPending ? '完整模型中的文本待应用' :
    modelView?.positionPending ? '发送时将核对输入的位置，并随问题附上画布草稿' :
    modelView?.dirty ? '随问题附上画布草稿' + (viewRevision ? ' · 源自' + sourceLabel : '') :
    viewRevision ? messageBasis + sourceLabel : modelView ? '当前画布尚未建立光路' : '正在读取光路';
  const needsLatestTools = ['analysis','result','tools'].includes(tab) && (!runId || runId===latest?.id);
  const fetchLatestTools = useRef(false);
  useLayoutEffect(()=>{ fetchLatestTools.current=needsLatestTools; },[needsLatestTools]);
  const workspace = state?.workspace || {};
  const busy = sending || agentActive;
  const unknownOwner = latest?.execution_owner_state === 'unverified';
  const lastStep=latest?.steps?.at(-1);
  const current = runId == null || runId === latest?.id ? latest :
    runDetail?.id === runId ? runDetail : state?.runs?.find(run => run.id === runId);
  const historicalError = detailFailure?.id === runId ? detailFailure.message : null;
  const detailError = runId != null && runId !== latest?.id ? historicalError : error;
  const showRun = async selectedId => {
    if (docked && document.fullscreenElement?.contains(conversationHost)) await document.exitFullscreen();
    setDockRequested(false);
    setRunId(selectedId); setTab('result'); setNavigation({destination:'result'});
  };
  const retryDetails = () => {
    setDetailFailure(null); setError(''); setDetailAttempt(n=>n+1);
    if (tab === 'result') setNavigation({destination:'result'});
  };
  useLayoutEffect(() => {
    const target = navigation?.destination === 'result' ? resultBody.current : null;
    if (target) {
      target.focus({preventScroll:true});
      target.scrollIntoView({block:'start'});
    }
  }, [navigation]);
  const result = current?.result;
  function canvasLinks(models) {
    return models.map(model => {
      const rid = model.revision_id;
      const same = viewRevision === rid;
      const separate = !same && (modelView?.dirty || modelView?.rawPending || modelView?.positionPending);
      const label = model.variant_name || (model.kind === 'draft' ? '方案' : '光路');
      if (same) return <button type="button" key={rid} onClick={() => returnToBench('canvas')}>查看当前画布 · {label} #{rid}</button>;
      return <Link key={rid} to={`/cases/${id}?model_revision=${rid}`}
        target={separate ? '_blank' : undefined} rel={separate ? 'noopener' : undefined}
        onClick={()=>{if (!separate) { setDockRequested(false); setTab('project'); }}}>
        {separate ? '在新标签页编辑' : '在画布中编辑'} · {label} #{rid}
      </Link>;
    });
  }
  const systemReceipt = current?.result_origin === 'system_receipt';
  const handoff = result?.failure_handoff;
  const interrupted = systemReceipt || ['failed','cancelled'].includes(current?.status);
  const shownStatus = current?.execution_owner_state === 'unverified' ? 'unverified' : interrupted ? current.status : result?.status;
  const tools = current?.tools?.filter(call => call.name !== 'finish') || [];
  const factsRun = state?.runs?.find(run => run.result);
  const workbenchFiles = state?.attachments ?? workspace.attachments;
  const filesKnown = fileListReady || workbenchFiles != null;
  const attachments = useMemo(() => {
    if (fileListReady) return caseFiles;
    const merged = new Map((workbenchFiles || []).map(file => [file.id, file]));
    for (const file of caseFiles) merged.set(file.id, file);
    return [...merged.values()];
  }, [caseFiles, fileListReady, workbenchFiles]);
  const initializedComposer = useRef(false);
  const readWorkbench = data => {
    const headId=data.workspace.project_revision_id, resultId=data.runs[0]?.final_content_id;
    const previous=lastSnapshot.current;
    // The first load already fetches the optical record in parallel. A later
    // completed study can add scans and variants without replacing that head.
    if (previous?.caseId===id && previous.headId===headId && previous.resultId!==resultId) setWorkbenchChanges(n=>n+1);
    if (previous?.caseId===id && resultId && previous.resultId!==resultId) {
      onArtifactsChanged?.().catch(cause=>setError(cause.message));
    }
    const archiveIndex=data.archive_index;
    if (previous?.archiveIndex && archiveIndex && (previous.archiveIndex.count!==archiveIndex.count ||
        previous.archiveIndex.newest_id!==archiveIndex.newest_id)) refreshFiles?.();
    lastSnapshot.current={caseId:id,headId,resultId,archiveIndex};
    if (!initializedComposer.current) {
      const record = composerCache.read(data.draft_scope);
      if (record) {
        const accessible = new Set(data.attachments.map(a => a.id));
        const selectedIds = record.selected.filter(i => accessible.has(i));
        setMessage(text => text && text !== record.message ? [record.message, text].filter(Boolean).join('\n\n') : record.message);
        setContextRefs(referencesFor(record.refs));
        setSelected(previous => uniqueReferences([...previous, ...selectedIds]).slice(0, 8));
        setRestoredMessage(selectedIds.length === record.selected.length ? '已恢复本标签页的待发问题及引用。' :
          '已恢复待发问题。原先引用的部分附件已移除，请在发送前核对。');
      }
      initializedComposer.current = true; setComposerLoaded(true);
    }
    setState(previous=>mergeLatestMessages(previous,
      data.attachments || !previous?.attachments ? data : {...data,attachments:previous.attachments}));
  };
  const loadEarlier = async () => {
    const cursor = state?.message_page?.next_before_id;
    if (!cursor || historyPending) return;
    setHistoryPending(true);setHistoryError('');
    try {
      const page = await workbenchApi.earlierMessages(id,cursor);
      if (!alive.current) return;
      const first = scroll.current?.querySelector('[data-message-id]');
      if (first) readingAnchor.current = {node:first, top:first.getBoundingClientRect().top};
      followMessages.current = false;
      setState(previous=>prependMessages(previous,cursor,page));
    } catch (cause) {
      if (alive.current) setHistoryError(cause.message);
    } finally {
      if (alive.current) setHistoryPending(false);
    }
  };
  const refresh = async () => {
    const sequence = ++readSequence.current;
    try {
      const data = await workbenchApi.get(id,fetchLatestTools.current,!initializedComposer.current,20);
      if (alive.current && sequence === readSequence.current) readWorkbench(data);
    } catch (cause) {
      if (alive.current && sequence === readSequence.current) throw cause;
    }
  };
  const projectChanged = () => {
    refresh().catch(cause => setError('工程已保存。对话与任务记录刷新失败。' + cause.message));
    return onArtifactsChanged?.();
  };
  useEffect(() => {
    alive.current = true;
    let active = true;
    Promise.resolve().then(() => active && refresh()).catch(cause => {
      if (alive.current) { setError(cause.message); setComposerLoaded(true); }
    });
    return () => { active = false; alive.current = false; readSequence.current++; };
  }, [id]);
  useEffect(() => {
    if (needsLatestTools && latest && !latest.tools_loaded) refresh().catch(cause => setError(cause.message));
  }, [id, needsLatestTools, latest?.id, latest?.tools_loaded, detailAttempt]);
  useEffect(() => {
    if (!runId || runId === latest?.id) return;
    let active = true;
    workbenchApi.getRun(id, runId).then(data => {
      if (active) { setRunDetail(data); setDetailFailure(null); }
    }).catch(cause => { if (active) setDetailFailure({id:runId,message:cause.message}); });
    return () => { active = false; };
  }, [id, runId, latest?.id, detailAttempt]);
  useEffect(() => {
    if (!busy || unknownOwner) return;
    let active = true;
    let timer;
    const poll = async () => {
      try { await refresh(); }
      catch (cause) { if (active) setError(cause.message); }
      if (active) timer = setTimeout(poll, 2000);
    };
    timer = setTimeout(poll, 2000);
    return () => { active = false; clearTimeout(timer); };
  }, [id, busy, unknownOwner]);
  useLayoutEffect(() => {
    const box = scroll.current, anchor = readingAnchor.current;
    readingAnchor.current = null;
    if (!box) return;
    if (anchor && box.contains(anchor.node)) box.scrollTop += anchor.node.getBoundingClientRect().top - anchor.top;
    else if (followMessages.current) box.scrollTop = box.scrollHeight;
  }, [state?.messages?.[0]?.id,state?.messages?.at(-1)?.id,docked,historyPending]);
  useEffect(() => {
    if (!message.trim()) return;
    const warn = event => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [message]);
  useEffect(() => {
    if (!composerLoaded) return;
    if (message || selected.length || contextRefs.project_revision_id || contextRefs.followCanvas || contextRefs.startEmpty || ['element_uids','project_objects','inventory_ids','tool_call_ids','artifact_ids'].some(k => contextRefs[k]?.length)) {
      composerCache.store({message, selected, refs:contextRefs});
    } else composerCache.clear();
  }, [composerLoaded, message, selected, contextRefs]);
  const seed = (text, refs = {}, fromCanvas = false) => {
    if (fromCanvas) openComposer();
    else {
      setDockRequested(false);
      requestAnimationFrame(() => { input.current?.scrollIntoView({ block: 'center' }); input.current?.focus({preventScroll:true}); });
    }
    const normalized = referencesFor(refs);
    try {
      if(uniqueReferences([...selected,...normalized.attachment_ids]).length>8) throw new Error('本条消息最多引用 8 份文件。请先移除一份引用，再添加这段读数。');
      const prepared = appendDraft(message, text, contextRefs, normalized);
      setMessage(prepared.message); setContextRefs(prepared.refs); setComposerError('');
    } catch (cause) { setComposerError(cause.message); return; }
    if (normalized.attachment_ids.length) setSelected(previous => uniqueReferences([...previous, ...normalized.attachment_ids]).slice(0, 8));
  };
  useImperativeHandle(composerActions,()=>({quote:seed,openStudy,useFit,focusEditor:()=>fileActions.current?.focusEditor(),
    checkFileInputs:()=>{
      if (!fileActions.current) throw Error('工程正在读取，请稍后保存。');
      fileActions.current.checkFileInputs();
    },prepareFileSave:async()=>{
      if (!fileActions.current) throw Error('工程正在读取，请稍后保存。');
      return fileActions.current.prepare();
  },useOriginal:file=>useStudySource({file}),useMeasurement(source){
    clearStudySource();
    setTableSources(old=>[source,...old.filter(item=>item.source.sha256!==source.source.sha256 ||
      JSON.stringify(item.table)!==JSON.stringify(source.table))]);
    openInputs();
  }}));
  const send = async event => {
    event.preventDefault(); if (busy || uploading || !message.trim()) return;
    const token = composerCache.token.current;
    const refs = referencesFor({ ...contextRefs, attachment_ids: selected });
    const startEmpty = refs.startEmpty;
    delete refs.startEmpty;
    delete refs.followCanvas; // A composer binding. The request carries the model actually visible at submission.
    let model = {};
    setError(''); setEditorError(''); setComposerError('');
    if (startEmpty) model = {viewed_empty_project:true};
    else if (!refs.project_revision_id) {
      try {
        if (!fileActions.current) throw Error('工作台正在读取工程，请稍后发送。');
        const {revision, ...input} = fileActions.current.agentModel();
        if (revision) refs.project_revision_id = revision;
        model = input;
      } catch (cause) { setEditorError(cause.message); return; }
    }
    setSending(true); setRunId(null); setTab('result');
    try {
      await workbenchApi.run(id, message.trim(), selected, refs, model);
      composerCache.clear(token);
      if (!alive.current) return;
      followMessages.current = true;
      setRestoredMessage(''); setMessage(''); setSelected([]); setContextRefs(emptyReferences());
      try { await refresh(); } catch (cause) { setError(`任务已提交，请重新连接查看进度，无需重复发送。${cause.message}`); }
    } catch (cause) {
      if ([400, 404, 422].includes(cause.status)) setComposerError(cause.message);
      else setError(cause.message);
    } finally { setSending(false); }
  };
  const referenceTokens = [
    ...(contextRefs.followCanvas ? [{key:'followCanvas', value:true, label:'当前画布'}] : []),
    ...(contextRefs.startEmpty ? [{key:'startEmpty', value:true, label:'空工程起点'}] : []),
    ...(contextRefs.project_revision_id ? [{ key: 'project_revision_id', value: contextRefs.project_revision_id, label: `光路 #${contextRefs.project_revision_id}` }] : []),
    ...contextRefs.element_uids.map((uid, index) => {
      const elements = !contextRefs.project_revision_id || (contextRefs.project_revision_id === viewRevision && !modelView?.dirty)
        ? modelView?.project.system.elements : contextRefs.project_revision_id === workspace.project_revision_id ? workspace.elements : [];
      return {key:'element_uids', value:uid, label:elements?.find(element => element.uid === uid)?.name || `工程对象 ${index + 1}`};
    }),
    ...contextRefs.project_objects.map(object => ({
      key: 'project_objects', value: objectKey(object),
      label: objectLabel(!contextRefs.project_revision_id || (contextRefs.project_revision_id === viewRevision && !modelView?.dirty) ? viewedProject : null, object),
    })),
    ...contextRefs.inventory_ids.map(value => ({ key: 'inventory_ids', value, label: `库存 #${value}` })),
    ...contextRefs.tool_call_ids.map(value => ({ key: 'tool_call_ids', value, label: `工具调用 #${value}` })),
    ...contextRefs.artifact_ids.map(value => ({ key: 'artifact_ids', value, label: `已存结果 #${value}` })),
    ...selected.map(value => {
      const file = attachments.find(item => item.id === value);
      return {key:'attachment_ids', value, label:file ? (file.relative_path || file.filename)+' · #'+value : '附件 #'+value};
    }),
  ];
  const removeReference = token => {
    setComposerError('');
    if (token.key === 'attachment_ids') setSelected(previous => previous.filter(value => value !== token.value));
    else if (token.key === 'followCanvas' || token.key === 'startEmpty') setContextRefs(previous => ({...previous, [token.key]:false}));
    else if (token.key === 'project_revision_id') setContextRefs(previous => ({ ...previous, project_revision_id: null, element_uids: [], project_objects: [] }));
    else if (token.key === 'project_objects') setContextRefs(previous => ({ ...previous, project_objects: previous.project_objects.filter(object => objectKey(object) !== token.value) }));
    else setContextRefs(previous => ({ ...previous, [token.key]: previous[token.key].filter(value => value !== token.value) }));
  };
  const resultRefs = { artifact_ids: current?.final_content_id ? [current.final_content_id] : [],
    tool_call_ids: current?.final_content_id ? [] : result?.evidence_tool_call_ids || [],
    project_revision_id: current?.result_model?.revision_id ?? current?.model_basis?.revision_id ?? null };
  const originalRequest = current?.request_references || state?.messages?.find(item => item.role === 'user' && item.metadata?.workbench_run_id === current?.id)?.metadata;
  const originalRefs = referencesFor(originalRequest?.context_refs);
  const requestedFiles = uniqueReferences([...originalRefs.attachment_ids, ...(originalRequest?.attachment_ids || [])]);
  const inputRevision = originalRefs.project_revision_id ?? current?.model_basis?.revision_id;
  const outputRevision = current?.result_model?.revision_id;
  const resumeBases = [];
  if (outputRevision && outputRevision !== inputRevision) {
    resumeBases.push({key:String(outputRevision), revision:outputRevision, label:'本轮保存的工程 #'+outputRevision});
  }
  if (inputRevision) resumeBases.push({key:String(inputRevision), revision:inputRevision, label:'原任务输入的工程 #'+inputRevision});
  else if (current?.model_basis?.kind === 'empty') resumeBases.push({key:'empty', label:'原任务从空工程开始'});
  else if (current && !resumeBases.length) {
    resumeBases.push({key:'empty', label:'起点未记录，使用空工程'}, {key:'canvas', label:'使用发送时查看的工程'});
  }
  const resumeBlocked = busy || current?.tools_loaded === false || current?.execution_owner_state === 'unverified';
  const resumeRun = (chosen = resumeBases[0]) => {
    if (!current || resumeBlocked) return;
    seed(current.goal, {
      ...originalRefs, attachment_ids:requestedFiles,
      tool_call_ids: uniqueReferences([...originalRefs.tool_call_ids, ...(result?.evidence_tool_call_ids || []), ...(result?.diagnostic_tool_call_ids || [])]),
      project_revision_id: chosen?.revision ?? null, startEmpty:chosen?.key === 'empty', followCanvas:chosen?.key === 'canvas',
    });
  };
  const openComposer = () => {
    conversationTrigger.current=document.activeElement;
    setDockRequested(true);
    if (docked) input.current?.focus({preventScroll:true});
    else if (!conversationHost) {
      input.current?.scrollIntoView({block:'center'});
      input.current?.focus({preventScroll:true});
    }
  };
  useLayoutEffect(() => {
    if (!docked) return;
    const surface=conversationHost.closest('.bench-workarea');
    const graph=surface?.querySelector('.bench-plot-scroll,.port-diagram,.bench-empty-axis');
    const narrow=window.matchMedia('(max-width: 760px)');
    const target=narrow.matches ? graph || surface : surface;
    target?.scrollIntoView({block:'start'});
    input.current?.focus({preventScroll:true});
    // Some mobile keyboards resize only the visual viewport. Keep the composer inside its visible bottom.
    const viewport=window.visualViewport;
    if (!viewport) return;
    const fitKeyboard=()=>{
      if (narrow.matches && viewport.scale===1) {
        const covered=Math.max(0,window.innerHeight-viewport.offsetTop-viewport.height);
        conversationHost.style.setProperty('--bench-keyboard-inset',covered+'px');
        conversationHost.style.setProperty('--bench-chat-height',Math.round(viewport.height*.46)+'px');
      } else {
        conversationHost.style.removeProperty('--bench-keyboard-inset');
        conversationHost.style.removeProperty('--bench-chat-height');
      }
    };
    fitKeyboard();
    viewport.addEventListener('resize',fitKeyboard);
    viewport.addEventListener('scroll',fitKeyboard);
    return ()=>{
      viewport.removeEventListener('resize',fitKeyboard);
      viewport.removeEventListener('scroll',fitKeyboard);
      conversationHost.style.removeProperty('--bench-keyboard-inset');
      conversationHost.style.removeProperty('--bench-chat-height');
    };
  },[docked,conversationHost]);
  useEffect(() => {
    if (composerLoaded && hash === '#agent') openComposer();
  }, [id, composerLoaded,hash]);
  const returnToBench = destination => {
    const bench=benchHost?.querySelector('.optical-bench');
    setDockRequested(false);
    requestAnimationFrame(()=>{
      bench?.scrollIntoView({block:'start'});
      if (destination === 'canvas') { bench?.focus({preventScroll:true}); return; }
      const trigger=conversationTrigger.current;
      (docked && trigger?.isConnected && bench?.contains(trigger) && trigger.getBoundingClientRect().top<window.innerHeight
        ? trigger : bench?.querySelector('.bench-agent') || bench)?.focus({preventScroll:true});
    });
  };
  const conversation = <aside className="wb-conversation" aria-label="工程对话" onKeyDown={event=>{
    if (docked && event.key==='Escape' && !event.defaultPrevented && !event.nativeEvent.isComposing) {
      event.preventDefault(); returnToBench();
    }
  }}><div className="wb-heading"><h2>工程对话</h2><div className="wb-chat-actions">
    <AgentSettingsLauncher className="bench-text-btn"/>{docked &&
    <button type="button" className="bench-text-btn" title="收起工程对话" onClick={returnToBench}>返回光路</button>}</div></div>
      <div className="wb-messages" ref={scroll} aria-live="polite"
        onScroll={e=>{const box=e.currentTarget;followMessages.current=box.scrollHeight-box.scrollTop-box.clientHeight<12;}}>
        {state?.message_page?.next_before_id && <button type="button" className="btn btn-secondary" disabled={historyPending}
          onClick={loadEarlier}>{historyPending?'正在读取…':'查看更早的对话'}</button>}
        {historyError && <p role="alert">较早对话读取失败。{historyError}</p>}
        {!state?.messages?.length && (caseData.goal?.trim() || caseData.description?.trim()) &&
        <div className="wb-welcome"><button className="btn btn-secondary" onClick={() => seed([caseData.goal, caseData.description].filter(Boolean).join('\n'),{},docked)}>引用工程资料 →</button></div>}
        {state?.messages?.map(item => <article className={`wb-message ${item.role}`} key={item.id} data-message-id={item.id}><small>{messageAuthor(item, current)}</small>
          {item.role === 'assistant' && item.metadata?.workbench_run_id === current?.id && systemReceipt && handoff
            ? <HandoffMessage handoff={handoff}/>
            : <ConversationText content={item.content} canExpand={item.role==='assistant' && Boolean(item.metadata?.workbench_run_id)}/>}
          {!!item.metadata?.attachment_ids?.length && <small>引用 {item.metadata.attachment_ids.length} 个附件</small>}
          {item.role === 'assistant' && editableModels(item.metadata).length > 0 &&
            <div className="action-row">{canvasLinks(editableModels(item.metadata))}</div>}
          {item.metadata?.workbench_run_id && item.role === 'assistant' && <button onClick={() => showRun(item.metadata.workbench_run_id)}>
            {item.metadata?.result_origin === 'system_receipt' ? '查看任务回执' : '查看本轮结果 ↗'}</button>}</article>)}
      </div>
      {sending ? <p className="wb-progress" role="status">正在提交本次任务…</p> : latest && <div className="wb-progress" role="status"><div className="wb-row"><strong>{states[unknownOwner ? 'unverified' : latest.status]} · #{latest.id}</strong>
        {unknownOwner && <button onClick={() => refresh().catch(cause => setError(cause.message))}>重新检查状态</button>}
        {agentActive && <button onClick={() => workbenchApi.stop(id, latest.id).then(refresh).catch(cause => setError(cause.message))}>{unknownOwner ? `结束任务 #${latest.id}` : '停止'}</button>}</div>
        {unknownOwner ? <p>请先查看原执行窗口。若决定结束该编号，可保留已存结果并开始新一轮。</p> :
          busy && <p>{[lastStep?.label || lastStep?.title || '读取工程资料',lastStep?.purpose].filter(Boolean).join(' · ')}</p>}</div>}
      {restoredMessage && <p className="wb-engine" role="status">{restoredMessage}</p>}
      {composerCache.warning && <p className="wb-error" role="alert">{composerCache.warning}</p>}
      <form onSubmit={send} className="wb-composer"><div className="wb-row"><label htmlFor={`wb-message-${id}`}>需求、补充信息或追问</label>
        {benchHost && !docked && <button className="bench-text-btn" type="button" onClick={returnToBench}>返回光路</button>}</div>
        <textarea id={`wb-message-${id}`} ref={input} disabled={sending || !composerLoaded} rows={5} value={message} maxLength={24000} onChange={event => { setComposerError(''); setMessage(event.target.value); }} placeholder="描述目标，或引用工程中的元件、结果和实验资料。" />
        {composerError && <p className="wb-error" role="alert">{composerError}</p>}
        {!!referenceTokens.length && <div className="wb-reference-chips" aria-label="下一条消息引用"><small>这条消息将引用</small><div>{referenceTokens.map(token => <button type="button" key={token.key + token.value} title="移除此引用" onClick={() => removeReference(token)}>{token.label}<span aria-hidden="true"> ×</span></button>)}</div></div>}
        {!!attachments.length && <details><summary>引用已归档资料 · 本条消息 {selected.length}/8</summary><div className="wb-attachments"><ArchiveList files={attachments} label="引用已归档资料">{attachment => <label key={attachment.id}><input type="checkbox" checked={selected.includes(attachment.id)} disabled={busy || uploading || (!selected.includes(attachment.id) && selected.length >= 8)} onChange={() => setSelected(previous => previous.includes(attachment.id) ? previous.filter(value => value !== attachment.id) : [...previous, attachment.id])} />{attachment.relative_path || attachment.filename} <small>#{attachment.id}</small></label>}</ArchiveList></div></details>}
        <div className="wb-row"><FileUpload composer caseId={id} onBusyChange={setUploading} transfers={transfers} disabled={busy} /><button className="btn btn-primary" disabled={busy || !composerLoaded || uploading || !message.trim()}>{unknownOwner ? '请先核实上一轮' : busy ? '工作中…' : '交给 Agent →'}</button></div>
        <small className="wb-engine">{contextRefs.startEmpty ? '本轮从空工程开始' : contextRefs.project_revision_id ? messageBasis+'明确引用的版本 #'+contextRefs.project_revision_id : viewLabel || '可根据案例资料提问'}</small>
      </form>{editorError && <div className="wb-error" role="alert">{editorError} <button onClick={()=>{
        setDockRequested(false); fileActions.current?.focusEditor();
      }}>回到工程编辑</button></div>}
      {error && <div className="wb-error" role="alert">{error}<button onClick={() => refresh().then(() => setError('')).catch(cause => setError(cause.message))}>重新连接</button></div>}
    </aside>;
  return <div className={'wb-workspace'+(docked?' wb-canvas-conversation':'')}>
    <div className="wb-stage" ref={setBenchHost} tabIndex={-1} />
    {docked ? createPortal(conversation,conversationHost) : conversation}
    <main className="wb-results"><div className="wb-result-header"><div><span className="wb-eyebrow">实验工作台</span><h2>目标、工程与实验依据</h2></div>{!!state?.runs?.length && <label className="wb-run-selector">查看任务<select aria-label="任务轮次" value={runId ?? current?.id ?? ''} onChange={event => { setRunId(Number(event.target.value)); if (['project', 'evidence'].includes(tab)) setTab('result'); }}>{runId != null && !state.runs.some(run=>run.id===runId) && <option value={runId}>历史任务 #{runId}</option>}{state.runs.map(run => <option key={run.id} value={run.id}>第 #{run.id} 轮 · {states[run.execution_owner_state === 'unverified' ? 'unverified' : run.status]}</option>)}</select></label>}</div>
      <div className="wb-workspace-context"><button onClick={() => setTab('project')}>{canvasLabel}{viewedMode && (viewRevision || modelView?.dirty) ? ' · ' + (systemModes[viewedMode] || viewedMode) : ''}</button><span>{viewedWavelength != null && viewedWavelength+' nm · '}{filesKnown ? String(attachments.length) + ' 份资料' : '资料清单尚未读入'}</span><button onClick={() => setTab('evidence')}>核对依据与约束 ↗</button></div>
      <nav className="wb-tabs" aria-label="实验工作台视图">{[['project', '当前工程'], ['result', '任务结果'], ['analysis', '分析记录'], ['evidence', '证据与约束'], ['tools', '执行记录']].map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}</nav>
      {['analysis','tools','result'].includes(tab) && current?.tools_loaded===false && <p role="status" className="field-note">
        {detailError ? <>任务 #{current.id} 的执行明细尚未载入。{detailError} <button onClick={retryDetails}>重新读取</button></>
          : '正在读取任务 #'+current.id+' 的执行明细…'}</p>}
      <div hidden={tab !== 'project'}><OpticalProject caseId={id} fileActions={fileActions} savedStudyId={savedStudyId} onOpenStudy={openStudy} onUseQuantity={useStudySource} files={attachments} benchHost={benchHost} openFiles={openFiles}
        benchChat={{host:setConversationHost,open:openComposer,close:()=>setDockRequested(false),docked,ask:(text,refs)=>seed(text,refs,true)}}
        showProject={() => setTab('project')} onAsk={seed} onProjectChanged={projectChanged} onModelView={receiveModel} pending={busy}
        activeTask={agentActive ? latest : null} reportedHead={workspace.project_revision_id} refreshToken={workbenchChanges}
        defaultRevision={singleCanvasCandidate(availableModels,busy)}
        availableAlternatives={availableModels.some(model => model.revision_id !== viewRevision) &&
          <div className="bench-hint" aria-label="可继续编辑的光路">工程中可编辑的方案
            <div className="action-row">{canvasLinks(availableModels.filter(model => model.revision_id !== viewRevision))}</div></div>}/></div>
      {tab === 'analysis' && <><ScientificStudies entryRef={focusStudyEntry} key={studyEntry.sequence} caseId={id} draftScope={caseData.draft_scope} latestStudy={savedStudyId} canvas={modelView} defaultRevision={viewRevision || workspace.project_revision_id} sourceRequest={studyEntry.source} clearSource={clearStudySource} onContinue={useStudySource} onSaved={setSavedStudyId} onAsk={seed} latestRun={latest?.id} latestStatus={latest?.status} files={attachments} tableSources={tableSources} openFiles={openFiles} runId={studyRunId} setRunId={setStudyRunId} openStudy={openStudy} focusRequest={studyFocus} openStudyInput={(...args)=>fileActions.current.openStudyInput(...args)}/>
        {current?.tools_loaded && <><p className="field-note">任务 #{current.id} 使用的计算与方案</p><NumericalResults onUseFit={useFit} caseId={id} key={current.id} calls={tools} onAsk={seed} onOpenStudy={openStudy}/></>}</>}
      {tab === 'evidence' && <EvidenceView caseData={caseData} workspace={workspace} attachments={attachments} filesKnown={filesKnown} factsRun={factsRun} latestRun={latest} onAsk={seed} selected={selected} setSelected={setSelected} disabled={busy || uploading} />}
      {tab === 'tools' && <div className="wb-trace"><p className="field-note">任务 #{current?.id || '—'} 的实际执行记录。每一步都保留输入、返回结果和错误，可据此复核。</p>{!tools.length && current?.tools_loaded!==false && <p className="wb-muted">Agent 调用工具后，执行目的与结果会显示在这里。</p>}{tools.map(call => <ToolTrace onUseFit={useFit} caseId={id} key={call.id} call={call} onAsk={seed} onOpenStudy={openStudy} highlighted={tab === 'tools' && evidenceTarget === call.id} />)}</div>}
      {tab === 'result' && <>
        {current?.model_basis && <StudyModelReceipt key={current.id} caseId={id} input={current.model_basis} result={current.result_model}
          disabled={sending} onAsk={seed} onAdopting={() => fileActions.current?.startHeadUpdate(false) || projectChanged} />}
        {!current && !runId && <section className="wb-empty"><div className="wb-orbit">λ</div><h3>从实验目标开始。</h3><p>在左侧描述目标，或上传实物标签、测量数据和资料。Agent 将判断能解决的部分，建立模型、比较方案，并指导补齐必要信息。</p><div className="wb-capability-grid">{[['设计与建模', '单程光路、谐振腔、参数扫描'], ['元件与方案', '查库存、核规格、保留实物约束'], ['实验与数据', '拟合数据、诊断问题、指导补测'], ['证据与研究', '读取附件、查阅依据、识别缺口']].map(([title, description]) => <div key={title}><strong>{title}</strong><small>{description}</small></div>)}</div><Link to="/inventory">查阅元件库 →</Link></section>}
        <div ref={resultBody} tabIndex={-1} role="region" aria-label={current || runId ? '任务 #'+(runId ?? current.id)+' 的结果' : '任务结果'}>
        {current?.agent_profile && <AgentRunProfile profile={current.agent_profile}/>}
        {!current && runId && <p role={historicalError ? 'alert' : 'status'}>{historicalError ? <>读取任务 #{runId} 失败。{historicalError} <button type="button" onClick={retryDetails}>重新读取</button></> : '正在读取任务 #'+runId+'…'}</p>}
        {current && !result && <section className="wb-pending"><span className="wb-eyebrow">{states[current.execution_owner_state === 'unverified' ? 'unverified' : current.status]}</span><h3>{current.goal.split('\n')[0].slice(0, 140)}</h3><details className="wb-goal"><summary>展开完整任务与约束</summary><p>{current.goal}</p></details><p>{interrupted ? (current.steps?.at(-1)?.summary || states[current.execution_owner_state === 'unverified' ? 'unverified' : current.status]) : (current.steps?.at(-1)?.purpose || 'Agent 正在读取案例与已有信息。')}</p>{tools.map(call => <ToolTrace onUseFit={useFit} caseId={id} key={call.id} call={call} onAsk={seed} onOpenStudy={openStudy} highlighted={tab === 'tools' && evidenceTarget === call.id} />)}{interrupted && <ResumeTask key={current.id} bases={resumeBases} onResume={resumeRun} disabled={resumeBlocked}/>}</section>}
        {result && <><section className="wb-answer"><div className="wb-row"><span className={`wb-status ${shownStatus}`}>{states[shownStatus]} · 任务 #{current.id}</span><button className="btn btn-secondary" onClick={() => saveJSON(`case-${id}-run-${current.id}.json`, current)}>导出本轮记录</button></div>
          {systemReceipt && <h3>系统执行回执</h3>}
          {current.execution_owner_state === 'unverified' && current.id !== latest?.id && <div className="wb-row">
            <button className="btn btn-secondary" onClick={() => setDetailAttempt(n => n + 1)}>重新检查状态</button>
            <button className="btn btn-secondary" onClick={() => workbenchApi.stop(id, current.id).then(() => {
              setDetailAttempt(n => n + 1); return refresh();
            }).catch(cause => setError(cause.message))}>结束任务 #{current.id}</button></div>}
          {current.result_origin === 'recorded_model_finish' && <h3>从执行记录恢复的模型答复</h3>}
          {current.result_origin === 'archived' && <div><h3>历史任务记录</h3><p>这份归档未标注答复来源。可在执行记录中查看当时保存的工具输入和结果。</p></div>}
          <details className="wb-goal"><summary>本轮目标</summary><p>{current.goal}</p></details>
          {handoff && systemReceipt ? <details className="wb-goal"><summary>完整回执原文</summary><AgentText>{result.summary}</AgentText></details>
            : <AgentText>{result.summary}</AgentText>}{bullets(result.findings, 'wb-findings')}
          {handoff && <>{!systemReceipt && <h3>系统整理的任务交接</h3>}<TaskHandoff handoff={handoff}
            calls={tools} onRecord={id => {setEvidenceTarget(id);setTab('tools');}}
            onFile={openFiles} key={current.id} requestedFiles={requestedFiles} bases={resumeBases} onResume={resumeRun} busy={resumeBlocked}/></>}
          {!!result.assumptions?.length && <details><summary>采用的假设 · {result.assumptions.length}</summary>{bullets(result.assumptions)}</details>}{!!result.facts?.length && <details><summary>事实与来源 · {result.facts.length}</summary>{result.facts.map((fact, index) => <p key={index}><small>{factKinds[fact.kind]} · {fact.source}</small><br />{fact.statement}</p>)}</details>}
          {!handoff && (systemReceipt ? <div className="wb-receipt-calls" aria-label="本轮已保存的工具调用">{tools.map(call =>
            <button key={call.id} className={call.status} onClick={() => { setEvidenceTarget(call.id); setTab('tools'); }}>
              <span>#{call.id} {call.label}</span><small>{receiptStates[call.status] || states[call.status]}</small>
            </button>)}</div> : !!result.evidence_tool_call_ids?.length && <p className="wb-evidence">{interrupted ? '已保留的工具记录' : '计算与检索依据'} {result.evidence_tool_call_ids.map(value => <button key={value} onClick={() => { setEvidenceTarget(value); setTab('tools'); }}>记录 #{value}</button>)}</p>)}
          {!handoff && !!result.diagnostic_tool_call_ids?.length && <p className="wb-evidence">查看异常原因 {result.diagnostic_tool_call_ids.map(value =>
            <button key={value} onClick={() => { setEvidenceTarget(value); setTab('tools'); }}>记录 #{value}</button>)}</p>}
          {(current.final_content_id || result.evidence_tool_call_ids?.length > 0) && <button className="btn btn-secondary" onClick={() => seed('关于这份任务结果：\n', resultRefs)}>引用这份结果提问</button>}
        </section>{!!result.questions?.length && <section className="wb-questions"><span className="wb-eyebrow">补充后继续</span><h3>下一步需要的信息</h3>{result.questions.map((question, index) => <article key={index}><h4>{question.question}</h4><p>{question.why}</p><div><strong>怎么获得</strong><p>{question.how_to_obtain}</p></div><small>回复格式：{question.expected_format}</small><button className="btn btn-secondary" onClick={() => seed(`关于「${question.question}」：\n`, resultRefs)}>补充这项信息 →</button></article>)}</section>}
          {current.tools_loaded && <NumericalResults onUseFit={useFit} caseId={id} key={current.id} calls={tools} onAsk={seed} onOpenStudy={openStudy} />}{!handoff && !!result.next_steps?.length && <section className="wb-next"><h3>建议接下来做</h3>{bullets(result.next_steps)}{interrupted ? <ResumeTask key={current.id} bases={resumeBases} onResume={resumeRun} disabled={resumeBlocked}/> : <button className="btn btn-secondary" onClick={() => seed('请围绕当前目标继续推进，复用已有结果，并说明需要我补充的信息。', resultRefs)}>继续推进 →</button>}</section>}
        </>}
        </div>
      </>}
      <details className="wb-advanced" open={archiveRevision != null || undefined} onToggle={event => { if (event.currentTarget === event.target) setAdvancedOpen(event.currentTarget.open); }}>
        <summary>高级工具与历史记录</summary>
        {advancedOpen && <>
          <details className="wb-capabilities" onToggle={e=>{if(e.target===e.currentTarget)setDirectoryOpen(e.currentTarget.open);}}>
            <summary>可调用工具与计算范围</summary>{directoryOpen && <CapabilityBrowser caseId={id}/>}
          </details>
          <details open={archiveRevision != null || undefined} onToggle={event => {
            if (event.target === event.currentTarget && event.currentTarget.open) setVisitedDesign(true);
          }}><summary>历史专用设计</summary>
            <p className="field-note">保留当时的元件、尺寸和假设。打开某份记录可继续试算或建立独立工程。</p>
            {(visitedDesign || archiveRevision != null) && <DesignWorkspace key={`${archiveRevision}-${archiveRow}-${archiveTarget}`} caseData={caseData} savedOnly onAsk={seed}
              openingRevision={archiveRevision} openingRow={archiveRow} openingTarget={archiveTarget} />}
          </details>
        </>}
      </details>
    </main>
  </div>;
}
