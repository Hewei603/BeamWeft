export const productName = 'BeamWeft';

// Stored study definitions use the original provider key to replay old results.
export const providerLabel = key => key === 'LaserClaw' ? productName : key;
