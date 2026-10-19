import assert from 'node:assert/strict';
import {constants, generateKeyPairSync, privateDecrypt} from 'node:crypto';
import test from 'node:test';
import {findRecipient, main, sealRecipient, validToken} from '../tools/telegram-setup.mjs';

const now = 1800000000000;
const challenge = {text: '연결 12345678', issuedAt: now - 10000, expiresAt: now + 10000};
test('token structure is checked without printing a credential', () => {
  assert.ok(validToken('123456789:' + 'a'.repeat(35)));
  assert.ok(validToken(' 123456789:' + 'a'.repeat(35) + '\n'));
  for (const value of [undefined, 'TELEGRAM_BOT_TOKEN', 'https://api.telegram.org/bot...', 'AQ.example', 'bot123:example']) {
    assert.equal(validToken(value), false);
  }
});
function update(id = 1234567, overrides = {}) {
  return {message: {date: now / 1000, chat: {id, type: 'private'}, from: {id, is_bot: false}, text: challenge.text, ...overrides}};
}
test('only exact recent personal challenge identifies recipient', () => {
  assert.equal(findRecipient([update()], challenge, now), '1234567');
  assert.throws(() => findRecipient([update(1234567, {text: '/start'})], challenge, now));
});
test('groups, other senders, bots and stale messages are rejected', () => {
  for (const override of [{chat: {id: -1234567, type: 'group'}}, {from: {id: 9876543}},
    {from: {id: 1234567, is_bot: true}}, {date: (now - 20000) / 1000}]) {
    assert.throws(() => findRecipient([update(1234567, override)], challenge, now));
  }
});
test('expired challenge and more than one matching recipient fail closed', () => {
  assert.throws(() => findRecipient([update()], challenge, now + 20000));
  assert.throws(() => findRecipient([update(), update(9876543)], challenge, now));
  assert.equal(findRecipient([update(), update()], challenge, now), '1234567');
});
test('public log envelope requires the matching private key', () => {
  const keys = generateKeyPairSync('rsa', {modulusLength: 3072});
  const sealed = sealRecipient('1234567', challenge.text, keys.publicKey);
  const clear = privateDecrypt({key: keys.privateKey, padding: constants.RSA_PKCS1_OAEP_PADDING,
    oaepHash: 'sha256'}, Buffer.from(sealed, 'base64'));
  assert.equal(JSON.parse(clear).chat_id, '1234567');
  assert.ok(!sealed.includes('1234567'));
  const other = generateKeyPairSync('rsa', {modulusLength: 2048});
  assert.throws(() => privateDecrypt(other.privateKey, Buffer.from(sealed, 'base64')));
});
for (const failDelivery of [false, true]) {
  test(`connection test sends once to configured recipient (network failure=${failDelivery})`, async () => {
    const originalFetch = globalThis.fetch;
    const originalLog = console.log;
    const keys = ['SETUP_MODE', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'];
    const previous = Object.fromEntries(keys.map(k => [k, process.env[k]]));
    let sends = 0;
    try {
      process.env.SETUP_MODE = 'test';
      process.env.TELEGRAM_BOT_TOKEN = '123456789:' + 'a'.repeat(35);
      process.env.TELEGRAM_CHAT_ID = '1234567';
      console.log = () => {};
      globalThis.fetch = async (url, options) => {
        if (url.endsWith('/getMe')) return {ok: true, status: 200,
          json: async () => ({ok: true, result: {is_bot: true, username: 'FixtureBot'}})};
        assert.ok(url.endsWith('/sendMessage'));
        sends++;
        assert.equal(JSON.parse(options.body).chat_id, '1234567');
        if (failDelivery) throw new Error('sensitive URL must not appear in the error');
        return {ok: true, status: 200, json: async () => ({ok: true, result: {chat: {id: 1234567}, message_id: 42}})};
      };
      if (failDelivery) await assert.rejects(main(), {message: 'telegram_network_error'});
      else await main();
      assert.equal(sends, 1);
    } finally {
      globalThis.fetch = originalFetch;
      console.log = originalLog;
      for (const k of keys) {
        if (previous[k] === undefined) delete process.env[k];
        else process.env[k] = previous[k];
      }
    }
  });
}
