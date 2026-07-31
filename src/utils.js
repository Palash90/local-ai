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
