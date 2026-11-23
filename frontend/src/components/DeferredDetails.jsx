import React, {memo, useState} from 'react';

export function DeferredDetails({title, defaultOpen=false, onOpen, className, children}) {
  const [open,setOpen]=useState(defaultOpen);
  return <details open={open} className={className} onToggle={event=>{
    if (event.target!==event.currentTarget) return;
    const visible=event.currentTarget.open;
    setOpen(visible);
    if (visible) onOpen?.();
  }}>
    <summary>{title}</summary>
    {open && (typeof children === 'function' ? children() : children)}
  </details>;
}

export const JsonText=memo(function JsonText({value}) {
  return <pre>{JSON.stringify(value,null,2)}</pre>;
});

export function JsonDetails({title,value,defaultOpen=false}) {
  return <DeferredDetails key={defaultOpen ? 'raw' : 'more'} title={title} defaultOpen={defaultOpen}>
    <JsonText value={value}/>
  </DeferredDetails>;
}
