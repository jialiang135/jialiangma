const BASE = '/api'

async function request(url, options = {}) {
  const token = localStorage.getItem('token')
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...options.headers,
  }

  const res = await fetch(`${BASE}${url}`, { ...options, headers })

  if (res.status === 401) {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    window.dispatchEvent(new CustomEvent('auth-expired'))
    throw new Error('登录已过期，请重新登录')
  }

  return res
}

export async function get(url, params = {}) {
  const qs = new URLSearchParams(params).toString()
  const full = qs ? `${url}?${qs}` : url
  const res = await request(full)
  return res.json()
}

export async function post(url, data = {}) {
  const res = await request(url, {
    method: 'POST',
    body: JSON.stringify(data),
  })
  return res.json()
}

export async function del(url) {
  const res = await request(url, { method: 'DELETE' })
  return res.json()
}

export async function uploadFiles(url, files) {
  const token = localStorage.getItem('token')
  const form = new FormData()
  for (const f of files) {
    form.append('files', f)
  }
  const res = await fetch(`${BASE}${url}`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  })
  return res.json()
}

export async function uploadFile(url, file, fieldName = 'file') {
  const token = localStorage.getItem('token')
  const form = new FormData()
  form.append(fieldName, file)
  const res = await fetch(`${BASE}${url}`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  })
  return res.json()
}
