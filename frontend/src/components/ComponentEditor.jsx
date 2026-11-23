import React, { useId, useState } from 'react';
import { inventoryApi, inventoryCategoryLabels } from '../api/inventory';
import { useLanguage } from '../LanguageContext';
import { completeSectionLength, crystalDescription, independentLength, independentMaterial, makeCrystalRecord, sectionForm, sectionRoles } from './crystalRecord';
import './ComponentEditor.css';

const materials = ['YAG', 'Nd:YAG', 'Cr:YAG', 'YVO4', 'Nd:YVO4'];
const numberOrNull = value => value === '' || value == null ? null : Number(value);
const supportsSections = category => category !== 'mirror' && category !== 'lens';
export { crystalDescription } from './crystalRecord';

const toForm = (item, defaultCategory) => ({
    name: item?.name || '', category: item?.category || defaultCategory, vendor: item?.vendor || '',
    location: item?.location || '', keeper:item?.keeper || '', quantity: item?.quantity ?? 1, raw_spec: item?.raw_spec || '',
    dimensions:item?.dimensions || '', cut_axis:item?.cut_axis || '', doping_pct:item?.doping_pct ?? '',
    cut_angle_theta_deg:item?.cut_angle_theta_deg ?? '', cut_angle_phi_deg:item?.cut_angle_phi_deg ?? '',
    material: independentMaterial(item), diameter_mm: item?.diameter_mm ?? '',
    focal_length_mm: item?.focal_length_mm ?? '', roc_mm: item?.roc_mm ?? '',
    roc_mode: item?.roc_is_flat ? 'flat' : item?.roc_mm != null ? 'curved' : 'unknown',
    width_mm: item?.crystal_spec?.width_mm ?? '', height_mm: item?.crystal_spec?.height_mm ?? '',
    overall_length_mm: independentLength(item),
    recordedSections: Boolean(item?.crystal_spec?.segments?.length),
    segments: item?.crystal_spec?.segments?.length ? item.crystal_spec.segments.map(sectionForm) : [sectionForm()],
    coating_note: item?.crystal_spec?.coating_note || '未知', notes: item?.crystal_spec?.notes || '',
    coatings: (item?.coatings || []).map(c => ({ ...c, value_pct: c.value_pct ?? '' })),
  });

function itemPayload(form) {
  const physicalSections = supportsSections(form.category);
  const crystal = physicalSections ? makeCrystalRecord(form) : null;
  const coatings = form.coatings.map(c => ({surface:c.surface, wl_min_nm:Number(c.wl_min_nm), wl_max_nm:Number(c.wl_max_nm), function:c.function,
    value_type:c.value_pct === '' ? null : c.value_type || 'R', comparator:c.value_pct === '' ? null : c.comparator || '=',
    value_pct:numberOrNull(c.value_pct), raw_fragment:c.raw_fragment || null}));
  return {name:form.name.trim(), category:form.category, vendor:form.vendor || null, location:form.location || null, keeper:form.keeper || null,
    quantity:Number(form.quantity), raw_spec:form.raw_spec, material:form.material || null,
    diameter_mm:numberOrNull(form.diameter_mm), thickness_mm:numberOrNull(form.overall_length_mm),
    dimensions:form.dimensions || null, cut_axis:form.cut_axis || null, doping_pct:numberOrNull(form.doping_pct),
    cut_angle_theta_deg:numberOrNull(form.cut_angle_theta_deg), cut_angle_phi_deg:numberOrNull(form.cut_angle_phi_deg),
    roc_mm:form.roc_mode === 'curved' ? numberOrNull(form.roc_mm) : null, roc_is_flat:form.roc_mode === 'flat',
    focal_length_mm:numberOrNull(form.focal_length_mm), crystal_spec:crystal, coatings};
}

