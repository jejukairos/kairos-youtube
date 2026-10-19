// Run only on the owner's PC, never in Actions. Does not print tokens/chat IDs.
import {constants, createHash, generateKeyPairSync, privateDecrypt, randomInt} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(fileURLToPath(new URL('..', import.meta.url)));
const folder = resolve(root, 'state');
const repo = 'jejukairos/kairos-youtube';
const recordPath = resolve(folder, 'telegram-link.json');
const keyPath = resolve(folder, 'telegram-link-private.pem');
function gh(args, input) {
  return execFileSync('gh', args, {input, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe']});
}
try {
  if (process.argv[2] === 'prepare') {
    mkdirSync(folder, {recursive: true});
    let record, privateKey, publicKey;
    if (existsSync(recordPath) && existsSync(keyPath)) {
      record = JSON.parse(readFileSync(recordPath, 'utf8'));
      privateKey = readFileSync(keyPath, 'utf8');
      publicKey = record.publicKey;
    }
    if (!record || record.challenge.expiresAt <= Date.now()) {
      const keys = generateKeyPairSync('rsa', {modulusLength: 3072,
        publicKeyEncoding: {type: 'spki', format: 'pem'},
        privateKeyEncoding: {type: 'pkcs8', format: 'pem'}});
      ({privateKey, publicKey} = keys);
      record = {challenge: {text: `연결 ${randomInt(10000000, 100000000)}`,
        issuedAt: Date.now(), expiresAt: Date.now() + 12 * 3600_000}, publicKey};
      writeFileSync(keyPath, privateKey, {mode: 0o600});
      writeFileSync(recordPath, JSON.stringify(record), {mode: 0o600});
    }
    gh(['secret', 'set', 'TELEGRAM_LINK_CHALLENGE', '--repo', repo], JSON.stringify(record.challenge));
    gh(['variable', 'set', 'TELEGRAM_LINK_PUBLIC_KEY', '--repo', repo], publicKey);
    console.log(JSON.stringify({status: 'telegram_link_prepared', message_to_send: record.challenge.text}));
  } else if (process.argv[2] === 'complete') {
    const record = JSON.parse(readFileSync(recordPath, 'utf8'));
    if (Date.now() > record.challenge.expiresAt) throw new Error('telegram_link_challenge_expired');
    const log = readFileSync(resolve(folder, 'telegram-link-run.log'), 'utf8');
    const matches = [...log.matchAll(/KAIROS_LINK_RESULT=([A-Za-z0-9+/=]+)/g)];
    if (matches.length !== 1) throw new Error('telegram_link_result_missing_or_ambiguous');
    const plaintext = privateDecrypt({key: readFileSync(keyPath, 'utf8'),
      padding: constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256'}, Buffer.from(matches[0][1], 'base64'));
    const result = JSON.parse(plaintext);
    if (result.challenge_sha256 !== createHash('sha256').update(record.challenge.text).digest('hex') ||
        !/^[1-9][0-9]{4,15}$/.test(result.chat_id)) throw new Error('telegram_link_result_invalid');
    gh(['secret', 'set', 'TELEGRAM_CHAT_ID', '--repo', repo], result.chat_id);
    console.log('telegram_chat_secret_saved');
    gh(['secret', 'delete', 'TELEGRAM_LINK_CHALLENGE', '--repo', repo]);
    gh(['variable', 'delete', 'TELEGRAM_LINK_PUBLIC_KEY', '--repo', repo]);
    unlinkSync(keyPath);
    unlinkSync(recordPath);
    console.log('telegram_link_temporary_credentials_removed');
  } else throw new Error('telegram_local_setup_mode_invalid');
} catch (error) {
  console.error(/^telegram_[a-z_]+$/.test(error.message) ? error.message : 'telegram_local_setup_failed_details_redacted');
  process.exitCode = 1;
}
