export function toApiImage(url, shareToken) {
  if (!url || typeof url !== 'string') return url
  if (url.startsWith('data:') || /^https?:/i.test(url)) return url
  if (url.startsWith('/api/public/')) return url
  if (shareToken) {
    // On public share pages everything must stay public: rewrite raw
    // /api/image/... links (which need SSO) to the scoped share route.
    // Unknown /api/... shapes pass through (server answers 404, never 403).
    if (url.startsWith('/api/image/')) return `/api/public/share/${shareToken}/image/${url.slice('/api/image/'.length)}`
    if (url.startsWith('/api/')) return url
    if (url.startsWith('/')) return `/api/public/share/${shareToken}/image/${url.slice(1)}`
    return url
  }
  if (url.startsWith('/api/')) return url
  if (url.startsWith('/uploads/') || url.startsWith('/output/')) return '/api/image/' + url.slice(1)
  if (url.startsWith('/')) return '/api/image/' + url.slice(1)
  return url
}

export function toApiMusic(url, shareToken) {
  if (!url || typeof url !== 'string') return url
  if (url.startsWith('data:') || /^https?:/i.test(url)) return url
  if (url.startsWith('/api/public/')) return url
  if (shareToken) {
    if (url.startsWith('/api/music/')) return `/api/public/share/${shareToken}/music/${url.slice('/api/music/'.length)}`
    if (url.startsWith('/api/')) return url
    if (url.startsWith('/music/')) return `/api/public/share/${shareToken}/music/${url.slice('/music/'.length)}`
    return url
  }
  if (url.startsWith('/api/')) return url
  if (url.startsWith('/music/')) return '/api/music/' + url.slice('/music/'.length)
  return url
}

export async function downloadFile(url, fallbackName = 'file') {
  const full = url.startsWith('http') || url.startsWith('data:') ? url : window.location.origin + url
  const filename = full.split('/').pop() || fallbackName
  try {
    const res = await fetch(full)
    const blob = await res.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(a.href)
  } catch {
    window.open(full, '_blank')
  }
}

// Join soft line breaks inside inline $...$ math spans so the KaTeX inline
// tokenizer (which excludes \n from math content) can match them. Fenced
// code blocks and $$ display blocks pass through untouched. Pure function:
// same input always yields same output, no DOM access (safe for tests).
export function normalizeMathLineBreaks(text) {
  if (!text || typeof text !== 'string' || !text.includes('$')) return text
  return text.split(/(```[\s\S]*?```)/g).map((seg, i) => {
    if (i % 2 === 1) return seg
    return seg.split(/(\$\$[\s\S]*?\$\$)/g).map((chunk, j) => {
      if (j % 2 === 1) return chunk
      let out = ''
      let inMath = false
      let k = 0
      while (k < chunk.length) {
        const ch = chunk[k]
        if (ch === '\\' && k + 1 < chunk.length) {
          out += ch + chunk[k + 1]
          k += 2
          continue
        }
        if (ch === '$') {
          if (chunk[k + 1] === '$') {
            out += '$$'
            k += 2
            continue
          }
          inMath = !inMath
          out += ch
          k += 1
          continue
        }
        out += (ch === '\n' && inMath) ? ' ' : ch
        k += 1
      }
      return out
    }).join('')
  }).join('')
}
