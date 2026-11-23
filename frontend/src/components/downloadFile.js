export function downloadJSON(filename,value) {
  downloadBlob(filename,new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
}

export function downloadBlob(filename,blob) {
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');
  link.href=url;
  link.download=filename;
  link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
