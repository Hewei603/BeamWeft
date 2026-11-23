import React from 'react';
import {FitOriginRecord} from './BeamStudy';
import {declaredRadiusBasis, setRadiusBasis} from './beamWidths';

export function Num({label,value,change, accessiblePrefix = ''}) {
  return <label>{label}<input type="number" step="any" value={value??''} aria-label={accessiblePrefix ? accessiblePrefix + label : undefined}
    onChange={e=>change(e.target.value===''?null:Number(e.target.value))}/></label>;
}

function AxisInputs({axis,set,prefix,accessiblePrefix}) {
  const edit=(key,v)=>set({...axis,[key]:v});
  const origin=axis.fit_origin;
  return <div className="section-fields"><Num label={prefix+'输入半径 / mm'} value={axis.radius_mm} change={v=>edit('radius_mm',v)} accessiblePrefix={accessiblePrefix}/>
    <label>{prefix}波前<select aria-label={accessiblePrefix+prefix+'波前'} value={axis.wavefront ?? 'unknown'}
      onChange={e=>set({...axis,wavefront:e.target.value,curvature_mm:e.target.value==='curved'?axis.curvature_mm??null:null})}><option value="unknown">尚未确定</option><option value="waist">源面在束腰</option><option value="curved">已知曲率</option></select></label>
    {(axis.wavefront==='curved' || axis.curvature_mm!=null) && <Num label={prefix+'波前 R / mm'} value={axis.curvature_mm} change={v=>edit('curvature_mm',v)} accessiblePrefix={accessiblePrefix}/>}
    {axis.wavefront!=='curved' && axis.curvature_mm!=null && <small>{prefix}方向仍登记了有限曲率。可清空 R 或选择“已知曲率”。</small>}
    <Num label={prefix+'M²'} value={axis.m2} change={v=>edit('m2',v)} accessiblePrefix={accessiblePrefix}/>
    <label>{prefix}输入半径约定<select aria-label={accessiblePrefix+prefix+'输入半径约定'} value={declaredRadiusBasis(axis)}
      onChange={e=>set(setRadiusBasis(axis,e.target.value))}>
      <option value="model">按光源模型判定</option><option value="second_moment">二阶矩 2σ 半径</option>
      <option value="equivalent">等效半径</option>
    </select></label>
    {origin && <FitOriginRecord key={origin.study_id} prefix={prefix} origin={origin}/>}</div>;
}

export function BeamSourceFields({source, change, accessiblePrefix = ''}) {
  const edit=(key,value)=>change({...source,[key]:value});
  return <>
    <AxisInputs prefix="T " accessiblePrefix={accessiblePrefix} axis={source.tangential} set={a=>edit('tangential',a)}/>
    <details><summary>源面位置与指向</summary><p>以空气中的源参考面为起点，沿展开光路的 T/S 轴给出偏移和小角度。默认沿光轴，清空一项表示该量未知。</p>
      <div className="section-fields">{['t','s'].flatMap(axis=>[['offset_mm','中心偏移 / mm'],['angle_mrad','指向 / mrad']].map(([key,text])=>{
        const field=axis+'_'+key, ray=source.launch_ray || {};
        return <Num key={field} label={axis.toUpperCase()+' '+text} accessiblePrefix={accessiblePrefix} value={Object.hasOwn(ray,field)?ray[field]:0}
          change={value=>edit('launch_ray',{...ray,[field]:value})}/>;
      }))}</div></details>
    <label className="section-inline"><input type="checkbox" aria-label={accessiblePrefix+'分别指定 S 轴光源参数'} checked={source.sagittal!=null} onChange={e=>edit('sagittal',e.target.checked?{...source.tangential}:null)}/>分别指定 S 轴光源参数</label>
    {source.sagittal && <AxisInputs prefix="S " accessiblePrefix={accessiblePrefix} axis={source.sagittal} set={a=>edit('sagittal',a)}/>}
  </>;
}
