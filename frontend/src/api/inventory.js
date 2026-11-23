import apiClient from './client';

export const inventoryCategoryLabels = {
  other:'catOther', mirror:'catMirror', lens:'catLens', window:'catWindow',
  gain_crystal:'catGain', nonlinear_crystal:'catNonlinear', crystal_other:'catOtherCrystal',
};

export const inventoryApi = {
  recognizeLabel: async file => { const data = new FormData(); data.append('file', file); return (await apiClient.post('/api/inventory/recognize-label', data, { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 360000 })).data; },
  createItem: async payload => (await apiClient.post('/api/inventory/items', payload)).data,
  updateItem: async (itemId, payload) => (await apiClient.patch(`/api/inventory/items/${itemId}`, payload)).data,
  getItem: async itemId => (await apiClient.get(`/api/inventory/items/${itemId}`)).data,
  importWorkbook: async (file) => {
    const formData = new FormData();
    formData.append('file', file);
    const response = await apiClient.post('/api/inventory/import', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
    return response.data;
  },

  listItems: async ({ category, wavelengthNm, functionName, needsReview, limit = 100 } = {}) => {
    const params = { limit };
    if (category) params.category = category;
    if (wavelengthNm) params.wavelength_nm = wavelengthNm;
    if (functionName) params.function = functionName;
    if (needsReview) params.needs_review = true;
    const response = await apiClient.get('/api/inventory/items', { params });
    return response.data;
  },

  match: async (requirement) => {
    const response = await apiClient.post('/api/inventory/match', requirement);
    return response.data;
  },

  listSources: async () => {
    const response = await apiClient.get('/api/inventory/sources');
    return response.data;
  },

  clearItems: async (sourceFile) => {
    const params = sourceFile ? { source_file: sourceFile } : {};
    const response = await apiClient.delete('/api/inventory/items', { params });
    return response.data;
  },

  borrow: async (itemId, payload) => {
    const response = await apiClient.post(`/api/inventory/items/${itemId}/borrow`, payload);
    return response.data;
  },

  returnLoan: async (loanId, payload = {}) => {
    const response = await apiClient.post(`/api/inventory/loans/${loanId}/return`, payload);
    return response.data;
  },

  listLoans: async ({ openOnly = true } = {}) => {
    const response = await apiClient.get('/api/inventory/loans', { params: { open_only: openOnly } });
    return response.data;
  },

  setCondition: async (itemId, payload) => {
    const response = await apiClient.patch(`/api/inventory/items/${itemId}/condition`, payload);
    return response.data;
  },
};