export default function ComponentEditor({ item = null, defaultCategory = 'other', onSaved, onCancel }) {
  const {t} = useLanguage();
  const materialOptions = useId();
  const [form, setForm] = useState(() => toForm(item, defaultCategory));
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [recognizing, setRecognizing] = useState(false), [recognition, setRecognition] = useState(null);
  const [edited, setEdited] = useState(false), [photoNotice, setPhotoNotice] = useState('');
  const [photoWarnings, setPhotoWarnings] = useState([]);
  const [structureEnabled, setStructureEnabled] = useState(Boolean(item?.crystal_spec) || defaultCategory === 'gain_crystal');
  const sectional = supportsSections(form.category);
  const crystalCategory = ['gain_crystal','nonlinear_crystal','crystal_other'].includes(form.category);
  const update = (key, value) => { setEdited(true); setForm(prev => ({ ...prev, [key]: value })); };
  const changeCategory = category => {
    if (sectional && !supportsSections(category)) {
      const spec = makeCrystalRecord(form);
      if (spec && (spec.segments.length || spec.width_mm != null || spec.height_mm != null
          || spec.notes || (spec.coating_note && spec.coating_note !== '未知'))) {
        setError('镜片和透镜目前不能保存分段记录。请核对并移除截面、分段和端面记录后再改类，或保留当前物料。');
        return;
      }
    }
    setError('');
    update('category',category);
    if (category === 'gain_crystal') setStructureEnabled(true);
  };
  const readPhoto = async event => {
    const file = event.target.files?.[0]; event.target.value = '';
    if (!file) return;
    setRecognizing(true); setRecognition(null); setError(''); setPhotoNotice('');
    try { setRecognition(await inventoryApi.recognizeLabel(file)); }
    catch (err) { setError(`读取标签未完成：${err.message}。已填写的内容保留，可继续手工录入。`); }
    finally { setRecognizing(false); }
  };
  const adoptRecognition = () => {
    const draft = toForm(recognition.draft, defaultCategory);
    const warnings = [...(recognition.warnings || [])];
    if (supportsSections(form.category) && supportsSections(draft.category)) {
      for (const [key, name, unit] of [['overall_length_mm','总厚度',' mm'],['material','整件材料标注','']]) {
        if (form[key] === '') continue;
        const differs = key === 'overall_length_mm' ? Number(form[key]) !== Number(draft[key]) : form[key] !== draft[key];
        if (draft[key] !== '' && differs)
          warnings.push(`已保留登记的${name} ${form[key]}${unit}。图片识别值为 ${draft[key]}${unit}，请核对。`);
        draft[key] = form[key];
      }
    }
    const label = [recognition.source_filename && `来源图片：${recognition.source_filename}`,
      draft.raw_spec !== form.raw_spec && draft.raw_spec, ...warnings.map(w => `识别待核实：${w}`)].filter(Boolean).join('\n');
    draft.raw_spec = [form.raw_spec, label].filter(Boolean).join('\n\n');
    setForm(draft); setEdited(true); setStructureEnabled(Boolean(recognition.draft.crystal_spec));
    setPhotoWarnings(warnings);
    setPhotoNotice(`已采用 ${recognition.source_filename || '标签照片'} 的识别草稿。请逐项核对后再保存；尚未写入元件库。`);
    setRecognition(null);
  };
  const segment = (index, patch) => update('segments', form.segments.map((s, i) => i === index ? { ...s, ...patch } : s));
  const moveSegment = (index, delta) => { const list = [...form.segments]; [list[index], list[index + delta]] = [list[index + delta], list[index]]; update('segments', list); };
  const removeSegment = index => {
    if (form.segments.length > 1) { update('segments', form.segments.filter((_, i) => i !== index)); return; }
    setEdited(true);
    setForm(previous => ({...previous, recordedSections:false, segments:[sectionForm()]}));
  };
  const total = completeSectionLength(form.segments);
  const recordedLength = numberOrNull(form.overall_length_mm);
  const lengthDifference = recordedLength == null || total == null ? null : recordedLength - total;
  const conflictingLength = lengthDifference != null && Math.abs(lengthDifference) > 1e-9 * Math.max(1, total, recordedLength);
  const submit = async event => {
    event.preventDefault(); if (recognizing) return; setError('');
    if (!form.name.trim()) { setError('请给这件元件填写名称，便于在案例中选择。'); return; }
    if (form.category === 'mirror' && form.roc_mode === 'curved' && (!Number.isFinite(Number(form.roc_mm)) || !Number(form.roc_mm))) { setError('曲面镜需要非零曲率半径；不知道时请选“曲率未知”，不要把未知当作平面。'); return; }
    if (form.category === 'lens' && form.focal_length_mm !== '' && !Number(form.focal_length_mm)) { setError('已知焦距需要非零数值；不知道时请留空。R是曲率，不能直接当作FL。'); return; }
    const values = itemPayload(form);
    const baseline = item && itemPayload(toForm(item,defaultCategory));
    const payload = baseline ? Object.fromEntries(Object.entries(values).filter(([key,value]) =>
      JSON.stringify(value) !== JSON.stringify(baseline[key]))) : values;
    if ('coatings' in payload && form.coatings.some(c => !(Number(c.wl_min_nm) > 0) || !(Number(c.wl_max_nm) >= Number(c.wl_min_nm)))) { setError('结构化镀膜需要明确波长范围。波长未知时可将信息保留在标签原文。'); return; }
    setBusy(true);
    try { const saved = item?.id ? await inventoryApi.updateItem(item.id, payload) : await inventoryApi.createItem(payload); await onSaved(saved); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const coatingRow = (index, patch) => update('coatings', form.coatings.map((c, i) => i === index ? { ...c, ...patch } : c));
  return <form className="component-editor" onSubmit={submit} aria-label={item ? '编辑元件' : '新增元件'}>
    <div className="component-heading"><div><h3>{item ? `编辑元件 #${item.id}` : '新增元件'}</h3><p>已知多少录入多少。未知参数留空；原始标签与待确认信息会一起保存。</p></div>{onCancel && <button type="button" className="btn btn-secondary" disabled={busy} onClick={onCancel}>关闭</button>}</div>
    <section className="component-photo"><div className="component-heading"><div><h4>从元件标签开始</h4><p>上传照片生成待核对草稿；识别完成后由你选择是否采用，不会自动入库或替换工程。</p></div><label className="btn btn-secondary component-photo-upload">{recognizing ? '正在读取标签…' : '读取标签照片'}<input aria-label="读取元件标签照片" type="file" accept="image/png,image/jpeg,image/webp" disabled={busy || recognizing} onChange={readPhoto} /></label></div>
      {recognizing && <p role="status">正在读取标签，可能需要几十秒。你可以继续查看或手工填写，已填内容不会被覆盖。</p>}
      {recognition && <article className="component-preview"><h4>识别草稿 · {recognition.draft?.name || '名称待确认'}</h4><p>{recognition.draft?.crystal_spec ? crystalDescription(recognition.draft) : recognition.draft?.raw_spec || '请核对下方识别结果。'}</p>{!!recognition.warnings?.length && <ul className="component-scope-list">{recognition.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}{(edited || item) && <p className="component-warning">采用草稿会重新填写元件与分段。已有标签原文会保留。也可以参照识别结果手动补充。</p>}<div className="component-actions"><button type="button" className="btn btn-secondary" onClick={adoptRecognition}>采用识别草稿，填写到表单</button><button type="button" className="btn btn-secondary" onClick={() => setRecognition(null)}>保留当前填写内容</button></div></article>}
      {photoNotice && <p className="component-success" role="status">{photoNotice}</p>}
      {!!photoWarnings.length && <ul className="component-scope-list">{photoWarnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
    </section>
    <div className="component-grid">
      <label>元件名称<input aria-label="元件名称" value={form.name} maxLength={255} required onChange={e => update('name', e.target.value)} placeholder="例如：CRYSLASER JBB4260 复合晶体" /></label>
      <label>元件类别<select aria-label="元件类别" value={form.category} onChange={e => changeCategory(e.target.value)}>{Object.entries(inventoryCategoryLabels).map(([key,label]) => <option key={key} value={key}>{t('inventoryPage.'+label)}</option>)}</select></label>
      <label>厂家<input aria-label="元件厂家" value={form.vendor} onChange={e => update('vendor', e.target.value)} /></label>
      <label>数量<input aria-label="元件数量" type="number" min="0" step="any" required value={form.quantity} onChange={e => update('quantity', e.target.value)} /></label>
      <label>存放位置<input aria-label="元件存放位置" value={form.location} onChange={e => update('location', e.target.value)} /></label>
      <label>保管人<input aria-label="元件保管人" value={form.keeper} onChange={e => update('keeper', e.target.value)} /></label>
    </div>
    <label className="component-wide">标签原文 / 参数来源<textarea aria-label="标签原文或参数来源" rows={3} value={form.raw_spec} onChange={e => update('raw_spec', e.target.value)} placeholder="完整保留标签文字。看不清的内容写待确认；不要把吸收体T值当作输出镜透过率。" /></label>
    {item?.source_file && <p className="component-help">库存来源 · {item.source_file}</p>}
    {item?.parse_notes?.length > 0 && <details className="component-coatings"><summary>来源与核对记录 · {item.parse_notes.length} 条</summary><ul>{item.parse_notes.map((note,index)=><li key={index}>{note}</li>)}</ul></details>}
    {sectional && <><div className="component-grid">
      <label>整件材料标注<input aria-label={crystalCategory ? '晶体材料文字' : '整件材料文字'} maxLength={100} value={form.material} onChange={e => update('material', e.target.value)} placeholder="可留空" /></label>
      <label>独立登记的总厚度 / mm<input aria-label={crystalCategory ? '晶体总厚度 mm' : '元件总厚度 mm'} type="number" step="any" min="0" value={form.overall_length_mm} onChange={e => update('overall_length_mm', e.target.value)} placeholder="量具读数或标签值" /></label>
      <label>直径 / mm<input aria-label="元件直径 mm" type="number" step="any" min="0" value={form.diameter_mm} onChange={e=>update('diameter_mm',e.target.value)} /></label>
      <label>外形尺寸原文<input aria-label="外形尺寸原文" maxLength={100} value={form.dimensions} onChange={e=>update('dimensions',e.target.value)} /></label>
      {crystalCategory && <><label>切向标注<input aria-label="晶体切向" value={form.cut_axis} maxLength={50} onChange={e=>update('cut_axis',e.target.value)} /></label>
        <label>切角 θ / °<input aria-label="晶体切角 theta 度" type="number" step="any" value={form.cut_angle_theta_deg} onChange={e=>update('cut_angle_theta_deg',e.target.value)} /></label>
        <label>切角 φ / °<input aria-label="晶体切角 phi 度" type="number" step="any" value={form.cut_angle_phi_deg} onChange={e=>update('cut_angle_phi_deg',e.target.value)} /></label>
        {form.category !== 'gain_crystal' && <label>掺杂浓度 / %<input aria-label="晶体掺杂浓度 %" type="number" step="any" min="0" max="100" value={form.doping_pct} onChange={e=>update('doping_pct',e.target.value)} /></label>}</>}
    </div><label className="component-check"><input aria-label={crystalCategory ? '展开晶体截面与分段' : '展开元件截面与分段'} type="checkbox" checked={structureEnabled} onChange={e => setStructureEnabled(e.target.checked)} />显示截面与分段</label></>}
    {sectional ? structureEnabled ? <section className="component-crystal">
      <div className="component-grid"><label>截面宽 / mm<input aria-label="晶体截面宽 mm" type="number" step="any" min="0" value={form.width_mm} onChange={e => update('width_mm', e.target.value)} /></label><label>截面高 / mm<input aria-label="晶体截面高 mm" type="number" step="any" min="0" value={form.height_mm} onChange={e => update('height_mm', e.target.value)} /></label><div className="component-total" data-pending={total == null || conflictingLength || undefined}><small>分段长度合计</small><strong>{total == null ? '待补段长' : Number(total.toPrecision(12)) + ' mm'}</strong></div></div>
      {conflictingLength && <p className="component-warning" role="status">登记总厚度 {recordedLength} mm，与分段合计相差 {Number(Math.abs(lengthDifference).toPrecision(8))} mm。两项会分别保存，装入工程前请核对长度。</p>}
      <h4>物料分段 · 按标签列出的顺序</h4><p className="component-help">可单独登记材料、作用或长度。折射率计算所需的材料、波长和适用条件可在工程中核对。</p>
      <datalist id={materialOptions}>{materials.map(material => <option key={material} value={material} />)}</datalist>
      {form.segments.map((s, index) => <article className={'component-segment ' + s.role} key={index}>
        <div className="component-heading"><strong>第 {index + 1} 段{sectionRoles[s.role] && ' · ' + sectionRoles[s.role]}</strong><div className="component-row"><button type="button" disabled={busy || index === 0} onClick={() => moveSegment(index, -1)} aria-label={`第${index + 1}段上移`}>↑</button><button type="button" disabled={busy || index === form.segments.length - 1} onClick={() => moveSegment(index, 1)} aria-label={`第${index + 1}段下移`}>↓</button><button type="button" disabled={busy} onClick={() => removeSegment(index)}>删除此段</button></div></div>
        <div className="component-grid"><label>材料<input aria-label={`第${index + 1}段材料`} list={materialOptions} maxLength={100} value={s.material} onChange={e => segment(index, {material:e.target.value})} placeholder="输入材料名，未知留空" /></label>
          <label>材料作用<select aria-label={`第${index + 1}段作用`} value={s.role} onChange={e => segment(index, {role:e.target.value})}><option value="">待确认</option>{Object.entries(sectionRoles).map(([key,name]) => <option key={key} value={key}>{name}</option>)}</select></label>
          <label>长度 / mm<input aria-label={`第${index + 1}段长度 mm`} type="number" min="0" step="any" value={s.length_mm} onChange={e => segment(index, { length_mm: e.target.value })} placeholder="未知" /></label>
          {(s.role !== 'undoped' || s.doping_pct !== '') && <label>掺杂浓度 / % · 可留空<input aria-label={`第${index + 1}段掺杂浓度 %`} type="number" min="0" max="100" step="any" value={s.doping_pct} onChange={e => segment(index, { doping_pct: e.target.value })} placeholder="未知" /></label>}
        </div>
        <label className="component-check"><input type="checkbox" checked={Boolean(s.uncertain)} onChange={e => segment(index, { uncertain: e.target.checked })} />这一段的材料、尺寸或浓度来自推断，待核实</label>
        <label className="component-wide">这一段的依据 / 待确认项<input aria-label={`第${index + 1}段依据`} value={s.note || ''} onChange={e => segment(index, { note: e.target.value })} /></label>
        {(s.role === 'saturable_absorber' || s.initial_transmission_pct !== '' || s.initial_transmission_wavelength_nm !== '') && <details className="component-absorber"><summary>吸收体初始透过率 · 仅在含义已确认时填写</summary><p>标签只写“T=90%”时，先保留在上方原文。这里的T₀不是腔镜透过率，冷腔几何也不会由此预测调Q脉冲。</p><div className="component-grid"><label>已确认初始透过率 / %<input aria-label={`第${index + 1}段初始透过率 %`} type="number" min="0" max="100" step="any" value={s.initial_transmission_pct} onChange={e => segment(index, { initial_transmission_pct: e.target.value })} placeholder="含义未确认则留空" /></label><label>对应波长 / nm<input aria-label={`第${index + 1}段初始透过率波长 nm`} type="number" min="1" step="any" value={s.initial_transmission_wavelength_nm} onChange={e => segment(index, { initial_transmission_wavelength_nm: e.target.value })} /></label></div></details>}
      </article>)}
      <button type="button" className="btn btn-secondary" disabled={busy || form.segments.length >= 24} onClick={() => update('segments', [...form.segments, sectionForm()])}>＋ 添加晶体段</button>
      {form.segments.some(s => s.role === 'saturable_absorber') && <p className="component-warning">含可饱和吸收体：当前可计算声明假设下的冷腔几何；脉宽、重复频率、单脉冲能量和输出功率尚需动力学模型。</p>}
      <label className="component-wide">晶体端面镀膜 / 原文<input aria-label="晶体端面镀膜原文" value={form.coating_note} onChange={e => update('coating_note', e.target.value)} /></label><label className="component-wide">补充说明<textarea aria-label="晶体补充说明" rows={2} value={form.notes} onChange={e => update('notes', e.target.value)} /></label>
    </section> : null : <section>
      <div className="component-grid"><label>基底材料 · 可留空<input aria-label="基底材料" value={form.material} onChange={e => update('material', e.target.value)} placeholder="未知" /></label><label>直径 / mm<input aria-label="元件直径 mm" type="number" min="0" step="any" value={form.diameter_mm} onChange={e => update('diameter_mm', e.target.value)} /></label><label>中心厚度 / mm<input aria-label="元件中心厚度 mm" type="number" min="0" step="any" value={form.overall_length_mm} onChange={e => update('overall_length_mm', e.target.value)} /></label></div>
      {form.category === 'mirror' ? <div className="component-grid"><label>反射面形状<select aria-label="镜面形状" value={form.roc_mode} onChange={e => update('roc_mode', e.target.value)}><option value="unknown">曲率未知 · 不当作平面</option><option value="flat">确认是平面镜</option><option value="curved">已知曲率半径</option></select></label>{form.roc_mode === 'curved' && <label>反射面曲率 R / mm<input aria-label="镜面曲率 R mm" type="number" step="any" required value={form.roc_mm} onChange={e => update('roc_mm', e.target.value)} placeholder="凹面正数，凸面负数" /></label>}</div>
        : <label className="component-wide">焦距 FL / mm · 可留空<input aria-label="透镜焦距 FL mm" type="number" step="any" value={form.focal_length_mm} onChange={e => update('focal_length_mm', e.target.value)} placeholder="R是曲率，FL才是焦距；不知道请留空" /></label>}
      <p className="component-help">腔镜用途在案例替换时选择为M1或M2。反射曲率不代表808 nm透射时的焦距；两面曲率与基底未知会继续列为待核验项。</p>
    </section>}
    <details className="component-coatings"><summary>已确认的镀膜参数 · {form.coatings.length} 条（可全部留空）</summary><p>只有明确波长、表面和功能的镀膜才录入这里。看不清的红字、含义未知的T保留在标签原文。</p>
      {form.coatings.map((c, index) => <article className="component-segment" key={index}><div className="component-grid"><label>表面<select aria-label={`镀膜${index + 1}表面`} value={c.surface} onChange={e => coatingRow(index, { surface: e.target.value })}>{['S1', 'S2', 'both'].map(s => <option key={s} value={s}>{s === 'both' ? '双面' : s}</option>)}</select></label><label>功能<select aria-label={`镀膜${index + 1}功能`} value={c.function} onChange={e => coatingRow(index, { function: e.target.value })}>{['HR', 'AR', 'HT', 'PR'].map(s => <option key={s}>{s}</option>)}</select></label><label>起始波长 / nm<input aria-label={`镀膜${index + 1}起始波长 nm`} type="number" min="1" step="any" value={c.wl_min_nm} onChange={e => coatingRow(index, { wl_min_nm: e.target.value, wl_max_nm: c.wl_max_nm || e.target.value })} /></label><label>结束波长 / nm<input aria-label={`镀膜${index + 1}结束波长 nm`} type="number" min="1" step="any" value={c.wl_max_nm} onChange={e => coatingRow(index, { wl_max_nm: e.target.value })} /></label><label>量<select value={c.value_type || 'R'} onChange={e => coatingRow(index, { value_type: e.target.value })}><option>R</option><option>T</option></select></label><label>关系<select value={c.comparator || '='} onChange={e => coatingRow(index, { comparator: e.target.value })}>{['=', '>', '<'].map(s => <option key={s}>{s}</option>)}</select></label><label>数值 / % · 未知留空<input aria-label={`镀膜${index + 1}数值 %`} type="number" min="0" max="100" step="any" value={c.value_pct} onChange={e => coatingRow(index, { value_pct: e.target.value })} /></label></div><button type="button" onClick={() => update('coatings', form.coatings.filter((_, i) => i !== index))}>删除镀膜条目</button></article>)}
      <button type="button" className="btn btn-secondary" onClick={() => update('coatings', [...form.coatings, { surface: 'both', function: 'AR', wl_min_nm: '', wl_max_nm: '', value_type: 'R', comparator: '<', value_pct: '', raw_fragment: '' }])}>＋ 添加已确认镀膜</button>
    </details>
    {error && <p className="component-error" role="alert">{error}</p>}
    <div className="component-actions"><button className="btn btn-primary" disabled={busy || recognizing} type="submit">{busy ? '保存中…' : item ? '保存元件修改' : '保存到元件库'}</button>{onCancel && <button className="btn btn-secondary" type="button" disabled={busy} onClick={onCancel}>取消</button>}</div>
    <p className="component-help">保存元件库记录不会改写已有案例或历史工程；在案例中替换后才会建立新的仿真版本。</p>
  </form>;
}
