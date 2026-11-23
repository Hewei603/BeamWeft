import React, {useId, useState} from 'react';
import {choiceLabel, newInputRecord, objectInputValue} from './studyObjectSchema';
import OriginalFileInput from './OriginalFileInput';

function RecordField({name, field, value, files, onChange}) {
  const id = useId(), title = field.title || name;
  const choices = Object.hasOwn(field,'const') ? [field.const] : field.enum || (field.type==='boolean' ? [true,false] : null);
  const required = field.required;
  const hasDefault = Object.hasOwn(field,'default') && field.default != null;
  const placeholder = hasDefault ? '默认 '+choiceLabel(field,field.default) : '';
  function read(event) {
    const control=event.target;
    if (control.value === '') return onChange(undefined);
    if (choices) return onChange(JSON.parse(control.value));
    if (field.type==='number' || field.type==='integer') return onChange(control.valueAsNumber);
    onChange(control.value);
  }
  return <div className="study-object-cell">
    <label htmlFor={id}>{title}{!required && <span aria-hidden="true">（可选）</span>}</label>
    {field['x-laserclaw-input']==='original-file' ? <OriginalFileInput id={id} aria-label={title} required={required}
      reference={value} files={files} onSelect={onChange}/> : choices ? <select id={id} aria-label={title} value={value == null ? '' : JSON.stringify(value)} required={required} onChange={read}>
      <option value="">{placeholder || (required?'请选择':'未指定')}</option>{choices.map(choice=>
        <option key={JSON.stringify(choice)} value={JSON.stringify(choice)}>{choiceLabel(field,choice)}</option>)}
    </select> : <input id={id} aria-label={title} type={field.type==='string'?'text':'number'}
      step={field.type==='integer'?1:'any'} required={required} value={value ?? ''}
      min={field.minimum} max={field.maximum} minLength={field.minLength} maxLength={field.maxLength}
      placeholder={placeholder} onChange={read}/>}
    {field.description && <small>{field.description}</small>}
  </div>;
}

export default function StudyObjectInput({title,config,value,files,required,disabled,onChange,onBlur,description}) {
  const [rawMode,setRawMode]=useState(false);
  const id=useId();
  const parsed=objectInputValue(config,value);
  const showRaw=rawMode || Boolean(parsed.error);
  const rows=config.list ? (parsed.value ?? []) : parsed.value == null ? [] : [parsed.value];
  const present=parsed.value!=null;
  function write(rows) {onChange(JSON.stringify(config.list ? rows : rows[0]));}
  function changeCell(index,key,next) {
    const record={...rows[index],[key]:next};
    if (next === undefined) delete record[key];
    const updated=[...rows]; updated[index]=record; write(updated);
  }
  function move(index,direction) {
    const updated=[...rows];
    [updated[index],updated[index+direction]]=[updated[index+direction],updated[index]];
    write(updated);
  }
  function clear() {onChange('');}
  return <fieldset className="study-object-input" disabled={disabled}
    onBlur={event=>{if(!event.currentTarget.contains(event.relatedTarget)) onBlur?.(event);}}>
    <legend>{title}{!required && <span aria-hidden="true">（可选）</span>}</legend>
    {description && <small>{description}</small>}
    {showRaw ? <>
      <label htmlFor={id}>{title} JSON</label>
      <textarea id={id} rows={5} value={value ?? ''} required={required} spellCheck={false}
        onChange={e=>{setRawMode(true);onChange(e.target.value);}}/>
      {parsed.error && <small role="status">{parsed.error}</small>}
      <button type="button" disabled={Boolean(parsed.error)} onClick={()=>setRawMode(false)}>逐项编辑{title}</button>
    </> : <>
      {(config.list ? rows : (present || required ? [parsed.value || {}] : [])).map((row,index)=>
        <fieldset className="study-object-row" key={index}>
          {config.list && <legend>第 {index+1} 项</legend>}
          <div className="study-object-cells">{Object.entries(config.properties).map(([key,field])=>
            <RecordField key={key} name={key} field={field} value={row[key]} files={files} onChange={next=>changeCell(index,key,next)}/>)}</div>
          {config.list && <div className="study-object-actions">
            {rows.length>1 && <><button type="button" aria-label={'上移第 '+(index+1)+' 项'} disabled={!index} onClick={()=>move(index,-1)}>上移</button>
            <button type="button" aria-label={'下移第 '+(index+1)+' 项'} disabled={index===rows.length-1} onClick={()=>move(index,1)}>下移</button></>}
            <button type="button" aria-label={'移除第 '+(index+1)+' 项'} onClick={()=>write(rows.filter((_,i)=>i!==index))}>移除</button>
          </div>}
        </fieldset>)}
      <div className="study-object-actions">
        {config.list ? <button type="button" disabled={config.max!=null && rows.length>=config.max}
          onClick={()=>write([...rows,newInputRecord(config.properties)])}>添加一项</button> :
          !present && !required && <button type="button" onClick={()=>write([newInputRecord(config.properties)])}>填写{title}</button>}
        {!present && required && (config.list ? !config.min : Object.values(config.properties).every(field=>!field.required)) &&
          <button type="button" onClick={()=>write(config.list ? [] : [{}])}>{config.list?'使用空列表':'使用默认设置'}</button>}
        {present && !required && <button type="button" onClick={clear}>留空</button>}
        <button type="button" onClick={()=>setRawMode(true)}>编辑此项 JSON</button>
      </div>
      {config.list && (required || present) && rows.length<(config.min ?? 0) && <small>至少需要 {config.min} 项。</small>}
    </>}
  </fieldset>;
}
