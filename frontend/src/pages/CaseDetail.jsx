import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { agentApi } from '../api/agent';
import { API_BASE_URL, nativeFileId, nativeFileSource, openedCase } from '../api/client';
import { casesApi } from '../api/cases';
import { knowledgeApi } from '../api/knowledge';
import { workbenchApi } from '../api/workbench';
import { deferredPanel } from '../components/deferredPanel';
import ModuleConfigForm, { buildModuleConfig, configToFormValues, OWNED_KEYS } from '../components/ModuleConfigForm';
import { useLanguage } from '../LanguageContext';
import { ModuleFile } from '../components/ModuleFile';
import ExperimentWorkbench from '../components/ExperimentWorkbench';
import { ArchiveList, FilePreview, FileUpload, fileModeLabels } from '../components/CaseFiles';
import BeamStudy from '../components/BeamStudy';
import StudyUsage from '../components/StudyUsage';
import { useCaseRead } from './useCaseRead';
import { ProjectFileActions } from '../components/ProjectFile';
import { transfersFor } from '../components/archiveTransfers';

const readLatest = (caseId, type) => casesApi.getGeneratedContents(caseId, type, 1);
const loadModuleResults = () => import('../components/ModuleResults');
const ModuleResult = deferredPanel(loadModuleResults, '模块计算结果', 'ModuleResult');
const PhysicsFactsBlock = deferredPanel(loadModuleResults, '物理计算依据', 'PhysicsFactsBlock');

function ReadStatus({ record, label }) {
  if (record.error) return <div className="error" role="alert">
    <p>{label}读取失败。{record.error}</p>
    {record.data != null && <p>当前显示上次成功读取的记录。</p>}
    <button className="btn btn-secondary" onClick={record.refresh}>重新读取{label}</button>
  </div>;
  if (record.pending && record.data == null) return <p className="muted" role="status">正在读取{label}…</p>;
  return null;
}

function RemoveOriginal({file, onDeleted}) {
  const { t } = useLanguage();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const remove = async () => {
    if (!window.confirm(t('caseDetail.attachmentsTab.deleteConfirm') + '\n\n' +
      (file.relative_path || file.filename) + ' · #' + file.id)) return;
    setPending(true);
    setError('');
    try {
      await casesApi.deleteAttachment(file.id);
      onDeleted(file.id);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setPending(false);
    }
  };
  return <>
    <button type="button" className="btn btn-danger" disabled={pending} onClick={remove}>{t('common.delete')}</button>
    {error && <div className="error archive-delete-error" role="alert">{error}</div>}
  </>;
}

function CaseDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { t, te } = useLanguage();

  const tabs = [
    ['plan', '实验工作台'],
    ['overview', t('caseDetail.tabs.overview')],
    ['attachments', '实验资料'],
  ];
  const advancedTabs = [
    ['troubleshooting', t('caseDetail.tabs.troubleshooting')],
    ['report', t('caseDetail.tabs.report')],
    ['knowledge', t('caseDetail.tabs.knowledge')],
    ['modules', t('caseDetail.tabs.modules')],
    ['agent', '旧版模块助手'],
  ];
  const caseId = Number(id);

  const caseRecord = useCaseRead(caseId, casesApi.get);
  const caseData = caseRecord.data;
  const projectTitle = useRef(null);
  const readyId = caseData?.id;
  useEffect(() => {
    if (readyId === caseId && document.activeElement === document.body) {
      projectTitle.current?.focus({preventScroll:true});
    }
  }, [readyId, caseId]);
  const [attachments, setAttachments] = useState([]);
  const [selectedFileIds, setSelectedFileIds] = useState([]);
  const localReceipts = useRef(new Map());
  const [previewId, setPreviewId] = useState(null);
  const [archiveVisit,setArchiveVisit] = useState({key:0, fileId:null});
  const [previewRequest,setPreviewRequest] = useState(0);
  const [fileSelections] = useState(()=>new Map());
  const composerActions = useRef(null);
  const transfers = transfersFor(API_BASE_URL,caseId,caseData?.draft_scope);
  const [activeTab, setActiveTab] = useState('plan');
  const [artifactVersion, setArtifactVersion] = useState(0);
  const ready = caseData != null;
  const type = ['report', 'troubleshooting'].includes(activeTab) ? activeTab : null;
  const projectRecord = useCaseRead(caseId, workbenchApi.getModelInput, ready && activeTab === 'overview');
  const projectInfo = projectRecord.data;
  const contentRecord = useCaseRead(caseId, readLatest, ready && !!type, type);
  const readFiles = useCallback(async id => {
    const confirmedBeforeRequest = [...localReceipts.current.keys()];
    return {items:await casesApi.listAttachments(id), confirmedBeforeRequest};
  }, []);
  const filesRecord = useCaseRead(caseId, readFiles, ready);
  const sourceRecord = useCaseRead(caseId, knowledgeApi.listSources, ready && activeTab === 'knowledge');
  const taskRecord = useCaseRead(caseId, agentApi.listTasks, ready && activeTab === 'agent');
  const moduleRecord = useCaseRead(caseId, casesApi.listModules, ready && activeTab === 'modules');
  const partsRecord = useCaseRead(caseId, casesApi.listComponents, ready && activeTab === 'modules');
  const modules = moduleRecord.data || [];
  const components = partsRecord.data || [];
  const sources = sourceRecord.data || [];
  const tasks = taskRecord.data || [];
  const [generating, setGenerating] = useState({});
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const [agentGoal, setAgentGoal] = useState('');
  const [agentMode, setAgentMode] = useState('troubleshooting');
  const [agentRunning, setAgentRunning] = useState(false);
  const [analyzingAttachment, setAnalyzingAttachment] = useState(null);
  const [newModuleType, setNewModuleType] = useState('stability');
  const [moduleRunning, setModuleRunning] = useState(null);
  const [moduleConfigs, setModuleConfigs] = useState({});
  const [moduleForms, setModuleForms] = useState({});
  const [actionError, setActionError] = useState(null);
  const [searching, setSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);

  useEffect(() => { window.scrollTo(0, 0); }, [id]);
  useEffect(() => transfers?.onArchived((item,composer) => {
    localReceipts.current.set(item.id, item);
    setAttachments(previous => previous.some(file => file.id === item.id) ? previous : [...previous, item]);
    if (composer) setSelectedFileIds(previous => [...new Set([...previous,item.id])].slice(0,8));
  }), [transfers]);
  useEffect(() => {
    if (!filesRecord.data) return;
    const {items:listed, confirmedBeforeRequest} = filesRecord.data;
    transfers?.reconcile(listed);
    for (const id of confirmedBeforeRequest) localReceipts.current.delete(id);
    for (const file of listed) localReceipts.current.delete(file.id);
    const currentFiles = [...listed, ...localReceipts.current.values()];
    const currentIds = new Set(currentFiles.map(file => file.id));
    setAttachments(currentFiles);
    setSelectedFileIds(previous => previous.filter(fileId => currentIds.has(fileId)));
  }, [filesRecord.data, transfers]);

  const refreshArtifacts = async () => {
    caseRecord.refresh();
    contentRecord.refresh();
    projectRecord.refresh();
    setArtifactVersion(value => value + 1);
  };

  const savedForm = module => configToFormValues(module.module_type, module.config_json || {});

  const handleGenerate = async (type) => {
    setGenerating((prev) => ({ ...prev, [type]: true }));
    try {
      if (type === 'plan') await casesApi.generatePlan(id);
      if (type === 'troubleshooting') await casesApi.generateTroubleshooting(id);
      if (type === 'report') await casesApi.generateReport(id);
      await refreshArtifacts();
    } catch (err) {
      setActionError(t('caseDetail.attachmentsTab.generateFailed') + err.message);
    } finally {
      setGenerating((prev) => ({ ...prev, [type]: false }));
    }
  };

  const handleSearch = async (event) => {
    event.preventDefault();
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const response = await knowledgeApi.search({ query: searchQuery, caseId, topK: 8 });
      setSearchResults(response.results);
      setHasSearched(true);
    } catch (err) {
      setActionError(t('caseDetail.actionFailed') + err.message);
    } finally {
      setSearching(false);
    }
  };

  const handleAgentSubmit = async (event) => {
    event.preventDefault();
    if (!agentGoal.trim()) return;
    setAgentRunning(true);
    try {
      await agentApi.createTask({ caseId, goal: agentGoal, mode: agentMode });
      setAgentGoal('');
      taskRecord.refresh();
      caseRecord.refresh();
      setActiveTab('agent');
    } catch (err) {
      setActionError(t('caseDetail.agentTab.taskFailed') + err.message);
    } finally {
      setAgentRunning(false);
    }
  };

  const openFiles = (fileId, selection) => {
    setActiveTab('attachments');
    if (Number.isInteger(fileId)) {
      if (filesRecord.data && !attachments.some(item=>item.id===fileId)) {
        setActionError('当前案例的资料清单中找不到原件 #'+fileId+'。可以回到相应任务或分析记录，查看当时保留的工具输入和结果。');
        return;
      }
      if (selection) {
        fileSelections.set(fileId, selection);
        setPreviewRequest(n=>n+1);
      }
      setPreviewId(fileId);
      setArchiveVisit(previous=>({key:previous.key+1,fileId}));
      requestAnimationFrame(() => document.getElementById('case-file-'+fileId)?.scrollIntoView({block:'start'}));
    }
  };
  const quoteFile = (text, fileId) => {
    setActiveTab('plan');
    composerActions.current?.quote(text, {attachment_ids:[fileId]});
  };

  const handleAnalyzeAttachment = async (attachmentId) => {
    setAnalyzingAttachment(attachmentId);
    try {
      await casesApi.analyzeAttachment(attachmentId);
      filesRecord.refresh();
      await refreshArtifacts();
      setActiveTab('attachments');
    } catch (err) {
      setActionError(t('caseDetail.actionFailed') + err.message);
    } finally {
      setAnalyzingAttachment(null);
    }
  };

  const handleCreateModule = async () => {
    try {
      await casesApi.createModule(id, { module_type: newModuleType });
      moduleRecord.refresh();
      setActiveTab('modules');
    } catch (err) {
      setActionError(t('caseDetail.actionFailed') + err.message);
    }
  };

  const handleModuleUpload = async (moduleId, event) => {
    const file = event.target.files[0];
    if (!file) return;
    try {
      await casesApi.uploadModuleFile(moduleId, file);
      event.target.value = '';
      moduleRecord.refresh();
    } catch (err) {
      setActionError(t('caseDetail.modulesTab.uploadFailed') + err.message);
    }
  };

  const handleRunModule = async (module) => {
    // The typed form is the primary input; the advanced JSON textarea (if
    // filled) is merged over it so power users can still override anything.
    const built = buildModuleConfig(module.module_type, moduleForms[module.id] ?? savedForm(module));
    if (built.errors.length > 0) {
      const labels = built.errors.map((k) => t(`caseDetail.modulesTab.form.${k}`)).join('、');
      setActionError(t('caseDetail.modulesTab.badFieldValue') + labels);
      return;
    }
    // The run payload replaces the stored config wholesale, so stored keys the
    // form doesn't own (inline power-curve data, vendor curves, hand-written
    // extras) must ride along — otherwise this click silently deletes them.
    const owned = new Set(OWNED_KEYS[module.module_type] || []);
    const preserved = {};
    Object.entries(module.config_json || {}).forEach(([key, value]) => {
      if (!owned.has(key)) preserved[key] = value;
    });
    let config = { ...preserved, ...built.config };
    const text = (moduleConfigs[module.id] || '').trim();
    if (text) {
      try {
        const advanced = JSON.parse(text);
        // A valid-JSON string/number/array would silently spread into
        // nonsense keys — only a plain object is a config override.
        if (typeof advanced !== 'object' || advanced === null || Array.isArray(advanced)) {
          setActionError(t('caseDetail.modulesTab.badConfig'));
          return;
        }
        config = { ...config, ...advanced };
      } catch {
        setActionError(t('caseDetail.modulesTab.badConfig'));
        return;
      }
    }
    setModuleRunning(module.id);
    try {
      await casesApi.runModule(module.id, { config_json: config });
      moduleRecord.refresh();
      partsRecord.refresh();
    } catch (err) {
      setActionError(t('caseDetail.modulesTab.runFailed') + err.message);
    } finally {
      setModuleRunning(null);
    }
  };

  const handleToggleOwned = async (item) => {
    try {
      await casesApi.updateComponent(item.id, { owned: !item.owned });
      partsRecord.refresh();
    } catch (err) {
      setActionError(t('caseDetail.actionFailed') + err.message);
    }
  };

  const handleDelete = async () => {
    if (!window.confirm(t('caseDetail.confirmDelete'))) return;
    try {
      await casesApi.delete(id);
      navigate('/cases');
    } catch (err) {
      setActionError(t('caseDetail.actionFailed') + err.message);
    }
  };

  const renderJson = (value) => (
    <pre className="json-view">{JSON.stringify(value, null, 2)}</pre>
  );

  const renderCitations = (citations = []) => {
    if (!citations.length) return null;
    return (
      <section className="generated-section">
        <h3>{t('caseDetail.citations')}</h3>
        <div className="citation-list">
          {citations.map((citation) => (
            <article key={`${citation.source_id}-${citation.chunk_id}`} className="generated-card">
              <div className="generated-card-title">
                <span>{citation.title}</span>
                <span className="meta-pill">{citation.source_type} · {citation.score}</span>
              </div>
              <p>{citation.snippet}</p>
            </article>
          ))}
        </div>
      </section>
    );
  };

  const moduleLabel = (type) => {
    const labels = {
      stability: t('caseDetail.modulesTab.stability'),
      beam_profile: t('caseDetail.modulesTab.beamProfile'),
      spectrum: t('caseDetail.modulesTab.spectrum'),
      components: t('caseDetail.modulesTab.components'),
      cavity_design: t('caseDetail.modulesTab.cavityDesign'),
      phase_match: t('caseDetail.modulesTab.phaseMatch'),
      coating_tmm: t('caseDetail.modulesTab.coatingTmm'),
      component_match: t('caseDetail.modulesTab.componentMatch'),
      power_curve: t('caseDetail.modulesTab.powerCurve'),
      literature_search: t('caseDetail.modulesTab.literatureSearch'),
    };
    return labels[type] || type;
  };

  const latestPowerCurveId = Math.max(
    -1, ...modules.filter((m) => m.module_type === 'power_curve').map((m) => m.id),
  );

  const renderModules = () => (
    <div>
      <div className="card">
        <h2 className="card-title">{t('caseDetail.modulesTab.addTitle')}</h2>
        <div className="input-row">
          <select value={newModuleType} onChange={(event) => setNewModuleType(event.target.value)}>
            <optgroup label={t('caseDetail.modulesTab.groupMeasure')}>
              <option value="power_curve">{t('caseDetail.modulesTab.powerCurve')}</option>
              <option value="stability">{t('caseDetail.modulesTab.stability')}</option>
              <option value="beam_profile">{t('caseDetail.modulesTab.beamProfile')}</option>
              <option value="spectrum">{t('caseDetail.modulesTab.spectrum')}</option>
            </optgroup>
            <optgroup label={t('caseDetail.modulesTab.groupCompute')}>
              <option value="cavity_design">{t('caseDetail.modulesTab.cavityDesign')}</option>
              <option value="phase_match">{t('caseDetail.modulesTab.phaseMatch')}</option>
              <option value="coating_tmm">{t('caseDetail.modulesTab.coatingTmm')}</option>
              <option value="component_match">{t('caseDetail.modulesTab.componentMatch')}</option>
            </optgroup>
            <optgroup label={t('caseDetail.modulesTab.groupLiterature')}>
              <option value="literature_search">{t('caseDetail.modulesTab.literatureSearch')}</option>
            </optgroup>
            <optgroup label="—">
              <option value="components">{t('caseDetail.modulesTab.components')}</option>
            </optgroup>
          </select>
          <button className="btn btn-primary" onClick={handleCreateModule}>{t('common.add')}</button>
        </div>
      </div>

      <div className="card">
        <h2 className="card-title">{t('caseDetail.modulesTab.runConfig')}</h2>
        <p className="muted">{t('caseDetail.modulesTab.configHint')}</p>
        <details>
          <summary className="muted">{t('caseDetail.modulesTab.configExamples')}</summary>
          <p className="muted mono">{t('caseDetail.modulesTab.exCavity')}</p>
          <p className="muted mono">{t('caseDetail.modulesTab.exPhase')}</p>
          <p className="muted mono">{t('caseDetail.modulesTab.exCoating')}</p>
          <p className="muted mono">{t('caseDetail.modulesTab.exPower')}</p>
          <p className="muted mono">{t('caseDetail.modulesTab.exLiterature')}</p>
        </details>
      </div>

      {modules.length === 0 && <div className="card"><p>{t('caseDetail.modulesTab.empty')}</p></div>}
      {modules.map((module) => (
        <article key={module.id} className="card">
          <div className="generated-card-title">
            <div>
              <h2 className="card-title">{module.title}</h2>
              <span className="meta-pill">{moduleLabel(module.module_type)}</span>
              <span className={`meta-pill status-${module.status}`}>{te(module.status)}</span>
            </div>
            <div className="action-row">
              <input type="file" onChange={(event) => handleModuleUpload(module.id, event)} />
              <button className="btn btn-primary" disabled={moduleRunning === module.id} onClick={() => handleRunModule(module)}>
                {moduleRunning === module.id ? t('caseDetail.modulesTab.running') : t('caseDetail.modulesTab.run')}
              </button>
            </div>
          </div>
          <ModuleConfigForm
            moduleType={module.module_type}
            values={moduleForms[module.id] ?? savedForm(module)}
            onChange={(key, value) => setModuleForms((prev) => (
              { ...prev, [module.id]: { ...(prev[module.id] ?? savedForm(module)), [key]: value } }
            ))}
          />
          <details>
            <summary className="muted">{t('caseDetail.modulesTab.advancedJson')}</summary>
            <textarea
              className="module-config"
              value={moduleConfigs[module.id] || ''}
              onChange={(event) => setModuleConfigs((prev) => ({ ...prev, [module.id]: event.target.value }))}
              rows={2}
              placeholder={t('caseDetail.modulesTab.runConfig')}
            />
          </details>

          {module.files.length > 0 && (
            <div className="generated-section">
              <h3>{t('caseDetail.modulesTab.files')}</h3>
              {module.files.map(file => <ModuleFile key={file.id} file={file}/>)}
            </div>
          )}

          {module.module_type === 'components' && (
            <div className="generated-section">
              <div className="generated-card-title">
                <h3>{t('caseDetail.modulesTab.componentList')}</h3>
                <a className="btn btn-secondary" href={casesApi.procurementUrl(id)} target="_blank" rel="noreferrer">{t('caseDetail.modulesTab.exportProcurement')}</a>
                <a className="btn btn-secondary" href={casesApi.bomUrl(id)} target="_blank" rel="noreferrer" title={t('caseDetail.modulesTab.exportBomHint')}>{t('caseDetail.modulesTab.exportBom')}</a>
              </div>
              {partsRecord.data == null ? null : components.length === 0 ? <p>{t('caseDetail.modulesTab.noComponents')}</p> : components.map((item) => (
                <div key={item.id} className="source-row">
                  <div>
                    <strong>{item.name}</strong>
                    <p className="muted">{item.category} · {item.specification || '-'} · {item.quantity} {item.unit}</p>
                  </div>
                  <div className="action-row">
                    <span className="meta-pill">{item.procurement_status}</span>
                    <button className="btn btn-secondary" onClick={() => handleToggleOwned(item)}>
                      {item.owned ? t('caseDetail.modulesTab.owned') : t('caseDetail.modulesTab.markOwned')}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          <ModuleResult moduleType={module.module_type} result={module.result_json}
            showComparison={module.id === latestPowerCurveId} />
          {module.result_json && Object.keys(module.result_json).length > 0 && (
            <details>
              <summary className="muted">{t('caseDetail.modulesTab.rawResult')}</summary>
              {renderJson(module.result_json)}
            </details>
          )}
        </article>
      ))}
    </div>
  );

  // Keys handled specially (or internal) — everything else renders generically,
  // so no provider's output field silently disappears from the card again.
  const HANDLED_KEYS = new Set([
    'title', 'summary', 'disclaimer', 'notice', 'citations', 'computed_physics',
    'retrieval', 'confidence', 'usage', 'model', 'model_provider', 'goal',
  ]);

  const sectionTitle = (key) => {
    const translated = t(`caseDetail.genSections.${key}`);
    return translated !== `caseDetail.genSections.${key}` ? translated : key.replace(/_/g, ' ');
  };

  const entryLine = (entry) => {
    if (typeof entry !== 'object' || entry === null) return String(entry);
    return entry.title || entry.step_title || entry.description || entry.step || entry.name
      || entry.symptom || entry.check || entry.action || JSON.stringify(entry);
  };

  const renderValue = (key, value) => {
    if (value == null || value === '') return null;
    if (Array.isArray(value)) {
      if (value.length === 0) return null;
      return (
        <div key={key} className="generated-section">
          <h3>{sectionTitle(key)}</h3>
          <ol>
            {value.map((entry, i) => (
              <li key={i}>{entryLine(entry)}
                {typeof entry === 'object' && entry !== null && (entry.detail || (entry.description && entry.title)) && (
                  <p className="muted">{entry.detail || entry.description}</p>
                )}
                {typeof entry === 'object' && entry !== null && Array.isArray(entry.possible_causes) && (
                  <ul>
                    {entry.possible_causes.map((cause, j) => <li key={j} className="muted">{cause}</li>)}
                  </ul>
                )}
                {typeof entry === 'object' && entry !== null && Array.isArray(entry.solutions || entry.recommended_actions) && (
                  <ul>
                    {(entry.solutions || entry.recommended_actions).map((s, j) => <li key={j}>{s}</li>)}
                  </ul>
                )}
              </li>
            ))}
          </ol>
        </div>
      );
    }
    if (typeof value === 'object') {
      return (
        <div key={key} className="generated-section">
          <h3>{sectionTitle(key)}</h3>
          <table className="mini-table">
            <tbody>
              {Object.entries(value).map(([k, v]) => (
                <tr key={k}><td>{k}</td><td className="num">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    return (
      <div key={key} className="generated-section">
        <h3>{sectionTitle(key)}</h3>
        <p>{String(value)}</p>
      </div>
    );
  };

  const renderGenerated = (type) => {
    const item = contentRecord.data?.[0];
    if (!item) {
      return (
        <div className="card">
          <p>{t('caseDetail.noContent')}</p>
          <button className="btn btn-primary" disabled={generating[type]} onClick={() => handleGenerate(type)}>
            {generating[type] ? t('caseDetail.generating') : t('caseDetail.generate')}
          </button>
          {generating[type] && <p className="muted">{t('caseDetail.generatingHint')}</p>}
        </div>
      );
    }
    const c = item.content || {};
    return (
      <div className="card">
        <div className="generated-card-title">
          <h2 className="card-title">{c.title || c.summary || item.content_type}</h2>
          <div className="action-row">
            {item.model && <span className="meta-pill">{t('caseDetail.modelLabel')}: {item.model}</span>}
            {item.latency_ms != null && <span className="meta-pill">{t('caseDetail.latencyLabel')}: {(item.latency_ms / 1000).toFixed(1)}s</span>}
            {/* Print lives with the content, not in the page header: only the
                active tab is mounted, so a header button would print whatever
                tab happens to be open rather than what the user is looking at. */}
            <button className="btn btn-secondary" onClick={() => window.print()}
              title={t('caseDetail.printHint')}>
              {t('caseDetail.print')}
            </button>
            <button className="btn btn-secondary" disabled={generating[type]} onClick={() => handleGenerate(type)}>
              {generating[type] ? t('caseDetail.generating') : t('caseDetail.generate')}
            </button>
          </div>
        </div>
        {generating[type] && <p className="muted">{t('caseDetail.generatingHint')}</p>}
        {c.notice && <div className="disclaimer">{c.notice}</div>}
        {type === 'plan' && <PhysicsFactsBlock physics={c.computed_physics} />}
        {c.summary && c.title && <p className="result-summary">{c.summary}</p>}
        {Object.entries(c).filter(([key]) => !HANDLED_KEYS.has(key)).map(([key, value]) => renderValue(key, value))}
        {c.disclaimer && <div className="disclaimer">{c.disclaimer}</div>}
        {renderCitations(c.citations)}
        <details>
          <summary className="muted">{t('caseDetail.modulesTab.rawResult')}</summary>
          {renderJson(c)}
        </details>
      </div>
    );
  };

  if (!caseData) return <ReadStatus record={caseRecord} label="案例" />;

  return (
    <div className="case-workspace">
      <header className="detail-header case-project-bar">
        <div className="case-project-name">
          <h1 ref={projectTitle} tabIndex={-1}>{caseData.title}</h1>
          {activeTab==='plan' && <a className="canvas-skip" href="#current-optical-canvas"
            onClick={event=>{
              const canvas=document.getElementById('current-optical-canvas') || document.querySelector('.wb-stage');
              if (canvas) { event.preventDefault(); canvas.focus(); canvas.scrollIntoView({block:'nearest'}); }
            }}>{t('nav.skipCanvas')}</a>}
          {nativeFileSource?.caseId === String(id)
            ? <details className="project-file-location muted">
                <summary>{t('caseDetail.nativeOpened')} · <bdi>{nativeFileSource.label}</bdi></summary>
                <p><bdi dir="auto">{nativeFileSource.path}</bdi></p>
              </details>
            : openedCase && <p className="muted" role="note">{t(String(id) === nativeFileId ? 'caseDetail.nativeOpened' : openedCase === 'legacy' ? 'caseDetail.legacyCopy' : 'caseDetail.localCopy')}</p>}
        </div>
        <nav className="tab-row" aria-label="工程视图">
          {tabs.map(([key, label]) => (
            <button key={key} className={activeTab === key ? 'tab active' : 'tab'}
              aria-current={activeTab === key ? 'page' : undefined} onClick={() => setActiveTab(key)}>{label}</button>
          ))}
          <label className="case-record-navigation">
            <select aria-label="案例记录与高级工具" value={advancedTabs.some(([key]) => key === activeTab) ? activeTab : ''} onChange={event => { if (event.target.value) setActiveTab(event.target.value); }}>
              <option value="">更多记录与工具</option>
              {advancedTabs.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
          </label>
        </nav>
        <div className="action-row">
          <ProjectFileActions id={id} title={caseData.title} createdAt={caseData.created_at}
            editor={composerActions} transfers={transfers} onDelete={caseData.is_open_document ? undefined : handleDelete}
            onRepairInput={()=>{setActiveTab('plan'); requestAnimationFrame(()=>composerActions.current?.focusEditor());}}/>
        </div>
      </header>

      {caseData.recovered_from && <details className="no-print project-file-location"><summary>{t('caseDetail.restoredFile')}</summary>
        <p>{t('caseDetail.previousWorkspace')} <code>{caseData.recovered_from}</code></p></details>}
      <ReadStatus record={caseRecord} label="案例" />
      {activeTab === 'overview' && <ReadStatus record={projectRecord} label="当前工程记录" />}
      {type && <ReadStatus record={contentRecord} label={type === 'report' ? '报告' : '排障记录'} />}
      {actionError && (
        <div className="error action-error">
          <span>{actionError}</span>
          <button className="btn btn-secondary" onClick={() => setActionError(null)}>{t('caseDetail.dismiss')}</button>
        </div>
      )}

      {activeTab === 'overview' && (
        <div className="grid-2">
          {projectInfo?.project && <section className="card case-current-project">
            <div className="wb-row"><h2 className="card-title">当前光学工程 · #{projectInfo.revision_id}</h2><button className="btn btn-secondary" onClick={() => setActiveTab('plan')}>前往实验工作台</button></div>
            <h3>{projectInfo.project.title}</h3>
            <p>{projectInfo.change_note}</p>
            <p className="field-note">此处为最新保存的计算模型，按该版本的假设解释；尚不能作为实测光路或已出光的证明。下方保留原始案例记录，后续更正以最新工程及对话为依据。</p>
            <div className="table-scroll"><table className="mini-table"><thead><tr><th>光学元件 / 传播段</th><th>物理长度 / mm</th><th>折射率 n</th><th>曲率或焦距 / mm</th></tr></thead><tbody>{projectInfo.project.system.elements.map((element, index) => <tr key={element.uid || index}>
              <td>{element.name}</td><td>{element.length_mm || 0}</td><td>{element.n ?? '待补'}</td><td>{element.kind === 'mirror' ? element.radius_mm == null ? '平面' : 'R=' + element.radius_mm : element.kind === 'lens' ? '空气等效 f=' + element.focal_mm : '—'}</td>
            </tr>)}</tbody></table></div>
          </section>}
          <div className="card">
            <h2 className="card-title">{t('caseDetail.overview.basicInfo')}</h2>
            <p><strong>{t('caseDetail.overview.desc')}</strong>{caseData.description || t('caseDetail.overview.none')}</p>
            {caseData.cavity_type && caseData.cavity_type !== 'custom' && <p><strong>{t('library.registered')} · {t('caseDetail.overview.cavity')}</strong>{t('cavityTypes.' + caseData.cavity_type)}</p>}
            <p><strong>{t('caseDetail.overview.goal')}</strong>{caseData.goal}</p>
            <p><strong>{t('caseDetail.overview.status')}</strong>{t(`caseStatus.${caseData.status}`) !== `caseStatus.${caseData.status}` ? t(`caseStatus.${caseData.status}`) : caseData.status}</p>
            <p><strong>{t('caseDetail.overview.visibility')}</strong>{te(caseData.visibility)}</p>
            <p><strong>{t('caseDetail.overview.projectId')}</strong>{caseData.project_id || '-'}</p>
            <p><strong>{t('caseDetail.overview.tags')}</strong>{(caseData.tags || []).join(', ') || '-'}</p>
            <p><strong>{t('caseDetail.overview.safetyNotes')}</strong>{caseData.safety_notes || '-'}</p>
            <p><strong>{t('caseDetail.overview.conclusions')}</strong>{caseData.conclusions || '-'}</p>
          </div>
          <div className="card">
            <h2 className="card-title">{t('caseDetail.overview.paramsSymptoms')}</h2>
            <details open={!projectInfo?.project || undefined}><summary>{projectInfo?.project ? '原始案例参数（保留记录）' : t('caseDetail.overview.parameters')}</summary>
            {Object.keys(caseData.parameters || {}).length === 0 ? <p className="muted">-</p> : (
              <table className="mini-table">
                <tbody>
                  {Object.entries(caseData.parameters).map(([k, v]) => (
                    <tr key={k}><td>{k}</td><td className="num">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</td></tr>
                  ))}
                </tbody>
              </table>
            )}</details>
            <h3>{t('caseDetail.overview.symptoms')}</h3>
            <div className="stat-row">
              {(caseData.symptoms || []).length === 0 ? <p className="muted">-</p>
                : caseData.symptoms.map((sym) => <span key={sym} className="meta-pill">{sym}</span>)}
            </div>
            {Object.keys(caseData.measurements || {}).length > 0 && (
              <details><summary className="muted">{t('caseDetail.overview.measurements')}</summary>{renderJson(caseData.measurements)}</details>
            )}
          </div>
        </div>
      )}

      <div hidden={activeTab !== 'plan'}><ExperimentWorkbench key={id} caseData={caseData} caseFiles={attachments} fileListReady={filesRecord.data != null} selected={selectedFileIds} setSelected={setSelectedFileIds} refreshFiles={filesRecord.refresh} transfers={transfers} onArtifactsChanged={refreshArtifacts} composerActions={composerActions} openFiles={openFiles} /></div>
      {activeTab === 'troubleshooting' && contentRecord.data != null && renderGenerated('troubleshooting')}
      {activeTab === 'report' && contentRecord.data != null && renderGenerated('report')}

      {activeTab === 'knowledge' && (
        <div>
          <form className="card search-panel" onSubmit={handleSearch}>
            <h2 className="card-title">{t('caseDetail.knowledge.searchTitle')}</h2>
            <div className="input-row">
              <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder={t('caseDetail.knowledge.searchPlaceholder')} />
              <button className="btn btn-primary" type="submit" disabled={searching}>{searching ? t('caseDetail.knowledge.searching') : t('common.search')}</button>
            </div>
          </form>

          <div className="card">
            <h2 className="card-title">{t('caseDetail.knowledge.sourcesTitle')}</h2>
            <ReadStatus record={sourceRecord} label="知识来源" />
            {sourceRecord.data == null ? null : sources.length === 0 ? <p>{t('caseDetail.knowledge.noSources')}</p> : sources.map((source) => (
              <div key={source.id} className="source-row">
                <strong>{source.title}</strong>
                <span className="meta-pill">{source.source_type}</span>
                <span className="meta-pill">{source.governance_status}</span>
                <span className="meta-pill">v{source.version}</span>
              </div>
            ))}
          </div>

          {hasSearched && searchResults.length === 0 && (
            <div className="card"><p className="muted">{t('caseDetail.knowledge.noResults')}</p></div>
          )}
          {searchResults.length > 0 && (
            <div className="card">
              <h2 className="card-title">{t('caseDetail.knowledge.resultsTitle')}</h2>
              {searchResults.map((result) => (
                <article key={result.chunk_id} className="generated-card">
                  <div className="generated-card-title">
                    <span>{result.title}</span>
                    <span className="meta-pill">#{result.rank} · {result.score}</span>
                  </div>
                  <p>{result.snippet}</p>
                </article>
              ))}
            </div>
          )}
        </div>
      )}

      {activeTab === 'modules' && <>
        <ReadStatus record={moduleRecord} label="仿真模块" />
        <ReadStatus record={partsRecord} label="元件记录" />
        {moduleRecord.data != null && renderModules()}
      </>}

      {activeTab === 'agent' && (
        <div>
          <form className="card" onSubmit={handleAgentSubmit}>
            <h2 className="card-title">{t('caseDetail.agentTab.newTask')}</h2>
            <textarea value={agentGoal} onChange={(event) => setAgentGoal(event.target.value)} placeholder={t('caseDetail.agentTab.placeholder')} rows={4} />
            <div className="input-row">
              <select value={agentMode} onChange={(event) => setAgentMode(event.target.value)}>
                <option value="troubleshooting">{t('caseDetail.agentTab.mTroubleshooting')}</option>
                <option value="plan">{t('caseDetail.agentTab.mPlan')}</option>
                <option value="report">{t('caseDetail.agentTab.mReport')}</option>
                <option value="cavity_design">{t('caseDetail.agentTab.mCavityDesign')}</option>
                <option value="phase_match">{t('caseDetail.agentTab.mPhaseMatch')}</option>
                <option value="coating_tmm">{t('caseDetail.agentTab.mCoatingTmm')}</option>
                <option value="component_match">{t('caseDetail.agentTab.mComponentMatch')}</option>
                <option value="power_curve">{t('caseDetail.agentTab.mPowerCurve')}</option>
                <option value="literature_search">{t('caseDetail.agentTab.mLiteratureSearch')}</option>
              </select>
              <button className="btn btn-primary" disabled={agentRunning} type="submit">
                {agentRunning ? t('caseDetail.agentTab.running') : t('caseDetail.agentTab.run')}
              </button>
            </div>
          </form>

          <ReadStatus record={taskRecord} label="历史任务" />
          {tasks.map((task) => (
            <article key={task.id} className="card">
              <div className="generated-card-title">
                <h2 className="card-title">{t('caseDetail.agentTab.taskTitle')}{task.id}</h2>
                <span className={`meta-pill status-${task.status}`}>{te(task.status)} · {task.risk_level}</span>
              </div>
              <p>{task.goal}</p>
              <div className="timeline">
                {task.steps.map((step) => (
                  <div key={step.id} className="timeline-step">
                    <span className="step-index">{step.step_index}</span>
                    <div>
                      <strong>{step.title}</strong>
                      <p>{step.result_summary || step.rationale}</p>
                    </div>
                  </div>
                ))}
              </div>
              <h3>{t('caseDetail.agentTab.toolCalls')}</h3>
              {task.tool_calls.map((call) => (
                <div key={call.id} className="source-row">
                  <strong>{call.tool_name}</strong>
                  <div className="action-row">
                    <span className="meta-pill">{call.status}</span>
                    {call.latency_ms != null && <span className="meta-pill">{call.latency_ms} ms</span>}
                  </div>
                </div>
              ))}
            </article>
          ))}
        </div>
      )}

      <div hidden={activeTab !== 'attachments'}>
          <div className="card">
            <h2 className="card-title">{t('caseDetail.attachmentsTab.uploadTitle')}</h2>
            <FileUpload caseId={caseId} transfers={transfers} />
            <p className="muted">{t('caseDetail.attachmentsTab.formats')}</p>
          </div>
          <ReadStatus record={filesRecord} label="实验资料" />
          <BeamStudy key={caseId} caseId={caseId} files={attachments} refreshToken={artifactVersion}
            onUseFit={fit=>{setActiveTab('plan');composerActions.current?.useFit(fit);}}/>
          <ArchiveList key={'archive-'+archiveVisit.key} files={attachments} initialId={archiveVisit.fileId} label={t('caseDetail.archivedFiles')}>{attachment => (
            <div key={attachment.id} id={'case-file-'+attachment.id} className="card attachment-row">
              <div>
                <strong>{attachment.relative_path || attachment.filename}</strong>
                <p className="muted">#{attachment.id} · {fileModeLabels[attachment.read_mode] || attachment.file_type} · {new Date(attachment.uploaded_at).toLocaleString()}</p>
              </div>
              <div className="action-row">
                <button className="btn btn-secondary" aria-expanded={previewId === attachment.id} onClick={() => setPreviewId(previewId === attachment.id ? null : attachment.id)}>{previewId === attachment.id ? '收起' : '查看原件信息'}</button>
                <button type="button" className="btn btn-secondary" onClick={()=>{
                  setActiveTab('plan');composerActions.current?.useOriginal(attachment);
                }}>用整份原件计算</button>
                {attachment.read_mode === 'image' && (
                  <button
                    className="btn btn-primary"
                    disabled={analyzingAttachment === attachment.id}
                    onClick={() => handleAnalyzeAttachment(attachment.id)}
                  >
                    {analyzingAttachment === attachment.id ? t('caseDetail.attachmentsTab.analyzing') : t('caseDetail.attachmentsTab.analyzeImage')}
                  </button>
                )}
                <a href={`${API_BASE_URL}/api/attachments/${attachment.id}`} className="btn btn-secondary" target="_blank" rel="noreferrer">{t('common.download')}</a>
                <RemoveOriginal file={attachment} onDeleted={id => {
                  localReceipts.current.delete(id);
                  setSelectedFileIds(previous => previous.filter(fileId => fileId !== id));
                  setAttachments(previous => previous.filter(file => file.id !== id));
                  filesRecord.refresh();
                }} />
              </div>
              {attachment.index_note && <p className="field-note">{attachment.index_note}</p>}
              {previewId === attachment.id && <><FilePreview key={attachment.id+'-'+previewRequest} file={attachment} caseId={caseId} onQuote={quoteFile} selections={fileSelections}
                onUseSelection={source=>{setActiveTab('plan');composerActions.current?.useMeasurement(source);}}/>
                <button type="button" className="btn btn-secondary" onClick={()=>quoteFile('关于这份原件：\n',attachment.id)}>{t('caseDetail.quoteOriginal')}</button>
                <StudyUsage caseId={caseId} fileId={attachment.id} onOpen={(...args)=>{setActiveTab('plan');composerActions.current?.openStudy(...args);}}/></>}
            </div>
          )}</ArchiveList>
        </div>
    </div>
  );
}

export default function CaseDetailRoute() {
  const { id } = useParams();
  return <CaseDetail key={id} />;
}
