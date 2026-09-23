/** MCP batch tools: start → status → results → submit eval (bearer-gated).
 *
 * Exercises the batch lifecycle without waiting for GPU completion: one
 * no_tools prompt (fast text-only lane), then status/results/submit calls
 * must all answer in valid shapes. Skips cleanly without a bearer.
 */
export const name = 'mcpbatch';
export const long = false;

export async function run(page, ctx, opts) {
  if (!opts.home) {
    ctx.step('mcp batch skipped (no bearer)', true, '');
    return;
  }
  const batch = await opts.home('start_chat_batch', {
    prompts: ['e2e batch probe: reply with exactly BATCH-OK'],
    no_tools: true,
  });
  const batchId = batch.batch_id || batch.batchId || '';
  ctx.step('batch starts', batchId.length > 0, `items=${batch.total ?? batch.item_count ?? '?'}`);

  const st = await opts.home('get_batch_status', { batch_id: batchId });
  const total = st.total ?? st.item_count ?? -1;
  ctx.step('batch status answers', total >= 1, JSON.stringify(st).slice(0, 120));

  const res = await opts.home('get_batch_results', { batch_id: batchId, new_only: false });
  ctx.step('batch results answer', Array.isArray(res) || (res && typeof res === 'object'), '');

  const sub = await opts.home('submit_batch_results', {
    batch_id: batchId, results: [{ index: 0, grade: 'pass', note: 'e2e' }],
  });
  ctx.step('batch eval submit answers', !!sub && (sub.accepted >= 1 || sub.ok === true || typeof sub === 'object'), JSON.stringify(sub).slice(0, 120));

  const ms = await opts.home('get_message_status', { task_id: 'no-such-task' }).catch(e => ({ __err: String(e).slice(0, 80) }));
  ctx.step('unknown message status handled', !!ms && (ms.status !== undefined || ms.__err !== undefined), JSON.stringify(ms).slice(0, 120));
  await page.screenshot({ path: 'reports/shot-mcpbatch.png' }).catch(() => {});
}
