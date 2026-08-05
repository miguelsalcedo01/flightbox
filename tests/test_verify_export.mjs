/**
 * The free verifier, checked the only way a verifier can be: against documents
 * it did not produce.
 *
 * Generation is the paid act; checking must not be. So the interesting cases
 * are not "does a good signature pass" but the four ways a bad one has to fail —
 * altered content, altered signature, wrong key, and no signature at all — plus
 * the one case that separates a real canonical form from a byte comparison: the
 * SAME document with its keys written in a different order must still verify.
 *
 * Run:  node tests/test_verify_export.mjs
 */

import { generateKeyPairSync, sign as edSign, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), '..', 'scripts', 'verify-export.mjs');
const { canonicalize } = await import(pathToFileURL(SCRIPT).href);

const dir = mkdtempSync(join(tmpdir(), 'fbx-verify-'));
const failures = [];

function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures.push(`${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
  // ASCII only, deliberately: this repo has been bitten by a Windows cp1252
  // console choking on a test's own output.
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}`);
}

// ── canonical form, pinned against a written expectation ────────────────────
// Not against the implementation's own output: a canonicalizer tested only
// against itself is consistent with anything, including wrong.
check('canonical: keys sort, whitespace goes',
  canonicalize({ b: 1, a: 2, A: 3 }), '{"A":3,"a":2,"b":1}');
check('canonical: nesting sorts too',
  canonicalize({ z: { y: 1, x: [3, { b: 1, a: 2 }] } }), '{"z":{"x":[3,{"a":2,"b":1}],"y":1}}');
check('canonical: array order is content, never sorted',
  canonicalize([3, 1, 2]), '[3,1,2]');
check('canonical: money in a fixed representation',
  canonicalize({ cost: 84.10, zero: -0, int: 12, small: 0.5 }),
  '{"cost":84.1,"int":12,"small":0.5,"zero":0}');
check('canonical: nulls kept, they are content',
  canonicalize({ ended_at: null }), '{"ended_at":null}');
check('canonical: strings escaped',
  canonicalize({ s: 'a"b\\c\nd' }), '{"s":"a\\"b\\\\c\\nd"}');
let threw = false;
try { canonicalize({ n: NaN }); } catch { threw = true; }
check('canonical: a non-finite number is not serialisable', threw, true);
threw = false;
try { canonicalize({ f: () => 1 }); } catch { threw = true; }
check('canonical: an unserialisable value is refused, not skipped', threw, true);

// ── a real signed export, made here, with keys this script never sees ───────
const { publicKey, privateKey } = generateKeyPairSync('ed25519');
const other = generateKeyPairSync('ed25519');

const doc = {
  version: 1,
  workspace: { id: randomUUID(), slug: 'acme', tier: 'team' },
  period: { from: '2026-07-01', to: '2026-07-31' },
  runs: [{
    adw_id: 'adw_9f21', repo: 'acme/app', branch: 'main', status: 'failed',
    halted: true, total_cost: 84.10, started_at: '2026-07-14T09:02:11Z',
    ended_at: '2026-07-14T09:41:50Z',
    governance: [{ name: 'budget_exceeded', payload: { max_run_cost: 25, run_cost: 25.4 } }],
  }],
  totals: { runs: 1, cost: 84.10, halted: 1 },
  generated_at: '2026-08-05T12:00:00Z',
};

const KEY_ID = 'k_2026_08';
const pemPath = join(dir, 'pub.pem');
writeFileSync(pemPath, publicKey.export({ type: 'spki', format: 'pem' }));
const wrongPemPath = join(dir, 'wrong.pem');
writeFileSync(wrongPemPath, other.publicKey.export({ type: 'spki', format: 'pem' }));

function signed(exportObj, key = privateKey) {
  return {
    export: exportObj,
    signature: {
      alg: 'Ed25519', key_id: KEY_ID,
      value: edSign(null, Buffer.from(canonicalize(exportObj), 'utf8'), key).toString('base64'),
    },
  };
}

function write(name, obj) {
  const p = join(dir, name);
  writeFileSync(p, JSON.stringify(obj, null, 2));
  return p;
}

/** Exit code of the verifier. 0 means it verified. */
let lastStderr = '';
function verify(docPath, keyArg) {
  try {
    execFileSync(process.execPath, [SCRIPT, docPath, keyArg], { stdio: 'pipe' });
    lastStderr = '';
    return 0;
  } catch (err) {
    lastStderr = String(err.stderr ?? '');
    return err.status ?? -1;
  }
}

const good = write('good.json', signed(doc));
check('a genuine export verifies', verify(good, pemPath), 0);

