export const productName = 'BeamWeft';

// Historical study documents carry these identifiers in their provider and JSON schema.
export const previousBuiltInProvider = 'LaserClaw';
export const providerLabel = key => key === previousBuiltInProvider ? productName : key;
export const sameProvider = (left, right) => providerLabel(left) === providerLabel(right);
export const isBuiltInProvider = key => providerLabel(key) === productName;
export const inputWidget = field => field?.['x-beamweft-input'] ?? field?.['x-laserclaw-input'];
