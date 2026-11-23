import {useEffect, useMemo, useState} from 'react';
import {workbenchApi} from '../api/workbench';
import {orderedSelections, networkSelections} from './layoutSelections';
import {sourceKey} from './studyConnections';

const objectKinds=new Set(['path-uid','plane-uid','network-port']);

function inlineObjects(raw) {
  try {
    const layout=typeof raw==='string'?JSON.parse(raw):raw;
    return Array.isArray(layout?.system?.elements)?layout:null;
  } catch {return null;}
}

export function choiceValue(kind, raw) {
  if(kind!=='network-port' || !raw)return raw || '';
  try {
    const at=JSON.parse(raw);
    if(at && Object.keys(at).length===2 && typeof at.component==='string' && typeof at.port==='string')
      return JSON.stringify({component:at.component,port:at.port});
  } catch { /* Show the original stored input until the user replaces it. */ }
  return raw;
}

export function useLayoutStudyChoices(caseId, definition, connections, manual, offeredState, ready) {
  const layouts=Object.entries(definition.input_ports || {}).filter(([,port])=>port.quantity==='optics.ordered-layout.v1' && port.shape==='state');
  const layoutKey=layouts.some(([name])=>name===definition.live_layout)?definition.live_layout:layouts.length===1?layouts[0][0]:null;
  const enabled=ready && layoutKey && Object.values(definition.inputs.properties).some(field=>objectKinds.has(field['x-laserclaw-input']));
  const reference=enabled && connections[layoutKey];
  const supplied=reference
    ? (offeredState?.reference && sourceKey(reference)===sourceKey(offeredState.reference)?offeredState.value:null)
    : (enabled?manual[layoutKey]:null);
  const inline=useMemo(()=>inlineObjects(supplied),[supplied]);
  const run=reference?.run_id, revision=reference?.revision_id, port=reference?.port;
  const key=!inline && port && (run || revision)?JSON.stringify([caseId,revision,run,port]):null;
  const [response,setResponse]=useState(null),[attempt,retry]=useState(0);
  useEffect(()=>{
    if(!key)return;
    const controller=new AbortController();
    const source=revision?{revision_id:revision,port}:{run_id:run,port};
    workbenchApi.layoutObjects(caseId,source,controller.signal).then(objects=>{
      if(!controller.signal.aborted)setResponse({key,objects});
    }).catch(()=>{
      if(!controller.signal.aborted)setResponse({key,error:true});
    });
    return ()=>controller.abort();
  },[key,caseId,port,revision,run,attempt]);
  const loaded=key && response?.key===key?response:null;
  const objects=inline || loaded?.objects;
  const choices=useMemo(()=>{
    if(!objects)return {};
    try {return {...orderedSelections(objects),...networkSelections(objects)};}
    catch {return {};} // Partially entered JSON stays editable through the original controls.
  },[objects]);
  return {choices,error:loaded?.error,retry:()=>retry(n=>n+1)};
}
