import {inputWidget} from '../brand.js';
import React, {useId} from 'react';
import StudyObjectInput from './StudyObjectInput';
import OriginalFileInput,{fileRef} from './OriginalFileInput';
import {choiceLabel, objectInputSchema} from './studyObjectSchema';
import {choiceValue} from './useLayoutStudyChoices';

export function shape(field) {
  const options=field.anyOf?.filter(s=>s.type!=='null');
  return options?.length===1 ? {...field,...options[0]} : field;
}
export function originalField(schema, field) {
  const spec=shape(field);
  const target=schema.$defs?.[spec.$ref?.split('/').at(-1)] || spec;
  if(inputWidget(target)==='original-file')return 'single';
  if(target.type==='array' && target.items){
    const item=shape(target.items);
    const entry=schema.$defs?.[item.$ref?.split('/').at(-1)] || item;
    if(inputWidget(entry)==='original-file')return 'list';
  }
  return null;
}
function numericSequence(field) {
  const numeric=item=>['integer','number'].includes(item?.type);
  return field.type==='array' && (numeric(field.items) || (field.prefixItems?.length && field.prefixItems.every(numeric)));
}
export function valueFor(field, raw) {
  if (field.interval) {
    const parts=raw.trim().split(/[\s,，]+/);
    const numbers=raw.trim().startsWith('[') ? JSON.parse(raw) : parts.map(p=>p ? Number(p) : NaN);
    if (!Array.isArray(numbers) || !numbers.length || numbers.length>2 || !numbers.every(Number.isFinite) ||
        (numbers.length===2 && numbers[0]>numbers[1])) throw Error('请填一个数或从小到大的两个界限');
    return numbers.length===1 ? [numbers[0],numbers[0]] : numbers;
  }
  if (field.enum) return field.enum.find(item=>JSON.stringify(item)===raw);
  if (field.type==='string') return raw;
  if (field.type==='boolean') return raw==='true';
  if (['number','integer'].includes(field.type)) {
    const n=Number(raw);
    if (!Number.isFinite(n)) throw Error('需要有限数值');
    return n;
  }
  if (numericSequence(field) && !raw.trim().startsWith('[')) {
    const cells=raw.trim().split(/[,，]/.test(raw) ? /[,，]/ : /\s+/);
    if (cells.some(v=>!v.trim() || !Number.isFinite(Number(v)))) throw Error('请逐项输入数值，保留每个测点');
    return cells.map(Number);
  }
  if (field.type==='array' && field.items?.type==='string' && !raw.trimStart().startsWith('[')) {
    const lines=raw.split(/\r\n|\r|\n/);
    if (lines.at(-1)==='') lines.pop();
    return lines;
  }
  return JSON.parse(raw);
}
export function editValues(study, values) {
  return Object.fromEntries(Object.entries(values).map(([k,v])=>{
    const field=shape(study.inputs.properties[k] || {});
    return [k, v==null ? '' : field.enum || typeof v==='object' ? JSON.stringify(v) : String(v)];
  }));
}
function OriginalList({title,refs,files,onChange,disabled,min,max,id}) {
  const write=next=>onChange(JSON.stringify(next));
  const move=(index,direction)=>{
    const next=[...refs], other=index+direction;
    [next[index],next[other]]=[next[other],next[index]];
    write(next);
  };
  return <fieldset className="study-original-list" disabled={disabled} aria-label={title}>
    {refs.length>0 && <ol>{refs.map((ref,i)=>{
      const file=files.find(f=>f.id===ref.attachment_id);
      return <li key={i}><span>{file?.relative_path || file?.filename || '原件 #'+ref.attachment_id}</span><div>
        <button type="button" aria-label={`向前移动第 ${i+1} 份原件`} disabled={i===0} onClick={()=>move(i,-1)}>前移</button>
        <button type="button" aria-label={`向后移动第 ${i+1} 份原件`} disabled={i===refs.length-1} onClick={()=>move(i,1)}>后移</button>
        <button type="button" aria-label={`移除第 ${i+1} 份原件`} onClick={()=>write(refs.filter((_,j)=>j!==i))}>移除</button>
      </div></li>;
    })}</ol>}
    <select id={id} aria-label={'向'+title+'添加原件'} value="" required={refs.length<(min??0)} disabled={max!=null && refs.length>=max} onChange={e=>{
      const file=files.find(item=>item.id===Number(e.target.value));if(file)write([...refs,fileRef(file)]);
    }}><option value="">{files.length?'按所需顺序添加原件':'请先归档原件'}</option>
      {files.map(f=><option key={f.id} value={f.id}>{f.relative_path || f.filename} · #{f.id}</option>)}</select>
  </fieldset>;
}
export function InputField({name,field:original,value,required,onChange,port,schema,files=[],disabled=false,markOptional=true,onBlur,inheritedTitle,choices}) {
  const field=shape(original), title=original.title || name, id=useId();
  const props={id,name,disabled,onBlur,value:value??'',required,onChange:e=>onChange(e.target.value)};
  let control;
  const originalKind=originalField(schema,original);
  const objectSchema=!originalKind && port?.shape!=='interval' && objectInputSchema(original,schema);
  if(!choices && objectSchema) return <div className="study-input study-object-field"><StudyObjectInput title={title} config={objectSchema}
    value={value} files={files} required={required} disabled={disabled} description={original.description} onChange={onChange} onBlur={onBlur}/></div>;
  if(choices){
    const selected=choiceValue(inputWidget(original),value);
    const missing=selected && !choices.some(item=>item.value===selected);
    control=<select {...props} value={selected}><option value="">{choices.length?(required?'请选择':'未指定'):'所选光路中没有此类对象'}</option>
      {missing && <option value={selected}>{selected}（所选光路中缺失）</option>}
      {choices.map(item=><option key={item.value} value={item.value}>{item.label}</option>)}</select>;
  }else if(originalKind==='list'){
    let refs;
    try{refs=JSON.parse(value || '[]');}catch{/* Preserve raw input for correction below. */}
    control=Array.isArray(refs) && refs.every(r=>Number.isInteger(r?.attachment_id))
      ? <OriginalList title={title} refs={refs} files={files} onChange={onChange} id={id} min={field.minItems} max={field.maxItems} disabled={disabled}/>
      : <textarea {...props} rows={3} spellCheck={false}/>;
  }else if (originalKind==='single') {
    let ref=null;
    try {ref=JSON.parse(value || 'null');} catch { /* A raw draft can be corrected by selecting an original. */ }
    control=<OriginalFileInput id={id} name={name} required={required} disabled={disabled} onBlur={onBlur}
      files={files} reference={ref} onSelect={next=>onChange(next?JSON.stringify(next):'')}/>;
  } else if (port?.shape==='interval') {
    control=<input {...props} type="text" placeholder="单个数值，或 下限, 上限"/>;
  } else if (field.enum || field.type==='boolean') {
    control=<select {...props}><option value="">{required?'请选择':'未指定'}</option>{(field.enum || [true,false]).map(v=>
      <option value={JSON.stringify(v)} key={JSON.stringify(v)}>{choiceLabel(field,v)}</option>)}</select>;
  } else if (['number','integer','string'].includes(field.type)) {
    control=<input {...props} type={field.type==='string'?'text':'number'} step={field.type==='integer'?1:'any'}
      min={field.minimum} max={field.maximum} minLength={field.minLength} maxLength={field.maxLength}/>;
  } else {
    const numericList=numericSequence(field);
    const hint = field.type==='array' && field.items?.type==='string' ? '每行一项，或粘贴 JSON 字符串数组' : '输入 JSON';
    control=<textarea {...props} rows={numericList?2:4} spellCheck={false} placeholder={numericList?'用空格或逗号分隔数值，或粘贴 JSON 数组':hint}/>;
  }
  return <div className="study-input"><label htmlFor={id}>{title}{!required && markOptional?'（可选）':''}</label>{control}
    {inheritedTitle && <small>留空沿用「{inheritedTitle}」中的值。</small>}
    {original.description && <small>{original.description}</small>}</div>;
}
