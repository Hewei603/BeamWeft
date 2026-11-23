import React from 'react';

export function fileRef(file) {
  return {attachment_id:file.id,...(file.content_hash && {sha256:file.content_hash})};
}

export default function OriginalFileInput({files=[],reference,onSelect,...props}) {
  return <select {...props} value={reference?.attachment_id ?? ''} onChange={event=>{
    const file=files.find(item=>item.id===Number(event.target.value));
    onSelect(file ? fileRef(file) : undefined);
  }}><option value="">{files.length?'选择案例原件':'请先在实验资料中归档原件'}</option>
    {reference && !files.some(f=>f.id===reference.attachment_id) && <option value={reference.attachment_id}>原件 #{reference.attachment_id}</option>}
    {files.map(file=><option key={file.id} value={file.id}>{file.relative_path || file.filename} · #{file.id}</option>)}</select>;
}
