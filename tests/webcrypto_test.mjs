import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { webcrypto } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { delimiter } from 'node:path';

if (!globalThis.crypto) globalThis.crypto = webcrypto;
const source = await readFile(new URL('../docs/app.js', import.meta.url), 'utf8');
const { decryptReport, renderChart } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const password = 'clave de prueba';
const expected = { participant: { id: 'synthetic', name: 'Ejemplo' }, as_of: '2026-01-02', balance_usd: '123.45', history: [], movements: [], allocation: [] };
const salt = crypto.getRandomValues(new Uint8Array(16));
const iv = crypto.getRandomValues(new Uint8Array(12));
const material = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
const key = await crypto.subtle.deriveKey({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations: 600000 }, material, { name: 'AES-GCM', length: 256 }, false, ['encrypt']);
const encrypted = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, new TextEncoder().encode(JSON.stringify(expected)));
const envelope = { version: 1, kdf: 'PBKDF2-SHA256', iterations: 600000, salt: Buffer.from(salt).toString('base64'), iv: Buffer.from(iv).toString('base64'), ciphertext: Buffer.from(encrypted).toString('base64') };
assert.deepEqual(await decryptReport(envelope, password), expected);
await assert.rejects(decryptReport(envelope, 'contraseña incorrecta'));
const damaged = Buffer.from(encrypted); damaged[0] ^= 1;
await assert.rejects(decryptReport({ ...envelope, ciphertext: damaged.toString('base64') }, password));
await assert.rejects(decryptReport({ ...envelope, version: 2 }, password));
assert.match(renderChart([], 'balance_usd'), /cierres disponibles/);
assert.match(renderChart([{ date: '2026-01-02', balance_usd: '123.45' }]), /<svg/);
assert.doesNotMatch(renderChart([{ date: '2026-01-02', balance_usd: '123.45' }]), /NaN|Infinity/);

// Exercise the real Python implementation, exchanging only synthetic data in
// memory. No plaintext fixture or password file is created inside the public tree.
function pythonCrypto(operation, data) {
  const pythonCode = `import json,sys\nfrom fci_familiar.crypto import encrypt_report,decrypt_report\nvalue=json.load(sys.stdin)\nresult=${operation === 'encrypt' ? 'encrypt_report(value["report"],value["password"])' : 'decrypt_report(value["envelope"],value["password"])'}\nprint(json.dumps(result,ensure_ascii=False))`;
  const python = process.env.FCI_TEST_PYTHON || 'python';
  const localPath = fileURLToPath(new URL('../local/', import.meta.url));
  const result = spawnSync(python, ['-c', pythonCode], {
    input: JSON.stringify(data), encoding: 'utf8', timeout: 30000,
    env: { ...process.env, PYTHONPATH: [localPath, process.env.PYTHONPATH].filter(Boolean).join(delimiter), PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, `Python ${operation} failed: ${result.stderr}`);
  return JSON.parse(result.stdout);
}
const unicodePassword = 'contraseña fácil';
const unicodeReport = { ...expected, participant: { id: 'synthetic', name: 'Prueba de integración' } };
const pythonEnvelope = pythonCrypto('encrypt', { report: unicodeReport, password: unicodePassword });
assert.deepEqual(await decryptReport(pythonEnvelope, unicodePassword), unicodeReport);
await assert.rejects(decryptReport(pythonEnvelope, 'otra clave'));
assert.deepEqual(pythonCrypto('decrypt', { envelope, password }), expected);
console.log('Web Crypto: Python → navegador y navegador → Python, Unicode, contraseña incorrecta, archivo alterado, versión y gráfico verificados.');
