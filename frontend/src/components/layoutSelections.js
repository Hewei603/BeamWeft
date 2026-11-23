import {inputWidget} from '../brand.js';
export const portsOf = component => component.model.ports ?? (component.model.kind === 'coupler' ? ['1','2','3','4'] : ['1']);

export function connectionAt(network, at) {
  const selected = terminal => terminal.component === at.component && terminal.port === at.port;
  const link = network.links?.find(edge => selected(edge.a) || selected(edge.b));
  return {link, peer:link && (selected(link.a) ? link.b : link.a),
    drive:network.drives?.find(source => selected(source.at))};
}

export const pathsOf = layout => [
  {uid:'primary', name:'主光路'},
  ...(layout.incident_paths || []),
];

export function pathFaces(geometry, beamUid) {
  const arm=geometry.routes.find(r=>r.path.uid===beamUid);
  const scoped=(rows,scope)=>rows.map(row=>({...row,scope}));
  if(beamUid==='primary')return scoped(geometry.positions,'host');
  return arm ? [...scoped(arm.rows,'arm'),...scoped(arm.shared,'host')] : [];
}

export function networkInputReferences(layout, geometry, sections) {
  const beams=componentLabels(pathsOf(layout));
  const planes=new Map((layout.observation_planes || []).map(p=>[p.uid,p]));
  const planeLabels=componentLabels(layout.observation_planes || []);
  const resolved=new Map((sections?.planes || []).map(p=>[p.uid,p]));
  function face(beamUid, ref) {
    const rows=pathFaces(geometry,beamUid);
    const incomplete=geometry.routes.some(r=>r.path.uid===beamUid && r.broken);
    const row=ref?rows.find(r=>r.scope===ref.scope && r.element.uid===ref.element_uid):!incomplete && rows.at(-1);
    const target=row && {pathUid:beamUid,scope:row.scope,elementUid:row.element.uid};
    return {beam:beams.get(beamUid),label:row?(row.index+1)+'. '+row.element.name+' 出射面':'原出射面待核对',target};
  }
  return (layout.network?.drives || []).flatMap(d=>{
    const rows=[];
    if(d.path_output)rows.push({quantity:'功率',...face(d.path_output.path_uid,d.path_output)});
    const f=d.profile;
    if(f?.kind==='layout') {
      if(f.plane_uid) {
        const p=planes.get(f.plane_uid);
        const elementUid=p?.target_uid ?? resolved.get(f.plane_uid)?.target_uid;
        rows.push({quantity:'光场',beam:beams.get(f.beam_uid),label:planeLabels.get(f.plane_uid) || '原观察面待核对',
          target:p && {pathUid:f.beam_uid,scope:'host',elementUid,planeUid:p.uid}});
      } else rows.push({quantity:'光场',...face(f.beam_uid,f.element_exit)});
    }
    return rows.length?[{at:d.at,rows}]:[];
  });
}

// A study names the kind of project object its input needs. The canvas
// supplies the choices and current selection independently of the solver.
export function orderedSelections(layout, selectedPath) {
  const choices = items => [...componentLabels(items)].map(([value,label])=>({value,label}));
  return {
    'path-uid': {label:'计算光路', choices:choices(pathsOf(layout)), current:selectedPath, follow:'跟随画布光路'},
    'plane-uid': {label:'取样面', choices:choices(layout.observation_planes || []), follow:'光路末端'},
  };
}

// In a comparison the same object kind can serve different experimental roles.
// A single canvas selection cannot supply both operands.
export function studyFieldSelections(schema,selections) {
  const fields=Object.entries(schema.properties).filter(([,field])=>selections[inputWidget(field)]);
  const counts=new Map();
  for(const [,field] of fields) {
    const kind=inputWidget(field);
    counts.set(kind,(counts.get(kind) || 0)+1);
  }
  return Object.fromEntries(fields.map(([key,field])=>{
    const kind=inputWidget(field),binding=selections[kind];
    if(counts.get(kind)===1)return [key,binding];
    const follow=field.default!=null?'方法默认':schema.required?.includes(key)?'请选择':'未指定';
    return [key,{...binding,label:field.title || key,current:field.default,follow}];
  }));
}

export function componentLabels(components) {
  const names=new Set(), repeated=new Set();
  for(const c of components){
    if(names.has(c.name))repeated.add(c.name);
    names.add(c.name);
  }
  return new Map(components.map((c,i)=>[c.uid,repeated.has(c.name)?(i+1)+' · '+c.name:c.name]));
}

export function networkSelections(layout, component, port, labels=componentLabels(layout.network?.components || [])) {
  const choices = (layout.network?.components || []).flatMap(c => portsOf(c).map(p => ({
    value:JSON.stringify({component:c.uid, port:p}), label:labels.get(c.uid)+' / '+p,
  })));
  return {'network-port': {label:'探测端口', choices, follow:'跟随选中端口',
    current:component && port ? JSON.stringify({component,port}) : undefined}};
}
