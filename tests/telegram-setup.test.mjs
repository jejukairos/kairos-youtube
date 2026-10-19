import assert from 'node:assert/strict';
import {constants, generateKeyPairSync, privateDecrypt} from 'node:crypto';
import test from 'node:test';
import {findRecipient, sealRecipient} from '../tools/telegram-setup.mjs';

const now = 1800000000000;
const challenge = {text: '연결 12345678', issuedAt: now - 10000, expiresAt: now + 10000};
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
