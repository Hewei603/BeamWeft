// Saved deviations can have more precision or range than a JavaScript number.
function decimalParts(value) {
  const match=/^(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(String(value));
  if(!match)return null;
  const digits=match[1]+(match[2] || '');
  const start=digits.search(/[1-9]/);
  if(start===-1)return {exponent:-Infinity,significant:''};
  return {exponent:Number(match[3] || 0)+match[1].length-start-1,significant:digits.slice(start).replace(/0+$/,'')};
}

export function compareDeviations(left,right) {
  if(Number.isFinite(left) && Number.isFinite(right))return Math.sign(left-right);
  const a=decimalParts(left),b=decimalParts(right);
  if(!a || !b)return null;
  if(a.exponent!==b.exponent)return a.exponent<b.exponent?-1:1;
  if(a.significant===b.significant)return 0;
  // With the same leading power, later digits refine the mantissa to the right.
  return a.significant<b.significant?-1:1;
}

export function deviationText(value,precise=false) {
  const approximation=Number(value);
  if(!precise && Number.isFinite(approximation) && (approximation!==0 || typeof value==='number'))return Number(approximation.toPrecision(7)).toString();
  if(typeof value==='number')return String(value);
  if(value.length<=28)return value;
  const parts=decimalParts(value);
  if(!parts || !parts.significant)return value;
  const digits=parts.significant;
  const truncated=digits.length>10?'…':'';
  const mantissa=digits[0]+(digits.length>1?'.'+digits.slice(1,10):'');
  return mantissa+truncated+' × 10^'+parts.exponent;
}
