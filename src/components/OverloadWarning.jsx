export default function OverloadWarning({ overheated, gpuTemp }) {
  if (!overheated) return null
  const tempStr = gpuTemp != null ? gpuTemp + '\u00B0C' : ''
  return (
    <div id="overload-warn" style={{ display: 'block' }}>
      Server overloaded. Your queued messages will be processed once the GPU cools down.{tempStr ? ' (GPU: ' + tempStr + ')' : ''}
    </div>
  )
}
