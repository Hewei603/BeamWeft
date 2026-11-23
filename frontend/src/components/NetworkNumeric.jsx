import React, {createContext,useContext,useId,useLayoutEffect,useRef,useState} from 'react';
import {flushSync} from 'react-dom';

export const NetworkFieldContext=createContext(null);

function NumberEntry({label,value,change,min,max,optional=false,placeholder,notify,owner}) {
  const draft=useRef(null),hintId=useId();
  const [editing,setEditing]=useState(null);
  const applied=value!=null?'当前计算使用 '+String(value)+'。':placeholder?'当前设置为「'+placeholder+'」。':'当前模型中此值待补。';
  const caption=[owner,label].filter(Boolean).join(' · ');
  const reverted=entry=>caption+' 的输入「'+entry.display+'」已撤回。'+applied;

  // A diagram selection or an upstream revision can replace a field without
  // firing blur. Keep the explanation with the old field's identity.
  useLayoutEffect(()=>()=>{if(draft.current)notify(draft.current.onLeave);},[notify]);

  function describe(el) {
    const display=el.value || (el.validity.badInput?'未完成的数值':'空值');
    let reason='';
    if(el.validity.badInput || (el.value!=='' && !Number.isFinite(el.valueAsNumber)))reason='请输入有限数值。';
    else if(!optional && el.value==='')reason='此项需要数值。';
    else if(el.validity.rangeUnderflow || el.validity.rangeOverflow)
      reason=min!=null && max!=null?'允许范围为 '+min+' 至 '+max+'。':min!=null?'最小值为 '+min+'。':'最大值为 '+max+'。';
    const entry={display,reason,next:el.value===''?null:el.valueAsNumber};
    entry.onLeave=reverted(entry);
    return entry;
  }

  function track(el) {
    const entry=describe(el);
    if(!entry.reason && entry.next===(value??null)) {
      draft.current=null;setEditing(null);notify('');return;
    }
    draft.current=entry;setEditing(entry);
    notify(caption+' 正在编辑。'+applied+entry.reason);
  }

  function settle(el,{discard=false,saveKey=false}={}) {
    const entry=draft.current;
    if(!entry)return;
    draft.current=null;setEditing(null);
    if(discard || entry.reason) {
      el.value=value??'';
      notify(entry.onLeave+(saveKey?' 再次按保存快捷键可保存当前工程。':''));
    } else {
      if(entry.next!==(value??null))change(entry.next);
      notify('');
    }
  }

  return <label>{label}<input aria-label={label} type="number" step="any" defaultValue={value??''} min={min} max={max}
    required={!optional} placeholder={placeholder??(optional?'待补':'')} aria-invalid={editing?.reason?true:undefined}
    aria-describedby={editing?hintId:undefined} onChange={e=>track(e.currentTarget)} onBlur={e=>settle(e.currentTarget)}
    onKeyDown={e=>{
      if((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='s' && draft.current) {
        if(draft.current.reason) {
          e.preventDefault();e.stopPropagation();settle(e.currentTarget,{saveKey:true});
        } else {
          // The project-file listener on window reads the current editor. Publish
          // the valid field before that same trusted shortcut opens the file picker.
          const field=e.currentTarget;
          flushSync(()=>settle(field));
        }
        return;
      }
      if(e.key==='Escape' && draft.current) {e.preventDefault();e.stopPropagation();settle(e.currentTarget,{discard:true});}
      if(e.key==='Enter'){e.preventDefault();if(!draft.current?.reason)e.currentTarget.blur();}
    }}/>
    {editing && <small id={hintId}>{editing.reason}{applied}{editing.reason?'可继续修改，离开字段将恢复。':'按 Enter 或移开焦点可应用。'}</small>}
  </label>;
}

export default function Numeric({scope,...props}) {
  const fields=useContext(NetworkFieldContext);
  const {identity,owner}=fields[scope || 'terminal'];
  return <NumberEntry key={JSON.stringify([identity,props.label,props.value,props.placeholder])}
    {...props} owner={owner} notify={fields.notify}/>;
}
