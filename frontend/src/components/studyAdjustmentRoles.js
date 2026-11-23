export const adjustmentRoles={
  design:'方案参数（选定后固定）',
  observed:'调节前可辨别的工况',
  hidden:'调节前无法辨别的工况',
  control:'可调参数',
};

export function selectedDesignIndex(result, view={}) {
  const designs=result?.designs || [];
  const valid=index=>Number.isInteger(index) && index>=0 && index<designs.length;
  const locate=focus=>Array.isArray(focus)
    ? designs.findIndex(item=>item.design.length===focus.length && item.design.every((v,i)=>v===focus[i])) : -1;
  const chosen=locate(view.focus_design);
  if (chosen>=0) return chosen;
  if(valid(view.designIndex)) return view.designIndex;
  const opened=locate(result?.focus_design);
  if(opened>=0) return opened;
  return valid(result?.designIndex)?result.designIndex:0;
}

export function adjustmentView(result) {
  const view={mode:'adjustment',roles:result.roles};
  if (Array.isArray(result.focus_design)) view.focus_design=result.focus_design;
  if (view.focus_design && result.designs?.length) view.designIndex=selectedDesignIndex(result);
  else if (Number.isInteger(result.designIndex) && result.designIndex>=0) view.designIndex=result.designIndex;
  if (result.distribution) {
    const {basis,source,scenarios,originals}=result.distribution;
    view.distribution={basis,source,scenarios:scenarios.map(({coordinates,weight})=>({coordinates,weight}))};
    if(originals?.length) view.distribution.originals=originals.map(({attachment_id,sha256})=>({attachment_id,sha256}));
  }
  return view;
}
