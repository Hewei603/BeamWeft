import React, {useLayoutEffect,useRef,useState} from 'react';
import {maxNetworkScale,networkView,readableScale,zoomNetworkView} from './networkCamera';
import {usePointerGesture} from './usePointerGesture';

export default function NetworkViewport({svg, bounds, focus, locateRequest, frozen, children}) {
  const [size,setSize]=useState({width:600,height:280});
  const [camera,setCamera]=useState(null);
  const [initialFocus]=useState(focus);
  const pointer=usePointerGesture(false);
  const previous=useRef(focus?.uid);
  useLayoutEffect(()=>{
    const el=svg.current;
    const resize=()=>{
      const {width,height}=el.getBoundingClientRect();
      if(width && height)setSize(old=>old.width===width && old.height===height ? old : {width,height});
    };
    resize();
    const observer=new ResizeObserver(resize);observer.observe(el);
    return ()=>observer.disconnect();
  },[svg]);
  const view=networkView(bounds,size,camera,initialFocus);
  useLayoutEffect(()=>{
    const element=svg.current;
    const wheel=event=>{
      if(frozen || pointer.active.current || !(event.ctrlKey || event.metaKey))return;
      event.preventDefault();
      const rect=element.getBoundingClientRect();
      const x=(event.clientX-rect.left)/rect.width,y=(event.clientY-rect.top)/rect.height;
      const units=event.deltaMode===1 ? 16 : event.deltaMode===2 ? rect.height : 1;
      const factor=Math.exp(-Math.max(-300,Math.min(300,event.deltaY*units))*.005);
      setCamera(current=>{
        const old=networkView(bounds,size,current,initialFocus);
        return zoomNetworkView(old,factor,{x:old.x+x*old.width,y:old.y+y*old.height});
      });
    };
    element.addEventListener('wheel',wheel,{passive:false});
    return ()=>element.removeEventListener('wheel',wheel);
  },[svg,bounds.x,bounds.y,bounds.width,bounds.height,size,initialFocus,frozen]);
  const locate=()=>focus && setCamera({x:focus.x,y:focus.y,scale:Math.max(readableScale,view.scale)});
  const outside=(x,y)=>x<view.x || x>view.x+view.width || y<view.y || y>view.y+view.height;
  // The inspector and the engineering object directory can both select a remote node.
  useLayoutEffect(()=>{
    if(!focus || previous.current===focus.uid)return;
    previous.current=focus.uid;
    if (!frozen && (view.scale<readableScale || outside(focus.x,focus.y))) {
      const id=requestAnimationFrame(locate);
      return ()=>cancelAnimationFrame(id);
    }
  },[focus?.uid]);
  useLayoutEffect(()=>{
    if(!locateRequest || !focus)return;
    const id=requestAnimationFrame(locate);
    return ()=>cancelAnimationFrame(id);
  },[locateRequest]);
  function pan(event) {
    if(frozen || event.target!==svg.current || view.scale<=view.fit)return;
    const start={x:event.clientX,y:event.clientY};
    if(pointer.beginDrag(event,svg.current,{
      move(e){setCamera({x:view.x+view.width/2+(start.x-e.clientX)/view.scale,
        y:view.y+view.height/2+(start.y-e.clientY)/view.scale,scale:view.scale});},
      finish(){},cancel(){setCamera(camera);},
    })) {event.preventDefault();svg.current.focus({preventScroll:true});}
  }
  function navigate(event) {
    if(frozen || pointer.active.current || event.altKey || event.ctrlKey || event.metaKey)return;
    const shifts={ArrowLeft:[-.3,0],ArrowRight:[.3,0],ArrowUp:[0,-.3],ArrowDown:[0,.3]};
    const shift=shifts[event.key];
    if(shift)setCamera({x:view.x+view.width*(.5+shift[0]),y:view.y+view.height*(.5+shift[1]),scale:view.scale});
    else if(event.key==='+' || event.key==='=')setCamera(zoomNetworkView(view,1.5));
    else if(event.key==='-')setCamera(zoomNetworkView(view,1/1.5));
    else if(event.key==='Home')setCamera({fit:true});
    else return;
    event.preventDefault();
  }
  function revealKeyboardTarget(event) {
    const el=event.target;
    if(frozen || el===svg.current || !el.matches(':focus-visible') || !el.getBBox)return;
    const box=el.getBBox(), x=box.x+box.width/2, y=box.y+box.height/2;
    if(outside(x,y))setCamera({x,y,scale:Math.max(readableScale,view.scale)});
  }
  const zoom=factor=>setCamera(zoomNetworkView(view,factor));
  return <><div className="network-view-actions" role="group" aria-label="端口图视野">
    <button type="button" aria-label="缩小端口图" disabled={view.scale<=view.fit} onClick={()=>zoom(1/1.5)}>−</button>
    <span aria-label="图面缩放比例">{Math.round(view.scale*100)}%</span>
    <button type="button" aria-label="放大端口图" disabled={view.scale>=maxNetworkScale} onClick={()=>zoom(1.5)}>＋</button>
    <button type="button" aria-label="定位所选网络元件" onClick={locate}>定位所选</button>
    <button type="button" aria-label="查看全部端口图" disabled={view.scale<=view.fit} onClick={()=>setCamera({fit:true})}>全图</button>
  </div><svg ref={svg} className="port-diagram" tabIndex={0} data-pannable={view.scale>view.fit || undefined}
    viewBox={frozen ?? [view.x,view.y,view.width,view.height].join(' ')} role="group" aria-label="光路端口与连接"
    onPointerDown={pan} onKeyDown={navigate} onFocusCapture={revealKeyboardTarget}>
    <title>拖动空白处或按方向键平移。加减键缩放，也可用 Ctrl 加滚轮围绕光标缩放。Home 查看全图。</title>
    {children}
  </svg></>;
}
