import React from 'react';
import RayMatrixFields from './RayMatrixFields';
import DeviceParameters from './DeviceParameters';
import {installedDevice} from './deviceCatalog';

const indexDirections={ordinary:'o 光',extraordinary:'e 光',x:'晶体 X 轴',y:'晶体 Y 轴',z:'晶体 Z 轴'};

function IndexFields({catalog,element,stockBound,frozenModel,wavelengthNm,reading,change,locked,prefix}) {
  const models=catalog.material_models || [];
  const ref=element.index_reference;
  const model=models.find(row=>row.key===ref?.model);
  const pinned=Boolean(ref?.model);
  const resolved=reading && reading.model===ref?.model && reading.wavelength_nm===wavelengthNm;
  const sameWave=!ref || ref.wavelength_nm===wavelengthNm;
  const n=resolved ? reading.n : sameWave ? element.n : null;
  const label=catalog.parameters.n;
  const range=model?.valid_range_nm;
  const outside=range && wavelengthNm!=null && (wavelengthNm<range[0] || wavelengthNm>range[1]);
  const frozenRange=stockBound && models.find(item=>item.key===frozenModel)?.valid_range_nm;
  const outsideFrozen=frozenRange && wavelengthNm!=null && (wavelengthNm<frozenRange[0] || wavelengthNm>frozenRange[1]);
  const choices=stockBound ? models.filter(item=>item.key===(frozenModel || ref?.model)) : models;
  const useFrozen=Boolean(stockBound && frozenModel && !pinned && frozenRange && wavelengthNm!=null && !outsideFrozen);
  const modelOption=row=><option key={row.key} value={row.key}>{row.name}{indexDirections[row.index_basis] && ' · '+indexDirections[row.index_basis]}</option>;
  const modelLocked=(stockBound && !outsideFrozen && !(frozenModel && !pinned)) || locked('n') || locked('index_reference');
  return <div className="op-index-source">
    <label>折射率依据<select aria-label={prefix+'折射率依据'} value={ref?.model || ''} disabled={modelLocked}
      onChange={e=>change('index_reference',{model:e.target.value||null,wavelength_nm:wavelengthNm??null})}>
      <option value="">填写该波长的 n</option>
      {pinned && !model && <option value={ref.model}>{ref.model} · 工程已存模型</option>}
      <optgroup label="标量介质">{choices.filter(row=>row.index_basis==='scalar').map(modelOption)}</optgroup>
      <optgroup label="指定偏振或晶轴">{choices.filter(row=>row.index_basis!=='scalar').map(modelOption)}</optgroup>
    </select></label>
    <label>{label}<input aria-label={prefix+label} data-element-field="n" type="number" min="1" max="5" step="any"
      readOnly={useFrozen || (pinned && (ref.model!=='air' || modelLocked))} disabled={locked('n')} value={n??''}
      placeholder={outside?'超出模型范围':wavelengthNm==null?'待补工作波长':pinned?'按工作波长计算':'填写当前波长下的 n'}
      onChange={event=>change('n',event.target.value===''?null:Number(event.target.value))}/></label>
    {model && <small>{outside?'波长超出模型范围 · ':''}{model.source}
      {range?.length===2 && ' · '+range.map(v=>Number(v.toPrecision(7))).join('–')+' nm'}
      {model.confidence==='representative' && ' · 代表性参数，需按样品核对'}</small>}
    {resolved && Number.isFinite(reading.reference_axis_opl_mm) && <small>本段轴上单程相位光程 · {Number(reading.reference_axis_opl_mm.toPrecision(8))} mm</small>}
    {useFrozen && <small>库存材料在当前波长已有目录值。请在折射率依据中恢复 {choices[0]?.name || frozenModel} 后保存。</small>}
    {!pinned && ref && !sameWave && <small>请补充 {wavelengthNm!=null?wavelengthNm+' nm':'新工作波长'} 下的折射率。</small>}
  </div>;
}

