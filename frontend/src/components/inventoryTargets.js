import {assemblyAt,materialSpace} from './benchGeometry.js';
import {portsOf} from './layoutSelections.js';

const unique = values=>[...new Set(values.filter(v=>v!=null && v!==''))];

export function frozenIndexModel(project,pathUid,elementUid) {
  const context=pathUid==='primary'?project.component_context:project.component_context?.path_contexts?.[pathUid];
  for (const item of Object.values(context?.components || {})) {
    const projection=item.medium_projection;
    const index=projection?.element_uids?.indexOf(elementUid) ?? -1;
    if(index>=0)return projection.sections?.[index]?.index_model ?? null;
  }
  return null;
}

function stockMount(entries) {
  if (entries.every(({part,element})=>part?.role==='crystal' && element.kind==='space')) return 'gain_crystal';
  const kind=entries[0].element.kind;
  const roles=unique(entries.map(({part})=>part?.role));
  if (roles.includes('crystal') || roles.length>1) return null;
  const endMirror=roles.some(role=>['pump_input','laser_output'].includes(role));
  if (entries.length===1 && (kind==='mirror' || (kind==='lens' && !endMirror))) return kind;
  if (endMirror) return null;
  return entries.every(({element,part})=>element.length_mm>0 && !element.clear_aperture && materialSpace(element,part))
    ? 'medium' : null;
}

export function engineeringObjects(project) {
  if (!project) return [];
  const scopes = [{uid:'primary',name:'主光路',elements:project.system.elements,parts:project.parts},
    ...(project.incident_paths || [])];
  const pathObjects = scopes.flatMap(path=>{
    const groups = new Map();
    const arm = {system:{elements:path.elements}, pathUid:path.uid, assemblies:project.assemblies};
    path.elements.forEach((element,index)=>{
      const part = path.parts?.find(p=>p.element_index===index);
      const assembly = assemblyAt(arm,index);
      const key = JSON.stringify([path.uid,assembly?'assembly':'element',assembly?.uid ?? element.uid ?? index]);
      if (!groups.has(key)) groups.set(key,{key,pathUid:path.uid,pathName:path.name,role:part?.role,
        title:assembly?.name || element.name,entries:[]});
      groups.get(key).entries.push({element,index,part});
    });
    return [...groups.values()].map(group=>{
      const {entries}=group;
      const category = stockMount(entries);
      return {...group,category,inventoryIds:unique(entries.map(e=>e.part?.inventory_id)),
        sources:unique(entries.map(e=>e.part?.source)),unknowns:unique(entries.flatMap(e=>e.part?.unknowns || []))};
    });
  });
  const portObjects = (project.network?.components || []).map(component => ({
    key:JSON.stringify(['network',component.uid]), title:component.name, component,
    ports:portsOf(component), inventoryIds:[],
  }));
  return [...pathObjects, ...portObjects];
}

export function objectReferences(object, revisionId) {
  const refs = revisionId == null ? {followCanvas:true} : {project_revision_id:revisionId};
  if (object.component) {
    return {...refs, project_objects:object.ports.map(port => ({component:object.component.uid, port}))};
  }
  return {...refs, inventory_ids:object.inventoryIds, project_objects:object.entries
    .filter(({element}) => element.uid)
    .map(({element}) => ({path_uid:object.pathUid, element_uid:element.uid}))};
}

export function findStockObject(objects, pathUid, elementUid) {
  return elementUid && objects.find(o=>o.pathUid===pathUid && o.entries?.some(e=>e.element.uid===elementUid));
}
