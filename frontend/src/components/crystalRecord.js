export const sectionRoles = {undoped:'无掺杂段', gain:'增益段', saturable_absorber:'可饱和吸收体'};

export function sectionForm(section = {}) {
  return {
    material: section.material ?? '', role: section.role ?? '', length_mm: section.length_mm ?? '',
    doping_pct: section.doping_pct ?? '', uncertain: Boolean(section.uncertain), note: section.note ?? '',
    initial_transmission_pct: section.initial_transmission_pct ?? '',
    initial_transmission_wavelength_nm: section.initial_transmission_wavelength_nm ?? '',
  };
}

export function completeSectionLength(sections) {
  if (!sections?.length || sections.some(section => !(Number(section.length_mm) > 0))) return null;
  return sections.reduce((sum, section) => sum + Number(section.length_mm), 0);
}

export function independentLength(item) {
  const spec = item?.crystal_spec;
  if (spec?.overall_length_mm != null) return spec.overall_length_mm;
  const length = item?.thickness_mm;
  if (length == null) return '';
  const sum = completeSectionLength(spec?.segments);
  // Historical items stored a complete section sum in thickness_mm.
  if (sum != null && Math.abs(sum - length) <= 1e-9 * Math.max(1, sum, length)) return '';
  return length;
}

export function independentMaterial(item) {
  const spec = item?.crystal_spec;
  if (spec?.material_label) return spec.material_label;
  const summary = [...new Set((spec?.segments || []).map(section => section.material).filter(Boolean))].join(' + ');
  return item?.material && item.material !== summary ? item.material : '';
}

export function crystalDescription(item) {
  const spec = item?.crystal_spec;
  if (!spec?.segments?.length) {
    const length = spec?.overall_length_mm ?? item?.thickness_mm;
    return [item?.material, length != null && `总厚度 ${length} mm`, item?.raw_spec].filter(Boolean).join(' · ') || '尚未录入分段参数';
  }
  const descriptions = spec.segments.map(section => {
    const material = section.material || '材料待确认';
    const role = sectionRoles[section.role];
    const length = section.length_mm == null ? '段长待确认' : section.length_mm + ' mm';
    return [material, role, length].filter(Boolean).join(' · ') + (section.uncertain ? '（待核实）' : '');
  }).join(' → ');
  return [descriptions, spec.material_label && '整件标注 ' + spec.material_label,
    spec.overall_length_mm != null && `登记总厚度 ${spec.overall_length_mm} mm`].filter(Boolean).join(' · ');
}

const numberOrNull = value => value === '' || value == null ? null : Number(value);

export function makeCrystalRecord(form) {
  const independent = numberOrNull(form.overall_length_mm);
  const materialLabel = form.material.trim() || null;
  const sections = form.segments.map(section => ({
    material: section.material.trim() || null, role: section.role || null,
    length_mm: numberOrNull(section.length_mm), doping_pct: numberOrNull(section.doping_pct),
    initial_transmission_pct: numberOrNull(section.initial_transmission_pct),
    initial_transmission_wavelength_nm: numberOrNull(section.initial_transmission_wavelength_nm),
    uncertain: section.uncertain, note: section.note,
  }));
  // A new form shows one empty row for convenience. It describes no section count
  // until the user supplies section data, adds a boundary or reopens a saved row.
  const first = sections[0];
  const singlePlaceholder = sections.length === 1 && !form.recordedSections
    && !Object.values(first).some(value => value != null && value !== '' && value !== false);
  const segments = singlePlaceholder ? [] : sections;
  const width = numberOrNull(form.width_mm), height = numberOrNull(form.height_mm);
  if (!segments.length && width == null && height == null && independent == null && !materialLabel
      && !form.notes.trim() && (!form.coating_note.trim() || form.coating_note.trim() === '未知')) return null;
  return {
    overall_length_mm: independent, material_label: materialLabel, width_mm: width, height_mm: height, segments,
    notes: form.notes, coating_note: form.coating_note.trim() || '未知',
  };
}
