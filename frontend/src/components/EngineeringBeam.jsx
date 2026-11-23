import React from 'react';
import {displayProfile,positionText} from './plotViewport';

export default function EngineeringBeam({profile,axis,uid,layer,x,y,tolerance}) {
  const coords=points=>points.map(([z,h])=>x(z).toFixed(2)+','+y(h).toFixed(2)).join(' ');
  const centers=profile.centers.map(segment=>displayProfile(segment,tolerance));
  if (!centers.length) return null;
  return <g className={'bench-engineering '+(uid==='primary'?'primary':'incident')+' '+layer}
    data-engineering-beam={uid} data-axis={axis} data-layer={layer}>
    {profile.bands.map(({upper,lower},i)=>{
      const top=displayProfile(upper,tolerance), bottom=displayProfile(lower,tolerance);
      if (top.length===1) return <line key={i} className="bench-projected-width" x1={x(top[0][0])} x2={x(top[0][0])}
        y1={y(bottom[0][1])} y2={y(top[0][1])}/>;
      return <g key={i}>{layer==='current' && <polygon points={coords(top)+' '+coords([...bottom].reverse())}/>}
        <polyline className="bench-projected-width" points={coords(top)}/>
        <polyline className="bench-projected-width" points={coords(bottom)}/></g>;
    })}
    {centers.map((segment,i)=>segment.length===1
      ? <circle key={i} className="bench-ray-center" cx={x(segment[0][0])} cy={y(segment[0][1])} r="3">
        <title>z = {positionText(segment[0][0])} mm · 束心 {positionText(segment[0][1])} mm</title></circle>
      : <polyline key={i} className="bench-ray-center" points={coords(segment)}/>)}
  </g>;
}
