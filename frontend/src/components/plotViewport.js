// The camera is expressed in the same unfolded millimetres as the model.
// A null camera follows the complete path as its physical extent changes.
export function positionText(z) {
  return Number.isFinite(z) ? String(Number(z.toPrecision(16))) : '—';
}

export function axialPixel(z, view, left, right) {
  if (view.start === view.end) return (left + right) / 2;
  return left + (z - view.start) / (view.end - view.start) * (right - left);
}

// A transfer may change the optical state at coincident reference faces.
// Ignore relative floating-point roundoff when comparing the two face values.
export function differentAtSameZ(z, value, previousZ, previousValue) {
  return z === previousZ && Math.abs(value-previousValue) >
    32*Number.EPSILON*Math.max(Math.abs(value),Math.abs(previousValue));
}

export function minimumSpan(bounds) {
  return 32 * Number.EPSILON * Math.max(1, Math.abs(bounds.start), Math.abs(bounds.end));
}

export function fitInterval(start, span, bounds) {
  const extent = bounds.end - bounds.start;
  if (span >= extent || extent <= minimumSpan(bounds)) return bounds;
  const width = Math.max(span, minimumSpan(bounds));
  if (start + width >= bounds.end) return {start: bounds.end - width, end: bounds.end};
  const first = Math.max(bounds.start, start);
  return {start: first, end: first + width};
}

export function zoomInterval(view, bounds, factor, focus) {
  const anchor = Number.isFinite(focus) && focus >= view.start && focus <= view.end
    ? focus : view.start + (view.end - view.start) / 2;
  const span = Math.max(minimumSpan(bounds), (view.end - view.start) * factor);
  return fitInterval(anchor - span / 2, span, bounds);
}

export function revealPosition(view, bounds, position) {
  if (!Number.isFinite(position) || position < bounds.start || position > bounds.end ||
      (position >= view.start && position <= view.end)) return view;
  const span = view.end - view.start;
  return fitInterval(position - span / 2, span, bounds);
}

// Clip the existing polyline before projecting it into pixels. This retains
// solver discontinuities and avoids extreme SVG coordinates at high zoom.
export function profileInView(points, breaks, view) {
  const result = [];
  const starts = new Set(breaks);
  let part;
  for (let i = 0; i < points.length; i++) {
    const [z, r] = points[i];
    // An equivalent optic may transform the radius between coincident reference
    // faces. Only the two endpoint radii are known at that coordinate.
    const previous = points[i-1];
    const faceJump = previous && differentAtSameZ(z,r,previous[0],previous[1]);
    if (!i || starts.has(i) || faceJump) {
      part = [];
      if (z >= view.start && z <= view.end) {
        part.push(points[i]);
        result.push(part);
      }
      continue;
    }
    const [prevZ, prevR] = points[i-1];
    if (z < view.start || prevZ > view.end) continue;
    const at = boundary => [boundary, prevR + (r-prevR) * ((boundary-prevZ)/(z-prevZ))];
    if (!part.length) {
      part.push(prevZ < view.start ? at(view.start) : points[i-1]);
      result.push(part);
    }
    const end = z > view.end ? at(view.end) : points[i];
    // Real coincident jumps started a new part above. Any remaining sample at
    // the same z is the same face within roundoff, which is a single marker.
    if (part.at(-1)[0] !== end[0]) part.push(end);
  }
  return result;
}

export function transverseExtent(segments) {
  let extent = 0;
  for (const segment of segments) {
    for (const [,value] of segment) extent = Math.max(extent, Math.abs(value));
  }
  return extent;
}

// Simplify one already-clipped continuous trace. Each discarded sample must
// lie within the vertical display tolerance of the line through retained
// points. Pin local extrema, plateau edges and both sides of a vertical jump.
export function displayProfile(points, tolerance) {
  if (points.length <= 2 || !(tolerance > 0)) return points;
  const output = [points[0]];
  let anchor = 0, lower = -Infinity, upper = Infinity;
  function pin(index) {
    if (index !== anchor) output.push(points[index]);
    anchor = index;
    lower = -Infinity;
    upper = Infinity;
  }
  for (let i=1; i<points.length; i++) {
    const previous = points[i-1], [z, radius] = points[i];
    if (z <= previous[0]) {
      pin(i-1);
      pin(i);
      continue;
    }
    if (i>1 && Math.sign(previous[1]-points[i-2][1]) !== Math.sign(radius-previous[1])) pin(i-1);
    let span = z-points[anchor][0], delta = radius-points[anchor][1];
    const slope = delta/span;
    if (slope < lower || slope > upper) {
      pin(i-1);
      span = z-previous[0];
      delta = radius-previous[1];
    }
    lower = Math.max(lower, (delta-tolerance)/span);
    upper = Math.min(upper, (delta+tolerance)/span);
  }
  pin(points.length-1);
  return output;
}

export function axisTicks(view, count) {
  const span = view.end - view.start;
  if (!span) return {offset:0,ticks:[{z:view.start,label:positionText(view.start)}]};
  if (span < minimumSpan(view)) {
    return {offset:0,ticks:[view.start,view.end].map(z=>({z,label:String(z)}))};
  }
  const offset = Math.max(Math.abs(view.start), Math.abs(view.end)) > 100000 * span
    ? view.start : 0;
  const target = span/count;
  const decade = 10**Math.floor(Math.log10(target));
  const factor = target/decade;
  const step = decade*(factor<Math.SQRT2?1:factor<Math.sqrt(10)?2:factor<Math.sqrt(50)?5:10);
  const first = Math.floor((view.start-offset)/step);
  const last = Math.ceil((view.end-offset)/step);
  const ticks = [];
  for (let i=first;i<=last;i++) {
    const relative = Number((i*step).toPrecision(12));
    const z = offset+relative;
    if (z >= view.start && z <= view.end) ticks.push({z,label:String(relative)});
  }
  return {offset, ticks};
}
