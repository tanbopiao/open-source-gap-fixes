'use strict';

const test = require('node:test');
const assert = require('node:assert');
const {
  currentMode,
  unknownKeyCheck,
  zeroAssertionCheck,
  strictGate,
} = require('./strict_eval.cjs');

test('default mode is off even with problems', () => {
  const r = strictGate({ assert: [] }, {}, [{ vars: {} }], null, false);
  assert.equal(r.ok, true);
  assert.deepEqual(r.problems, []);
});

test('mode from env PROMPTFOO_STRICT=1', () => {
  process.env.PROMPTFOO_STRICT = '1';
  try {
    assert.equal(currentMode(undefined), 'on');
  } finally {
    delete process.env.PROMPTFOO_STRICT;
  }
});

test('explicit flag overrides env', () => {
  process.env.PROMPTFOO_STRICT = '1';
  try {
    assert.equal(currentMode(false), 'off');
    assert.equal(currentMode(true), 'on');
  } finally {
    delete process.env.PROMPTFOO_STRICT;
  }
});

test('unknown top-level assert key is reported with hint', () => {
  const raw = { providers: ['echo'], assert: [{ type: 'contains', value: 'OK' }] };
  const parsed = { providers: ['echo'] }; // zod stripped the typo'd key
  const u = unknownKeyCheck(raw, parsed);
  assert.equal(u.length, 1);
  assert.equal(u[0].key, 'assert');
  assert.match(u[0].hint, /defaultTest\.assert/);
});

test('valid keys are not reported', () => {
  const raw = { providers: ['echo'], defaultTest: { assert: [] }, tests: [] };
  assert.deepEqual(unknownKeyCheck(raw, raw), []);
});

test('zero assertions fails in strict mode (repro 1)', () => {
  const tc = { vars: { topic: 'anything' } }; // no assert
  assert.match(zeroAssertionCheck(tc, null), /no assertions in strict mode/);
});

test('inherited defaultTest.assert satisfies the gate', () => {
  const tc = { vars: { topic: 'anything' } };
  const dt = { assert: [{ type: 'contains', value: 'OK' }] };
  assert.equal(zeroAssertionCheck(tc, dt), null);
});

test('own assertions satisfy the gate (repro 2 fixed)', () => {
  const tc = { vars: { topic: 'anything' }, assert: [{ type: 'contains', value: 'OK' }] };
  assert.equal(zeroAssertionCheck(tc, null), null);
});

test('strictGate catches both failure modes in one run', () => {
  const raw = { assert: [{ type: 'contains', value: 'OK' }], tests: [{ vars: {} }] };
  const parsed = { tests: [{ vars: {} }] };
  const r = strictGate(raw, parsed, parsed.tests, null, true);
  assert.equal(r.ok, false);
  assert.deepEqual(
    r.problems.map((p) => p.code).sort(),
    ['unknown_config_key', 'zero_assertions']
  );
});

test('clean strict config passes', () => {
  const raw = {
    providers: ['echo'],
    tests: [{ vars: {}, assert: [{ type: 'contains', value: 'OK' }] }],
  };
  const r = strictGate(raw, raw, raw.tests, null, true);
  assert.equal(r.ok, true);
});
