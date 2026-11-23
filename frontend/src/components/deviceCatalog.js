export function installedDevice(devices, recipe) {
  if (!recipe) return null;
  const device = devices?.[recipe.study];
  return device?.prescription.version === recipe.version &&
    device?.prescription.provider === recipe.provider ? device : null;
}