export default function OpticalElementFields({catalog, element, wavelengthNm, materialReading, stockBound, frozenModel, change, locked = () => false, nameLabel = '元件名称', prefix = '', matrixPrefix = '', className}) {
  const device = installedDevice(catalog.devices, element.prescription);
  const aperture = element.clear_aperture;
  return <>
    <label>元件名称<input aria-label={nameLabel} value={element.name} disabled={locked('name')}
      onChange={event => change('name', event.target.value)} /></label>
    <div className={className}>{catalog.models[element.kind]?.parameters.map(key => {
      const label = catalog.parameters[key];
      if(key==='n' && element.kind==='space')return <IndexFields key={key} catalog={catalog} element={element}
        wavelengthNm={wavelengthNm} reading={materialReading} stockBound={stockBound} frozenModel={frozenModel} change={change} locked={locked} prefix={prefix}/>;
      const indexExpired=key==='n' && element.index_reference && element.index_reference.wavelength_nm!==wavelengthNm;
      return <label key={key}>{label}<input aria-label={prefix + label} data-element-field={key} type="number" step="any"
        disabled={locked(key)} value={indexExpired ? '' : element[key] ?? ''}
        placeholder={indexExpired ? '请填写新波长的折射率' : key.includes('radius') ? '空白 = 平面' : ''}
        onChange={event => change(key, event.target.value === '' ? null : Number(event.target.value))} /></label>;
    })}</div>
    {element.kind === 'matrix' && <RayMatrixFields element={element} locked={locked} change={change} prefix={matrixPrefix}/>}
    {device?.length_input && <label>{device.inputs.properties[device.length_input].title || '参考面间距 / mm'}<input type="number" step="any"
      min="0" value={element.length_mm ?? ''} disabled={locked('length_mm')} onChange={e=>change('length_mm',e.target.value===''?null:Number(e.target.value))}/></label>}
    {element.prescription && element.length_mm > 0 && <small>两参考面相距 {element.length_mm} mm。模型只给出入口与出口的光束，图中留出内部区间。</small>}
    {element.prescription && <DeviceParameters key={element.uid} devices={catalog.devices} recipe={element.prescription}
      disabled={locked('prescription')} onChange={value=>change('prescription',value)}/>}
    <details className="bench-aperture"><summary>通光口径{aperture ? ' · '+(aperture.shape==='rectangle'?'矩形':'椭圆')+' '+(aperture.width_mm??'…')+' × '+(aperture.height_mm??aperture.width_mm??'…')+' mm' : ' · 未指定'}</summary>
      <fieldset disabled={locked('clear_aperture')}><label>入射面有效开口<select aria-label={prefix+'入射面有效开口'} value={aperture?.shape??''}
        onChange={e=>change('clear_aperture',e.target.value?{...(aperture || {width_mm:1}),shape:e.target.value}:null)}>
        <option value="">无限孔径模型</option><option value="ellipse">圆或椭圆</option><option value="rectangle">矩形</option></select></label>
      {aperture && <><div className={className}>{[
        ['width_mm','开口全宽 / mm'],['height_mm','开口全高 / mm'],['center_t_mm','孔心 T / mm'],
        ['center_s_mm','孔心 S / mm'],['angle_deg','从 T 向 S 转角 / °'],
      ].map(([key,label])=><label key={key}>{label}<input type="number" step="any" aria-label={prefix+label}
        placeholder={key==='height_mm'?'同全宽':key==='width_mm'?'必填':'0'} value={aperture[key]??''}
        onChange={e=>{const next={...aperture}; if(e.target.value==='')delete next[key];else next[key]=Number(e.target.value);change('clear_aperture',next);}}/></label>)}</div>
        <small>尺寸取垂直于展开光轴的入口投影。圆孔填写直径，全高留空。</small></>}</fieldset>
    </details>
  </>;
}
