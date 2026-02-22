import axios, { AxiosInstance } from 'axios';

const API_BASE = process.env.REACT_APP_API_URL || '/api';

let _token: string | null = localStorage.getItem('token');

export const api: AxiosInstance = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
});

api.interceptors.request.use((config) => {
  _token = localStorage.getItem('token');
  if (_token) {
    config.headers['Authorization'] = `Bearer ${_token}`;
  }
  return config;
});

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token');
      window.location.href = '/login';
    }
    return Promise.reject(err);
  }
);

export function setToken(token: string) {
  _token = token;
  localStorage.setItem('token', token);
}

export function clearToken() {
  _token = null;
  localStorage.removeItem('token');
}

// Typed API methods
export const authApi = {
  login: (username: string, password: string) =>
    api.post('/auth/login', { username, password }),
  me: () => api.get('/auth/me'),
};

export const dashboardApi = {
  summary: () => api.get('/dashboard/summary'),
};

export const endpointsApi = {
  list: (params?: Record<string, any>) => api.get('/endpoints', { params }),
  get: (id: string) => api.get(`/endpoints/${id}`),
  create: (data: any) => api.post('/endpoints', data),
  update: (id: string, data: any) => api.put(`/endpoints/${id}`, data),
  delete: (id: string) => api.delete(`/endpoints/${id}`),
  probes: (id: string, hours?: number) =>
    api.get(`/endpoints/${id}/probes`, { params: { hours } }),
};

export const probesApi = {
  latest: (limit?: number) => api.get('/probes/latest', { params: { limit } }),
  summary: () => api.get('/probes/summary'),
};

export const hl7Api = {
  stuck: (min_age_seconds?: number) =>
    api.get('/hl7/stuck', { params: { min_age_seconds } }),
  stats: (hours?: number) => api.get('/hl7/stats', { params: { hours } }),
  messages: (params?: Record<string, any>) => api.get('/hl7/messages', { params }),
  flows: () => api.get('/hl7/flows'),
};

export const alertsApi = {
  list: (params?: Record<string, any>) => api.get('/alerts', { params }),
  get: (id: string) => api.get(`/alerts/${id}`),
  activeCount: () => api.get('/alerts/active/count'),
  bite: (id: string) => api.get(`/alerts/bite/${id}`),
  acknowledge: (id: string) => api.post(`/alerts/${id}/acknowledge`),
  resolve: (id: string) => api.post(`/alerts/${id}/resolve`),
};

export const metricsApi = {
  servers: () => api.get('/metrics/servers'),
  serverHistory: (name: string, hours?: number) =>
    api.get(`/metrics/servers/${encodeURIComponent(name)}/history`, { params: { hours } }),
};
