const limit = (value, min, max) => Math.max(min, Math.min(max, value));

export const portHitHeight = 22;
// At the default local scale the 22-unit pin target is 28.6 CSS px high.
// A compact whole network can stay fitted if every target still reaches 24 px.
export const readableScale = 1.3;
const minimumTargetPixels = 24;
export const maxNetworkScale = 4;

// Scale is CSS pixels per schematic unit. Optical lengths never enter this calculation.
export function networkView(bounds, size, camera, selection) {
  const fit = Math.min(size.width/bounds.width, size.height/bounds.height, maxNetworkScale);
  const defaultScale = fit*portHitHeight >= minimumTargetPixels ? fit : readableScale;
  const scale = camera?.fit ? fit : limit(camera?.scale ?? defaultScale, fit, maxNetworkScale);
  const width = size.width/scale, height = size.height/scale;
  const center = camera && !camera.fit ? camera : scale>fit && selection ? selection :
    {x:bounds.x+bounds.width/2, y:bounds.y+bounds.height/2};
  function start(position, span, minimum, extent) {
    return span >= extent ? minimum+(extent-span)/2 : limit(position-span/2, minimum, minimum+extent-span);
  }
  return {x:start(center.x,width,bounds.x,bounds.width),y:start(center.y,height,bounds.y,bounds.height),
    width,height,scale,fit};
}

export function zoomNetworkView(view, factor, anchor) {
  const scale=limit(view.scale*factor,view.fit,maxNetworkScale);
  const center={x:view.x+view.width/2,y:view.y+view.height/2};
  const point=anchor || center;
  return {x:point.x+(center.x-point.x)*view.scale/scale,y:point.y+(center.y-point.y)*view.scale/scale,scale};
}
