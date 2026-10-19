// Current one-time setup job, not a recurring scheduler. No Gemini calls.
import {execFileSync} from 'node:child_process';
import {closeSync, mkdirSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(fileURLToPath(new URL('..', import.meta.url)));
const folder = resolve(root, 'state');
const repo = 'jejukairos/kairos-youtube';
const runId = process.argv[2];
const statusPath = resolve(folder, 'telegram-link-progress.json');
const lockPath = resolve(folder, 'telegram-link-wait.lock');
let locked = false;
function gh(args) {
  return execFileSync('gh', args, {encoding: 'utf8', timeout: 25000, stdio: ['pipe', 'pipe', 'pipe']});
}
const progress = {run_id: runId, phase: 'starting', started_at: new Date().toISOString(), pid: process.pid};
function save(phase) {
  progress.phase = phase;
  progress.updated_at = new Date().toISOString();
  writeFileSync(statusPath + '.tmp', JSON.stringify(progress, null, 2));
  renameSync(statusPath + '.tmp', statusPath);
}
try {
  if (!/^[0-9]{6,20}$/.test(runId || '')) throw new Error('invalid_run_id');
  mkdirSync(folder, {recursive: true});
  closeSync(openSync(lockPath, 'wx'));
  locked = true;
  const deadline = Math.min(Date.now() + 3 * 3600_000,
    JSON.parse(readFileSync(resolve(folder, 'telegram-link.json'), 'utf8')).challenge.expiresAt);
  save('waiting_for_github');
  console.log('Waiting for the already-requested private Telegram link job.');
  let complete = false;
  while (Date.now() < deadline) {
    let run;
    try { run = JSON.parse(gh(['run', 'view', runId, '--repo', repo, '--json', 'status,conclusion'])); }
    catch { await new Promise(r => setTimeout(r, 30000)); continue; }
    if (run.status === 'completed') {
      if (run.conclusion !== 'success') { save('github_link_failed'); process.exitCode = 2; break; }
      save('saving_recipient_secret');
      writeFileSync(resolve(folder, 'telegram-link-run.log'), gh(['run', 'view', runId, '--repo', repo, '--log']));
      execFileSync(process.execPath, [resolve(root, 'tools/telegram-setup-local.mjs'), 'complete'],
        {cwd: root, stdio: ['pipe', 'pipe', 'pipe'], timeout: 60000});
      save('recipient_secret_saved');
      // Record dispatch intent first. Never automatically dispatch twice.
      save('dispatching_single_test');
      const result = gh(['workflow', 'run', 'telegram-setup.yml', '--repo', repo, '-f', 'mode=test']);
      const match = result.match(/\/actions\/runs\/([0-9]+)/);
      if (match) progress.test_run_id = match[1];
      save('test_requested');
      complete = true;
      console.log('Recipient secret saved. One Telegram connection test requested.');
      break;
    }
    await new Promise(r => setTimeout(r, 30000));
  }
  if (!complete && !process.exitCode) { save('waiting_timed_out'); process.exitCode = 2; }
} catch {
  if (locked) save(progress.phase === 'dispatching_single_test' ? 'test_dispatch_uncertain' : 'link_setup_failed_details_redacted');
  console.error('Telegram setup did not complete; inspect the private local progress record.');
  process.exitCode = 2;
} finally {
  if (locked) unlinkSync(lockPath);
}
