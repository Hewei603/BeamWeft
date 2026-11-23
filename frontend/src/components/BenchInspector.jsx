import React, {useEffect, useId, useLayoutEffect, useRef, useState} from 'react';

// On a narrow screen the same editor docks below the live plot. Focusing the
// panel rather than a number input leaves the software keyboard under user control.
export function useBenchInspector() {
  const panel=useRef(null), origin=useRef(null);
  const [opened,setOpened]=useState(false);
  const small='(max-width: 760px)';
  const id=useId();
  const [narrow,setNarrow]=useState(()=>window.matchMedia(small).matches);
  const [collapsed,setCollapsed]=useState(false);
  const open=trigger=>{
    if(!window.matchMedia(small).matches){setCollapsed(false);return;}
    origin.current=trigger || document.activeElement;
    setOpened(true);
    origin.current?.closest('.bench-content,.network-bench')?.scrollIntoView({block:'start'});
  };
  const close=()=>{
    setOpened(false);
    if(origin.current?.isConnected)origin.current.focus({preventScroll:true});
  };
  useLayoutEffect(()=>{
    if(opened && panel.current){panel.current.scrollTop=0;panel.current.focus({preventScroll:true});}
  },[opened]);
  useEffect(()=>{
    const q=window.matchMedia(small);
    const resized=()=>{setNarrow(q.matches);if(!q.matches)setOpened(false);};
    q.addEventListener('change',resized);
    return ()=>q.removeEventListener('change',resized);
  },[]);
  const toggle=trigger=>{
    if(narrow){if(opened)close();else open(trigger);}
    else setCollapsed(value=>!value);
  };
  return {id,opened,open,close,narrow,toggle,expanded:narrow?opened:!collapsed,dismiss:()=>setOpened(false),props:{id,ref:panel,tabIndex:-1,
    'data-docked':opened || undefined,
    'data-inspector-hidden':!narrow && collapsed || undefined,
    onKeyDown:e=>{if(opened && e.key==='Escape'){e.preventDefault();close();}}}};
}

export function InspectorToggle({inspector, label}) {
  return <button type="button" className="bench-open-inspector" aria-expanded={inspector.expanded} aria-controls={inspector.id}
    onClick={event=>inspector.toggle(event.currentTarget)}>{inspector.narrow ? label : (inspector.expanded?'收起':'显示')+label}</button>;
}

export function InspectorHeading({inspector,title}) {
  return inspector.opened && <div className="bench-inspector-bar"><strong>{title}</strong>
    <button type="button" onClick={inspector.close}>收起属性</button></div>;
}
