import axios from 'axios';

export const openedCase = globalThis.document?.querySelector('meta[name="beamweft-case-copy"]')?.content;
const nativeTag = globalThis.document?.querySelector('meta[name="beamweft-native-file"]');
export const nativeFileId = nativeTag?.content;
export const nativeRequiresSaveAs = nativeTag?.dataset.saveAs === 'true';
export const nativeFileSource = nativeTag?.dataset.path ? {
  caseId: nativeTag.content, label: nativeTag.dataset.label, path: nativeTag.dataset.path,
} : null;
const libraryOrigin = globalThis.document?.querySelector('meta[name="beamweft-api-origin"]')?.content;
export const API_BASE_URL = openedCase ? window.location.origin : (libraryOrigin || import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000');

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

const formatErrorDetail = (detail) => {
  if (!detail) {
    return 'Request failed';
  }
  if (typeof detail === 'string') {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const path=Array.isArray(item?.loc) ? item.loc.filter(key=>key!=='body').join('.') : '';
        const message=item?.msg || item?.message || JSON.stringify(item);
        return path ? path+' · '+message : message;
      })
      .join('; ');
  }
  return detail.message || detail.error || JSON.stringify(detail);
};

apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (axios.isCancel(error)) return Promise.reject(error);
    if (error.response) {
      const statusHints = {
        400: '请求内容有误，请检查填写内容',
        401: '未授权，请检查登录状态或 API Key',
        403: '当前账号权限不足',
        404: '请求的内容不存在',
        409: '操作和当前状态冲突',
        413: '文件超过本服务的上传上限',
        422: '请核对输入是否满足本次操作的要求',
        500: '服务器内部错误，请稍后再试',
        501: '服务端缺少该功能所需的依赖，请联系维护者',
        502: 'AI 服务返回错误',
        503: '服务暂时不可用，请稍后再试',
      };
      const hint = statusHints[error.response.status];
      let responseData=error.response.data;
      if(responseData instanceof Blob && /json/i.test(responseData.type)){
        try {responseData=JSON.parse(await responseData.text());}catch{/* Fall back to the status for a malformed error body. */}
      }
      const explanation = responseData?.detail;
      const detail = formatErrorDetail(explanation || error.response.statusText);
      console.error('API error', error.response.status, detail);
      const validationMessage = error.response.status === 422 && typeof explanation === 'string' && explanation.trim();
      const message = validationMessage || (hint ? `${hint}（${detail}）` : `${error.response.status}: ${detail}`);
      const failure = new Error(message);
      failure.status = error.response.status;
      return Promise.reject(failure);
    }

    if (error.request) {
      console.error('API unreachable', API_BASE_URL);
      return Promise.reject(new Error(`未收到服务端回执。请检查与 ${API_BASE_URL} 的连接。`));
    }

    return Promise.reject(new Error(error.message || 'Unexpected API error'));
  },
);

export default apiClient;
