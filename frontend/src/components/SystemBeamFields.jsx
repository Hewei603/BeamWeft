import React, {useLayoutEffect, useRef} from 'react';
import {BeamSourceFields} from './BeamSourceFields';
import {setMainSource, sourceFromScalars} from './mainBeamSource';

export default function SystemBeamFields({system, change, prefix = ''}) {
  const sourcePanel = useRef(null), refocusM2 = useRef(false);
  const explicit = system.mode === 'single_pass' && system.source;
  const axisPrefix = prefix || '主光路 ';
  useLayoutEffect(() => {
    if (!refocusM2.current) return;
    refocusM2.current = false;
    sourcePanel.current?.querySelector('[aria-label="'+axisPrefix+'T M²"]')?.focus();
  }, [explicit,axisPrefix]);
  const fields = [
    ['wavelength_nm', '波长 / nm'],
    ...(system.mode === 'single_pass' && !explicit ? [
      ['input_radius_mm', '输入半径 / mm'],
      ['input_curvature_mm', '输入波前 R / mm', '空白 = 束腰'],
      ['beam_quality_m2', '等效 M²'],
    ] : []),
  ];
  function setField(key, value) {
    if (key === 'beam_quality_m2' && value === null) {
      // The scalar format defaults an omitted M² to one; only the axis
      // description distinguishes an explicitly unknown beam quality.
      const source = sourceFromScalars(system);
      refocusM2.current = true;
      change(setMainSource(system,{tangential:{...source.tangential,m2:null}}));
    } else change({...system,[key]:value});
  }
  return <>{fields.map(([key, label, placeholder]) => <label key={key}>{label}
    <input aria-label={prefix + label} type="number" step="any" min={key === 'beam_quality_m2' ? 1 : undefined}
      value={system[key] ?? (key === 'beam_quality_m2' && !Object.hasOwn(system,key) ? 1 : '')} placeholder={placeholder}
      onChange={event => setField(key, event.target.value === '' ? null : Number(event.target.value))} />
  </label>)}
    {explicit ? <div ref={sourcePanel} className="op-main-source">
      <BeamSourceFields source={system.source} accessiblePrefix={axisPrefix} change={source=>change(setMainSource(system,source))}/>
    </div> : system.mode==='single_pass' && <button type="button" className="btn btn-secondary"
      onClick={()=>change(setMainSource(system,sourceFromScalars(system)))}>按 T/S 轴编辑光源</button>}
  </>;
}
