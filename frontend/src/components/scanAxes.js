import {installedDevice} from './deviceCatalog.js';

export function scanFields(catalog, element) {
  if(!element?.prescription)return Object.fromEntries((catalog.models[element?.kind]?.parameters || [])
    .filter(key=>key!=='n' || !element.index_reference?.model)
    .map(key=>[key,{label:catalog.parameters[key],bounds:catalog.bounds[key],value:element[key]}]));
  const device=installedDevice(catalog.devices,element.prescription);
  if(!device)return {};
  const fields={};
  // Older catalogs only described flat numeric controls. Complex declarations need a current catalog.
  const legacy=()=>Object.keys(device.inputs.properties).filter(key=>{
    const spec=device.inputs.properties[key];
    return key!==device.wavelength_input && spec.type==='number' && !device.inputs.readOnly &&
      !spec.readOnly && !('const' in spec) && !['$ref','allOf','anyOf','oneOf','if'].some(k=>k in spec || k in device.inputs);
  });
  for(const key of device.scalar_scan_fields ?? legacy()) {
    const spec=device.inputs.properties[key];
    const length=key===device.length_input;
    fields[length?'length_mm':'parameters.'+key]={label:spec.title || key,bounds:spec,
      value:length?element.length_mm:element.prescription.parameters[key] ?? spec.default};
  }
  return fields;
}

export function scanAxis(catalog, elements, elementIndex, parameter, count = 41) {
  const field = scanFields(catalog,elements[elementIndex])[parameter];
  const value = field?.value;
  const bounds = field?.bounds || {};
  let start = Number.isFinite(value) ? value : '';
  let stop = '';
  if (Number.isFinite(value) && value !== 0) {
    const delta = Math.abs(value) * 0.1;
    const lower = Math.max(value - delta, bounds.minimum ?? -Infinity);
    const upper = Math.min(value + delta, bounds.maximum ?? Infinity);
    start = lower <= (bounds.exclusiveMinimum ?? -Infinity) ? value : Number(lower.toPrecision(12));
    stop = upper >= (bounds.exclusiveMaximum ?? Infinity) ? value : Number(upper.toPrecision(12));
    if (start >= stop) stop = '';
  }
  return {element_index: elementIndex, parameter, start, stop, count};
}

export function initialScanAxes(catalog, project) {
  const elements = project.system.elements;
  const candidates = elements.flatMap((element, index) =>
    Object.keys(scanFields(catalog,element)).map(parameter => {
      const locked = project.parts.find(p => p.element_index === index)?.locked_parameters?.includes(parameter.startsWith('parameters.')?'prescription':parameter);
      const axis = scanAxis(catalog, elements, index, parameter);
      return {axis, priority: Number(!!locked) * 2 + Number(axis.stop === '')};
    }));
  candidates.sort((a, b) => a.priority - b.priority);
  const [first, ...remaining] = candidates;
  const second = remaining.find(c => c.priority === remaining[0].priority && c.axis.element_index !== first.axis.element_index) || remaining[0];
  return [first?.axis ?? null, second?.axis ?? null];
}

export function validScanPair(x, y) {
  return [x, y].every(a => a && Number.isFinite(a.start) && Number.isFinite(a.stop) && a.stop > a.start &&
    Number.isInteger(a.count) && a.count >= 2 && a.count <= 101) &&
    (x.element_index !== y.element_index || x.parameter !== y.parameter);
}
