import {sameProvider} from '../brand.js';
export function installedDevice(devices, recipe) {
  if (!recipe) return null;
  const device = devices?.[recipe.study];
  return device && device.prescription.version === recipe.version &&
    sameProvider(device.prescription.provider, recipe.provider) ? device : null;
}