// The whole reason for a canonical form. Re-serialised with its keys in a
// different order and its numbers written differently, it is the same document
// and must still verify. A byte comparison fails this; only canonicalisation
// passes it.
const shuffled = JSON.parse(JSON.stringify(signed(doc)));
shuffled.export = {
  generated_at: doc.generated_at,
  totals: { halted: 1, cost: 84.1, runs: 1 },
  runs: doc.runs,
  period: { to: doc.period.to, from: doc.period.from },
  workspace: doc.workspace,
  version: 1,
};
check('the same document, keys reordered, still verifies',
  verify(write('shuffled.json', shuffled), pemPath), 0);

// ── the four ways it must fail ──────────────────────────────────────────────
const tampered = JSON.parse(JSON.stringify(signed(doc)));
tampered.export.runs[0].total_cost = 8.1;
check('a tampered cost fails', verify(write('tampered.json', tampered), pemPath) !== 0, true);

const unhalted = JSON.parse(JSON.stringify(signed(doc)));
unhalted.export.runs[0].halted = false;
check('a flipped halt flag fails', verify(write('unhalted.json', unhalted), pemPath) !== 0, true);

const addedRun = JSON.parse(JSON.stringify(signed(doc)));
addedRun.export.runs.push({ adw_id: 'adw_fake', total_cost: 0 });
check('an inserted run fails', verify(write('added.json', addedRun), pemPath) !== 0, true);

const droppedKey = JSON.parse(JSON.stringify(signed(doc)));
delete droppedKey.export.totals;
check('a removed section fails', verify(write('dropped.json', droppedKey), pemPath) !== 0, true);

check('the wrong public key fails', verify(good, wrongPemPath) !== 0, true);
check('a signature made with the wrong private key fails',
  verify(write('wrongsig.json', signed(doc, other.privateKey)), pemPath) !== 0, true);

const bentSig = JSON.parse(JSON.stringify(signed(doc)));
const raw = Buffer.from(bentSig.signature.value, 'base64');
raw[0] ^= 0xff;
bentSig.signature.value = raw.toString('base64');
check('a bent signature fails', verify(write('bent.json', bentSig), pemPath) !== 0, true);

const noSig = { export: doc };
check('no signature at all fails', verify(write('nosig.json', noSig), pemPath) !== 0, true);

// "Nobody signed this" and "somebody signed something else" are different
// accusations to put in front of an auditor, so the empty case is checked on
// its message and not only on its exit code.
const emptySig = signed(doc);
emptySig.signature.value = '';
check('an empty signature fails', verify(write('emptysig.json', emptySig), pemPath) !== 0, true);
check('and is reported as unsigned, not as altered', lastStderr.includes('not signed'), true);

// An unsigned export is never acceptable, and neither is one whose algorithm
// has been swapped for something the verifier does not actually check.
const wrongAlg = signed(doc);
wrongAlg.signature.alg = 'none';
check('alg other than Ed25519 fails', verify(write('alg.json', wrongAlg), pemPath) !== 0, true);

check('a file that is not an export fails',
  verify(write('junk.json', { hello: 'world' }), pemPath) !== 0, true);

// ── the shapes a public key actually arrives in ─────────────────────────────
// /v1/export/pubkey answers with JSON, and people will save that whole answer
// to a file rather than dig the key out of it.
const rawKey = publicKey.export({ type: 'spki', format: 'der' }).subarray(-32);
const rawPath = join(dir, 'pub.b64');
writeFileSync(rawPath, rawKey.toString('base64') + '\n');
check('a bare base64 Ed25519 key works', verify(good, rawPath), 0);

const spkiPath = join(dir, 'pub.spki.b64');
writeFileSync(spkiPath, publicKey.export({ type: 'spki', format: 'der' }).toString('base64'));
check('a base64 SPKI key works', verify(good, spkiPath), 0);

const jsonKeyPath = write('pubkey.json', { key_id: KEY_ID, public_key: rawKey.toString('base64') });
check('the /v1/export/pubkey response verbatim works', verify(good, jsonKeyPath), 0);

check('the key given inline rather than as a file works',
  verify(good, rawKey.toString('base64')), 0);

check('a key that is not a key fails', verify(good, 'not-a-key') !== 0, true);
check('a missing export file fails', verify(join(dir, 'nope.json'), pemPath) !== 0, true);

// Usage errors are worth telling apart from "this export is forged".
let usage = 0;
try {
  execFileSync(process.execPath, [SCRIPT], { stdio: 'pipe' });
} catch (err) { usage = err.status; }
check('no arguments is a usage error, not a verdict', usage, 2);

// The point of shipping in the MIT repo: it must run on a bare Node install.
const src = (await import('node:fs')).readFileSync(SCRIPT, 'utf8');
check('the verifier imports nothing outside node:',
  [...src.matchAll(/^\s*import[^;]*from\s+['"]([^'"]+)['"]/gm)]
    .map((m) => m[1]).filter((s) => !s.startsWith('node:')), []);

console.log();
if (failures.length) {
  console.log(`${failures.length} FAILED`);
  for (const f of failures) console.log('  - ' + f);
  process.exit(1);
}
console.log('ALL GREEN');
