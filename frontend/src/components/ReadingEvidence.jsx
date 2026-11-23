import React, {useEffect,useState} from 'react';
import {casesApi} from '../api/cases';
import {FilePreview} from './CaseFiles';

export const readingUses={unspecified:'独立性待确认',held_out:'登记为同工况的独立留出读数',
  model_input:'读数参与了建模',different_configuration:'采集工况不同'};

export function EvidenceRole({evidence}) {
  const role=evidence?.data_role;
  return <div className="reading-role">
    <strong>{readingUses[role] || readingUses.unspecified}</strong>
    {evidence?.declared_data_role && evidence.declared_data_role!==role && <small>登记声明 · {readingUses[evidence.declared_data_role]}，按拟合来源核对后调整。</small>}
    {!!evidence?.fit_input_ids?.length && <small>用过这条读数的拟合 · {evidence.fit_input_ids.map(id=>'#'+id).join('、')}</small>}
    {!!evidence?.unverified_fit_ids?.length && <small>与原采集的关系待核对 · 拟合 {evidence.unverified_fit_ids.map(id=>'#'+id).join('、')}</small>}
  </div>;
}

export function ReadingOriginal({caseId,source}) {
  const [open,setOpen]=useState(false),[attempt,retry]=useState(0);
  const [result,setResult]=useState(null);
  const attachment=source?.attachment_id,digest=source?.sha256;
  useEffect(()=>{
    if(!open || !caseId || !attachment)return;
    let live=true;
    casesApi.listAttachments(caseId,{attachment_id:attachment}).then(files=>{
      if(!live)return;
      const file=files.find(f=>f.id===attachment);
      if(!file)setResult({error:'此工程缺少检验引用的原件，归档路径和指纹仍保留在记录中。'});
      else if(digest && file.content_hash && digest!==file.content_hash)setResult({error:'现存原件的归档指纹与检验时不同，请核对原文件。'});
      else setResult({file});
    }).catch(e=>{if(live)setResult({error:e.message});});
    return ()=>{live=false;};
  },[caseId,attachment,digest,open,attempt]);
  if(!caseId || !attachment)return null;
  return <details onToggle={e=>{if(e.target===e.currentTarget){setOpen(e.currentTarget.open);setResult(null);}}}>
    <summary>核对检验原件 · {source.path || '#'+attachment}</summary>
    {open && <>{digest && <small className="check-digest">登记时 SHA-256 · <code>{digest}</code></small>}
      {result?.file?<FilePreview caseId={caseId} file={result.file}/>:result?.error?<p role="alert">{result.error}
        <button type="button" onClick={()=>{setResult(null);retry(n=>n+1);}}>重新读取</button></p>:<p role="status">正在定位原件…</p>}</>}
  </details>;
}
