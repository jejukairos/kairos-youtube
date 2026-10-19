// One-time setup: tokens remain inside Actions; private chat IDs leave encrypted.
import { constants, createHash, publicEncrypt } from 'node:crypto';
import { pathToFileURL } from 'node:url';

export function findRecipient(updates, challenge, now = Date.now()) {
  if (!challenge || typeof challenge.text !== 'string' || !/^연결 [0-9]{8}$/.test(challenge.text) ||
      !Number.isFinite(challenge.issuedAt) || !Number.isFinite(challenge.expiresAt) ||
      now < challenge.issuedAt || now > challenge.expiresAt ||
      challenge.expiresAt - challenge.issuedAt > 12 * 3600_000) {
    throw new Error('telegram_link_challenge_expired_or_invalid');
  }
  const ids = new Set();
  for (const update of updates) {
    const m = update.message;
    if (m?.chat?.type !== 'private' || m.from?.is_bot || m.from?.id !== m.chat.id ||
        !Number.isSafeInteger(m.chat.id) || m.chat.id <= 0 ||
        m.text?.trim() !== challenge.text || !Number.isFinite(m.date) ||
        m.date * 1000 < challenge.issuedAt - 2000 || m.date * 1000 > now + 2000) continue;
    ids.add(String(m.chat.id));
  }
  if (ids.size !== 1) throw new Error(ids.size ? 'telegram_link_ambiguous' : 'telegram_link_message_not_found');
  return [...ids][0];
}

export function sealRecipient(chatId, challengeText, publicKey) {
  const payload = { chat_id: chatId, challenge_sha256: createHash('sha256').update(challengeText).digest('hex') };
  return publicEncrypt({key: publicKey, padding: constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256'},
    Buffer.from(JSON.stringify(payload))).toString('base64');
}

async function api(method, payload = {}) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) throw new Error('telegram_token_missing');
  let response;
  try {
    response = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload), signal: AbortSignal.timeout(20000), redirect: 'error'
    });
  } catch { throw new Error('telegram_network_error'); }
  if (response.status === 401) throw new Error('telegram_token_invalid');
  if (!response.ok) throw new Error('telegram_api_failed');
  let data;
  try { data = await response.json(); } catch { throw new Error('telegram_response_invalid'); }
  if (!data.ok) throw new Error('telegram_api_failed');
  return data.result;
}

export async function main() {
  const bot = await api('getMe');
  if (!bot.is_bot || !/^[A-Za-z0-9_]+$/.test(bot.username || '')) throw new Error('telegram_bot_identity_invalid');
  console.log(JSON.stringify({status: 'bot_verified', bot_username: bot.username}));
  if (process.env.SETUP_MODE === 'identify') return;
  if (process.env.SETUP_MODE !== 'link') throw new Error('telegram_setup_mode_invalid');
  const webhook = await api('getWebhookInfo');
  if (webhook.url) throw new Error('telegram_webhook_configured_stop');
  const challenge = JSON.parse(process.env.TELEGRAM_LINK_CHALLENGE || '{}');
  const updates = await api('getUpdates', {timeout: 0, limit: 100, allowed_updates: ['message']});
  // No offset: never acknowledge or discard updates during linking.
  const cid = findRecipient(updates, challenge);
  const sealed = sealRecipient(cid, challenge.text, process.env.TELEGRAM_LINK_PUBLIC_KEY);
  console.log('KAIROS_LINK_RESULT=' + sealed);
  console.log('telegram_recipient_encrypted_ready');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => {
    const safe = /^telegram_[a-z_]+$/.test(error.message) ? error.message : 'telegram_setup_failed_details_redacted';
    console.error(safe);
    process.exitCode = 1;
  });
}
