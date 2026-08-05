#!/usr/bin/env node
/**
 * Verify a signed Flightbox Cloud audit export.
 *
 *   node scripts/verify-export.mjs <export.json> <public-key>
 *
 * Exit 0 when the signature covers exactly the document in the file, non-zero
 * otherwise. No dependencies, no network, no licence check: generating the
 * export is the paid act, but a proof that can only be checked by paying is not
 * a proof. This ships MIT so a compliance reviewer who has never heard of
 * Flightbox can confirm the record with a stock Node install.
 *
 * <public-key> may be a path or the key itself, as a PEM, a base64 SPKI, a bare
 * base64 Ed25519 key, or the JSON body of GET /v1/export/pubkey saved verbatim.
 * A reviewer is handed whatever the person being audited had lying around, and
 * refusing four of the five shapes helps nobody.
 *
 * What is signed
 * --------------
 * The signature covers the canonical serialisation of the `export` object, not
 * the bytes of the file. Object keys ascend by UTF-16 code unit, arrays keep
 * their order (it is content), there is no insignificant whitespace, and numbers
 * take the shortest form that round-trips, so 84.10 and 84.1 are one document
 * and not two. Re-indent the file, reorder its keys, hand it through a formatter
 * — it still verifies. Change one cost and it does not.
 *
 * This is written from the specification rather than copied from the generator.
 * Two independent implementations that agree are evidence; one implementation
 * checked against itself is a tautology, and if they ever disagree that is a
 * real bug in the export, found here rather than by a customer.
 */

import { createPublicKey, verify as edVerify } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const USAGE = 2;   // told apart from a verdict on purpose: 1 means forged
const FAIL = 1;

/** RFC 8785-style canonical JSON. Deterministic, so a signature over it means something. */
export function canonicalize(value) {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error(`not serialisable: ${value}`);
    // String() is the shortest round-tripping form, which is what makes 84.10
    // and 84.1 one document rather than two. It also renders -0 as "0", so the
    // sign JSON cannot represent cannot produce a second encoding of zero.
    return String(value);
  }
  if (typeof value === 'string') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(',')}]`;
  if (typeof value === 'object') {
    const keys = Object.keys(value).sort();      // UTF-16 code unit order
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalize(value[k])}`).join(',')}}`;
  }
  throw new Error(`not serialisable: ${typeof value}`);
}

const SPKI_ED25519_PREFIX = Buffer.from('302a300506032b6570032100', 'hex');

/** Read whatever the reviewer was given and turn it into a KeyObject. */
export function readPublicKey(arg) {
  let text = arg;
  try {
    text = readFileSync(arg, 'utf8');
  } catch {
    // Not a path, so treat the argument as the key itself.
  }
  text = text.trim();
  if (text.includes('BEGIN PUBLIC KEY')) return createPublicKey(text);
  if (text.startsWith('{')) {
    const body = JSON.parse(text);
    const inner = body.public_key ?? body.publicKey ?? body.key;
    if (!inner) throw new Error('that JSON carries no public_key');
    return readPublicKey(String(inner));
  }
  const der = Buffer.from(text.replace(/\s+/g, ''), 'base64');
  // A bare Ed25519 key is 32 bytes of nothing in particular; wrapping it in the
  // one fixed SPKI header is what lets node read it.
  if (der.length === 32) {
    return createPublicKey({
      key: Buffer.concat([SPKI_ED25519_PREFIX, der]), format: 'der', type: 'spki',
    });
  }
  return createPublicKey({ key: der, format: 'der', type: 'spki' });
}

export function verifyDocument(doc, key) {
  if (!doc || typeof doc !== 'object' || !doc.export || typeof doc.export !== 'object') {
    throw new Error('not an export: no `export` object');
  }
  const sig = doc.signature;
  if (!sig || typeof sig !== 'object' || typeof sig.value !== 'string' || !sig.value) {
    throw new Error('not signed: no `signature.value`');
  }
  // An unsigned export must never pass, and neither must one whose algorithm
  // was swapped for something this script does not actually check.
  if (sig.alg !== 'Ed25519') throw new Error(`unsupported alg ${JSON.stringify(sig.alg)}`);
  const message = Buffer.from(canonicalize(doc.export), 'utf8');
  return edVerify(null, message, key, Buffer.from(sig.value, 'base64'));
}

function main(argv) {
  const [docPath, keyArg] = argv;
  if (!docPath || !keyArg) {
    console.error('usage: node scripts/verify-export.mjs <export.json> <public-key>');
    return USAGE;
  }
  let doc;
  try {
    doc = JSON.parse(readFileSync(docPath, 'utf8'));
  } catch (err) {
    console.error(`cannot read ${docPath}: ${err.message}`);
    return USAGE;
  }
  let key;
  try {
    key = readPublicKey(keyArg);
  } catch (err) {
    console.error(`cannot read public key: ${err.message}`);
    return USAGE;
  }
  let ok;
  try {
    ok = verifyDocument(doc, key);
  } catch (err) {
    console.error(`INVALID: ${err.message}`);
    return FAIL;
  }
  if (!ok) {
    console.error('INVALID: the signature does not cover this document. It has been '
      + 'altered, or it was signed by a different key.');
    return FAIL;
  }
  const e = doc.export;
  const period = e.period ? `${e.period.from} to ${e.period.to}` : 'unknown period';
  const t = e.totals ?? {};
  console.log(`VERIFIED  key_id=${doc.signature.key_id ?? 'unstated'}`);
  console.log(`  workspace: ${e.workspace?.slug ?? e.workspace?.id ?? 'unstated'}`);
  console.log(`  period:    ${period}`);
  console.log(`  runs:      ${t.runs ?? '?'} (${t.halted ?? '?'} halted), cost ${t.cost ?? '?'}`);
  return 0;
}

// Importable by the test, runnable as a script. pathToFileURL rather than
// string surgery, because a Windows path is not a URL.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exit(main(process.argv.slice(2)));
}
