import React from 'react';

const reading=value=>Number.isFinite(value)?String(Number(value.toPrecision(6))):'未知';

export default function RegistrationDetails({registration,frame='参考图'}) {
  const evidence=registration.fit_evidence;
  return <>
    <p>观测原点在{frame}的 ({reading(registration.offset_x_mm)}, {reading(registration.offset_y_mm)}) mm。
      {registration.flip_y?'先将观测纵轴反向，再':'保留观测纵轴方向，再'}按参考坐标旋转 {reading(registration.rotation_deg)}°。</p>
    <p>{registration.basis}</p>
    {registration.calibration_basis && registration.calibration_basis!==registration.basis &&
      <p>坐标标定依据 · {registration.calibration_basis}</p>}
    {evidence ? <div aria-label="标记点拟合与检验">
      <p>拟合标记 {evidence.fit_count} 点，最大位置差 {reading(evidence.fit_max_mm)} mm。</p>
      {evidence.check_count>0 ? <>
        <p>预留 {evidence.check_count} 点，其最大位置差为 {reading(evidence.check_max_mm)} mm
          {Number.isFinite(evidence.check_rms_mm) && '，均方根为 '+reading(evidence.check_rms_mm)+' mm'}。</p>
        <p>{evidence.check_new_location_count==null?'此记录未区分预留点是否位于拟合标记之外，核对点位后再评价其他位置的精度。':
          evidence.check_new_location_count===0?'预留点均重复了拟合位置，只检验了这些位置的重复性。其他位置的配准精度仍待检验。':
          '按登记的参考坐标，预留点覆盖了 '+evidence.check_new_location_count+' 个拟合标记之外的位置。请核对标记身份和定位精度，确认这些读数是否来自不同的实物位置。'}</p>
      </>:<p>预留 0 点。其他位置的配准精度仍待检验。</p>}
      {Number.isFinite(evidence.max_error_mm) && <p>本次标定允许的最大位置差为 {reading(evidence.max_error_mm)} mm。</p>}
    </div>:<p>这份坐标变换未附标记点拟合及检验误差。</p>}
  </>;
}
