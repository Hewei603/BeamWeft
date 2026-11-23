import React, {useId, useLayoutEffect, useRef} from 'react';
import './BenchToolPanel.css';

// Keep the working prescription interactive while browsing its versions or reusable parts.
export default function BenchToolPanel({caseId, title, className, open, onOpenChange, children}) {
  const details=useRef(null), trigger=useRef(null), body=useRef(null);
  const titleId=useId(), panelId=useId();
  const close=restoreFocus=>{
    if (details.current) details.current.open=false;
    if (restoreFocus && trigger.current) {
      trigger.current.focus({preventScroll:true});
      const bounds=trigger.current.getBoundingClientRect();
      if(bounds.bottom<0 || bounds.top>window.innerHeight) trigger.current.scrollIntoView({block:'nearest'});
    }
  };
  useLayoutEffect(()=>{
    if (!open || !body.current) return;
    const owner=details.current.closest('.optical-bench');
    const toolbar=details.current.closest('.bench-toolbar');
    const narrow=window.matchMedia('(max-width: 760px)');
    if (narrow.matches) {
      const surface=owner.querySelector('.bench-content,.network-bench,.bench-empty-path');
      (surface || owner).scrollIntoView({block:'start'});
    }
    const size=event=>{
      if(event?.target instanceof Node && body.current?.contains(event.target)) return;
      const top=toolbar.getBoundingClientRect().bottom;
      const bottom=Math.min(window.innerHeight,owner.getBoundingClientRect().bottom);
      body.current?.style.setProperty('--panel-available',Math.max(1,bottom-top-4)+'px');
      const view=window.visualViewport;
      if(view && narrow.matches) {
        body.current?.style.setProperty('--panel-mobile-height',Math.round(view.height*.45)+'px');
        body.current?.style.setProperty('--panel-bottom',Math.max(0,window.innerHeight-view.height-view.offsetTop)+'px');
      }
    };
    size();
    body.current.focus({preventScroll:true});
    const resize=new ResizeObserver(size);
    resize.observe(toolbar);resize.observe(owner);
    const escape=event=>{
      if (event.key!=='Escape' || event.defaultPrevented || event.isComposing) return;
      event.preventDefault();
      close(true);
    };
    const changedWorkspace=event=>{
      if (!owner.contains(event.target) || event.target.closest('.bench-agent,.bench-conversation-host,.bench-open-inspector,[data-docked]'))
        close(false);
    };
    const offscreen=new IntersectionObserver(entries=>{
      if (!entries[0].isIntersecting) close(false);
    });
    offscreen.observe(owner);
    window.addEventListener('resize',size);
    window.addEventListener('scroll',size,true);
    window.visualViewport?.addEventListener('resize',size);
    window.visualViewport?.addEventListener('scroll',size);
    document.addEventListener('keydown',escape);
    document.addEventListener('focusin',changedWorkspace);
    document.addEventListener('pointerdown',changedWorkspace);
    return ()=>{
      resize.disconnect();offscreen.disconnect();
      window.removeEventListener('resize',size);
      window.removeEventListener('scroll',size,true);
      window.visualViewport?.removeEventListener('resize',size);
      window.visualViewport?.removeEventListener('scroll',size);
      document.removeEventListener('keydown',escape);
      document.removeEventListener('focusin',changedWorkspace);
      document.removeEventListener('pointerdown',changedWorkspace);
    };
  },[open]);
  return <details ref={details} name={'bench-tools-'+caseId} className={className+' bench-tool'} onToggle={e=>{
    if (e.target===e.currentTarget) onOpenChange(e.currentTarget.open);
  }}>
    <summary ref={trigger} id={titleId} aria-controls={panelId}>{title}</summary>
    {open && <div ref={body} id={panelId} className="bench-tool-body" tabIndex={-1} role="group" aria-labelledby={titleId}>
      <div className="bench-tool-heading"><strong>{title}</strong>
        <button type="button" onClick={()=>close(true)} aria-label={'收起'+title}>收起</button></div>
      {children}
    </div>}
  </details>;
}
