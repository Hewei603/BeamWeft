export function acceptanceInequality({label,unit,lower,upper}) {
  const quantity=unit && label.endsWith(' / '+unit) ? label.slice(0,-(unit.length+3)) : label;
  const reading=value=>unit && unit!=='1' ? value+' '+unit : String(value);
  if (lower!=null && upper!=null) {
    return lower===upper ? quantity+' = '+reading(lower) : reading(lower)+' ≤ '+quantity+' ≤ '+reading(upper);
  }
  if (lower!=null) return quantity+' ≥ '+reading(lower);
  if (upper!=null) return quantity+' ≤ '+reading(upper);
  return quantity;
}
