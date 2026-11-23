import React from 'react';

const identity = [1, 0, 0, 1];
const names = ['A', 'B / mm', 'C / mm⁻¹', 'D'];

export default function RayMatrixFields({ element, change, locked = () => false, prefix = '' }) {
  return <div className="ray-matrices">
    <p>近轴光线坐标为 (y, nθ)。B 用毫米，C 用每毫米。</p>
    {['matrix_t', 'matrix_s'].map((key, axis) => {
      const values = element[key];
      const label = axis ? '弧矢 S' : '切向 T';
      const complete = values?.length === 4 && values.every(Number.isFinite);
      const det = complete ? values[0] * values[3] - values[1] * values[2] : null;
      return <div key={key}>
        {axis === 1 && <label className="library-check"><input type="checkbox" aria-label={prefix + '单独填写弧矢 S 矩阵'} checked={values != null} disabled={locked(key)}
          onChange={e => change(key, e.target.checked ? [...(element.matrix_t || identity)] : null)} />单独填写弧矢 S 矩阵</label>}
        {(axis === 0 || values != null) && <fieldset><legend>{label}</legend><div className="matrix-fields">
          {names.map((name, i) => <label key={name}>{name}<input type="number" step="any" aria-label={prefix + label + ' ' + name}
            disabled={locked(key)} value={values?.[i] ?? ''} onChange={e => {
              const next = [...(values || identity)]; next[i] = e.target.value === '' ? null : Number(e.target.value);
              change(key, next);
            }} /></label>)}</div>
          <small role={det == null || Math.abs(det - 1) > 1e-8 ? 'status' : undefined}>
            {det == null ? '请填全四个系数。' : 'AD − BC = ' + Number(det.toPrecision(8)) + (Math.abs(det - 1) > 1e-8 ? '，请检查系数和单位，当前模型要求行列式为 1。' : '')}</small>
        </fieldset>}
      </div>;
    })}
  </div>;
}
