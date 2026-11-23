import apiClient, { API_BASE_URL } from './client';

export const designApi = {
  endUnverified: async (id, run) => (await apiClient.post(`/api/cases/${id}/design/runs/${run}/end-unverified`)).data,
  get: async id => (await apiClient.get(`/api/cases/${id}/design`)).data,
  importGeometry: async (id, revision, candidate, target, signal) =>
    (await apiClient.post(`/api/cases/${id}/design/${revision}/import-project`, {candidate, target}, {signal})).data,
  run: async (id, message, attachments, constraints = {}, assumptionPolicy) => (await apiClient.post(`/api/cases/${id}/design/runs`, { message, attachment_ids: attachments, constraints, assumption_policy: assumptionPolicy })).data,
  positionsUrl: (id, revision, candidate) => `${API_BASE_URL}/api/cases/${id}/design/${revision}/positions.csv?candidate=${candidate}`,
  bomUrl: (id, revision, candidate) => `${API_BASE_URL}/api/cases/${id}/design/${revision}/bom.csv?candidate=${candidate}`,
  rezonatorUrl: (id, revision, candidate) => `${API_BASE_URL}/api/cases/${id}/design/${revision}/cavity.rez?candidate=${candidate}`,
  analyze: async (id, revision, params, signal) => (await apiClient.post(`/api/cases/${id}/design/${revision}/analysis`, params, { signal })).data,
  probe: async (id, revision, params, signal) => (await apiClient.post(`/api/cases/${id}/design/${revision}/probe`, params, { signal })).data,
};
