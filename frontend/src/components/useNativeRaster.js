import {useEffect,useState} from 'react';
import {workbenchApi} from '../api/workbench';

// The archived overview remains visible while the selected physical extent resolves.
export function useNativeRaster(caseId,recordId,digest,view,maxSide){
  const [reply,setReply]=useState(null),[attempt,setAttempt]=useState(0);
  const xMin=view && Math.min(...view.x), xMax=view && Math.max(...view.x);
  const yMin=view && Math.min(...view.y), yMax=view && Math.max(...view.y);
  const key=JSON.stringify([caseId,recordId,digest,maxSide,xMin,xMax,yMin,yMax,attempt]);
  useEffect(()=>{
    if(!digest)return;
    const controller=new AbortController();
    const timer=setTimeout(async()=>{
      const bounds=xMin==null?{}:{x_min_mm:xMin,x_max_mm:xMax,y_min_mm:yMin,y_max_mm:yMax};
      try{
        const data=await workbenchApi.nativeGridView(caseId,recordId,{...bounds,max_side:maxSide},controller.signal);
        if(data.grid_sha256!==digest)throw new Error('返回网格的指纹与当前计算记录不符。');
        if(!controller.signal.aborted)setReply({key,data});
      }catch(error){if(!controller.signal.aborted)setReply({key,error:error.message});}
    },xMin==null?0:110);
    return ()=>{clearTimeout(timer);controller.abort();};
  },[caseId,recordId,digest,maxSide,xMin,xMax,yMin,yMax,attempt,key]);
  const current=digest && reply?.key===key ? reply : null;
  return {data:current?.data,error:current?.error,loading:digest && !current,retry:()=>setAttempt(n=>n+1)};
}
