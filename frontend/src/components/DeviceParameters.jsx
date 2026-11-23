import React, {useState} from 'react';
import {providerLabel} from '../brand';
import {InputField, shape, valueFor} from './ScientificInput';
import {installedDevice} from './deviceCatalog';

function EditableParameter({device, name, value, disabled, change}) {
  const [draft, setDraft] = useState(null);
  const [error, setError] = useState('');
  const field = shape(device.inputs.properties[name]);
  const formatted = value == null ? '' : field.enum || typeof value === 'object' ? JSON.stringify(value) : String(value);
  const scalar = field.enum || ['number', 'integer', 'string', 'boolean'].includes(field.type);
  function commit(raw) {
    try {
      change(raw === '' && field.type !== 'string' ? null : valueFor(field, raw));
      setDraft(null);
      setError('');
    } catch (cause) {
      setDraft(raw);
      setError(cause.message + '。当前计算沿用上次有效值。');
    }
  }
  return <><InputField name={name} field={device.inputs.properties[name]} schema={device.inputs}
    required={device.inputs.required?.includes(name)} markOptional={false} disabled={disabled}
    value={draft ?? formatted} onChange={scalar ? commit : setDraft}
    onBlur={() => { if (draft !== null) commit(draft); }}/>
    {error && <small role="alert">{error}</small>}</>;
}

export default function DeviceParameters({devices, recipe, onChange, disabled=false}) {
  const device = installedDevice(devices, recipe);
  const source = providerLabel(recipe.provider) + ' · ' + recipe.study + ' v' + recipe.version;
  if (!device) return <div role="status">重算需要 {source}。<pre>{JSON.stringify(recipe.parameters, null, 2)}</pre></div>;
  return <div className="device-parameters"><p>{device.scope}</p>
    {Object.entries(device.inputs.properties)
      .filter(([key]) => key !== device.wavelength_input && key !== device.length_input)
      .map(([key, field]) => <EditableParameter key={key} device={device} name={key} disabled={disabled}
        value={Object.hasOwn(recipe.parameters, key) ? recipe.parameters[key] : field.default}
        change={value => onChange({...recipe, parameters:{...recipe.parameters, [key]:value}})}/>)}
    <small>{source} · 波长随所在光路更新</small></div>;
}
