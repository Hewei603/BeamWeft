import {profileInView,transverseExtent} from './plotViewport.js';

const entire = {start:-Infinity, end:Infinity};

export function offAxisDirection(ray,axis) {
  const prefix=axis==='tangential'?'t':'s';
  return ['offset_mm','angle_mrad'].some(key=>{
    const value=ray?.[prefix+'_'+key];
    return Number.isFinite(value) && value!==0;
  });
}

export function initialEngineeringView(record) {
  const fallback={projection:'width',pathUid:null};
  if (!record?.project || !record.analysis) return fallback;
  const {project,analysis}=record;
  const branches=project.incident_paths || [];
  const candidates=[{uid:null,ray:project.system.source?.launch_ray,axes:analysis.planes,
    fallback:analysis.primary_center_axes},...branches.map(path=>({uid:path.uid,ray:path.launch_ray,
    axes:analysis.sections?.paths?.find(trace=>trace.uid===path.uid)?.axes}))];
  // Prefer the primary beam, then branches in project order. T precedes S
  // when both have specified misalignment and archived ray evidence.
  for (const candidate of candidates) for (const axis of ['tangential','sagittal']) {
    const plane=candidate.axes?.[axis] ?? candidate.fallback?.[axis];
    if (offAxisDirection(candidate.ray,axis) && plane?.center_profile?.length) {
      return {projection:axis,pathUid:candidate.uid};
    }
  }
  return fallback;
}

// Sample two piecewise-linear traces only on spans supported by both models.
// Their knot positions can differ (e.g. geometric ray vs sampled Gaussian waist).
function envelopePart(center, radius, start, end) {
  const upper=[], lower=[];
  let ci=0, ri=0;
  const sample=(part,index,z)=>{
    const [a,v]=part[index];
    if (a===z) return v;
    const [b,w]=part[index+1];
    return v+(w-v)*((z-a)/(b-a));
  };
  let z=start;
  for (;;) {
    while (ci+1<center.length && center[ci+1][0]<=z) ci++;
    while (ri+1<radius.length && radius[ri+1][0]<=z) ri++;
    const c=sample(center,ci,z), r=sample(radius,ri,z);
    upper.push([z,c+r]);
    lower.push([z,c-r]);
    if (z===end) return {upper,lower};
    z=Math.min(end,center[ci+1]?.[0]??Infinity,radius[ri+1]?.[0]??Infinity);
  }
}

// A face-only result at an axial discontinuity has no unique pairing if
// either quantity has two values. Connected spans determine their own sides.
function uniqueFace(parts,i,z) {
  const value=parts[i][0][0]===z?parts[i][0][1]:parts[i].at(-1)[1];
  const adjacent=[parts[i-1]?.at(-1),parts[i+1]?.[0]];
  return adjacent.every(p=>!p || p[0]!==z || p[1]===value);
}

export function engineeringProfile(plane) {
  const centers=profileInView(plane?.center_profile || [],plane?.center_profile_breaks,entire);
  const widths=profileInView(plane?.profile || [],plane?.profile_breaks,entire);
  const bands=[];
  for (let ci=0,ri=0; ci<centers.length && ri<widths.length;) {
    const c=centers[ci], r=widths[ri];
    const ce=c.at(-1)[0], re=r.at(-1)[0];
    const start=Math.max(c[0][0],r[0][0]), end=Math.min(ce,re);
    if (start<end || (start===end && uniqueFace(centers,ci,start) && uniqueFace(widths,ri,start))) {
      bands.push(envelopePart(c,r,start,end));
    }
    if (ce<=re) ci++;
    if (re<=ce) ri++;
  }
  return {centers,bands,extent:engineeringExtent(centers,bands)};
}

function engineeringExtent(centers,bands) {
  return transverseExtent([...centers,...bands.flatMap(({upper,lower})=>[upper,lower])]);
}

export function engineeringInView(profile, view) {
  const centers=profile.centers.flatMap(segment=>profileInView(segment,[],view));
  const bands=[];
  for (const band of profile.bands) {
    const [upper]=profileInView(band.upper,[],view);
    if (upper) {
      // Both sides have exactly the same axial knots, including isolated faces.
      const [lower]=profileInView(band.lower,[],view);
      bands.push({upper,lower});
    }
  }
  return {...profile,centers,bands,extent:engineeringExtent(centers,bands)};
}
