export default function OverloadWarning({ overheated, gpuTemp, ramEvacuating }) {
  if (!overheated && !ramEvacuating) return null
  const tempStr = gpuTemp != null ? gpuTemp + '\u00B0C' : ''
  const text = ramEvacuating
    ? 'Server overloaded (high memory). Your queued messages are paused while RAM is freed and servers restart.'
    : 'Server overloaded. Your queued messages will be processed once the GPU cools down.'
  return (
    <div id="overload-warn" style={{ display: 'block' }}>
      {text}{tempStr ? ' (GPU: ' + tempStr + ')' : ''}
    </div>
  )
}
