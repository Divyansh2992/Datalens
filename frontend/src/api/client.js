// Pick the backend from where the app is opened: on this machine (localhost)
// talk to the local backend, anywhere else talk to the deployed backend.
const LOCAL_API_URL = 'http://localhost:8000';
const DEPLOYED_API_URL = 'https://datalens-p3he.onrender.com';
const isLocalHost = ['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname);
const configuredBaseUrl = (import.meta.env.VITE_API_URL || '').trim();
const BASE = (isLocalHost ? LOCAL_API_URL : (configuredBaseUrl || DEPLOYED_API_URL)).replace(/\/$/, '');
const JOB_POLL_INTERVAL_MS = 700;
const JOB_TIMEOUT_MS = 120000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function postJson(path, payload) {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Request failed: ${path}`);
  }
  return res.json();
}

async function getJson(path) {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Request failed: ${path}`);
  }
  return res.json();
}

async function waitForJob(jobId, { onStatus, timeoutMs = JOB_TIMEOUT_MS } = {}) {
  const startedAt = Date.now();

  while (Date.now() - startedAt <= timeoutMs) {
    const job = await getJson(`/jobs/${jobId}`);
    if (typeof onStatus === 'function') onStatus(job);

    if (job.status === 'completed') {
      return job.result;
    }
    if (job.status === 'failed') {
      throw new Error(job.error || `${job.job_type || 'Background'} job failed`);
    }

    await sleep(JOB_POLL_INTERVAL_MS);
  }

  throw new Error('Background job timed out. Please retry.');
}

export async function uploadCSV(file) {
  const form = new FormData();
  form.append('file', file);

  const res = await fetch(`${BASE}/upload`, {
    method: 'POST',
    body: form,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Upload failed');
  }
  return res.json();
}

export async function sendQuery({ datasetId, question, mode, sessionId, guardianEnabled = true }) {
  return postJson('/query', {
      dataset_id: datasetId,
      question,
      mode,
      session_id: sessionId,
      guardian_enabled: guardianEnabled,
  });
}

export async function fetchAutoVisualize({ datasetId, mode, onStatus }) {
  try {
    const started = await postJson('/jobs/auto-visualize', {
      dataset_id: datasetId,
      mode,
    });
    return waitForJob(started.job_id, { onStatus });
  } catch (_jobError) {
    // Backward-compatible fallback for older backends.
    return postJson('/auto-visualize', { dataset_id: datasetId, mode });
  }
}

export async function fetchCorrelationMatrix({ datasetId, method = 'pearson', onStatus }) {
  try {
    const started = await postJson('/jobs/correlation', {
      dataset_id: datasetId,
      method,
    });
    return waitForJob(started.job_id, { onStatus });
  } catch (_jobError) {
    // Backward-compatible fallback for older backends.
    return postJson('/correlation-matrix', { dataset_id: datasetId, method });
  }
}

export async function runPreprocessJob({ datasetId, onStatus }) {
  const started = await postJson('/jobs/preprocess', { dataset_id: datasetId });
  return waitForJob(started.job_id, { onStatus });
}

export async function fetchDataHealth({ datasetId, mode }) {
  return postJson('/data-health', { dataset_id: datasetId, mode });
}
