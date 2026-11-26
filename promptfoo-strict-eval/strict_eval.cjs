/**
 * Strict eval mode — reference implementation (promptfoo #4321).
 *
 * Two config mistakes make promptfoo evals report 100% PASS while testing
 * nothing:
 *   1. Zero assertions: a test case with no `assert` (and no
 *      `defaultTest.assert`) has no grading criteria and defaults to PASS.
 *   2. Typo'd top-level key: writing `assert:` at the top level (valid key is
 *      `defaultTest.assert`) is silently stripped by the non-strict zod
 *      schema, so the intended gate never runs.
 *
 * This module implements the opt-in `--strict` gate proposed in the comment:
 *   - unknownKeyCheck: diff raw vs parsed top-level keys; unknown keys are
 *     reported, with a hint when the key matches a known misplaced one.
 *   - zeroAssertionCheck: in strict mode a test case whose merged assertions
 *     are empty FAILS instead of PASSing.
 *   - Mode is read from an explicit flag or the PROMPTFOO_STRICT env var
 *     ("1" enables). Default (off) is a no-op passthrough.
 *
 * Self-contained: standard JavaScript only, no dependencies.
 */

'use strict';

const KNOWN_MISPLACED_KEYS = {
  assert: 'Did you mean `defaultTest.assert`?',
  assertions: 'Did you mean `defaultTest.assert`?',
  defaultTest: 'This is a valid top-level key; check its inner shape.',
};

const STRICT_MODE = Object.freeze({ OFF: 'off', ON: 'on' });

function currentMode(explicit) {
  if (explicit === true) return STRICT_MODE.ON;
  if (explicit === false) return STRICT_MODE.OFF;
  const env = typeof process !== 'undefined' ? process.env.PROMPTFOO_STRICT : undefined;
  return env === '1' || env === 'true' ? STRICT_MODE.ON : STRICT_MODE.OFF;
}

/**
 * Diff raw top-level config keys against the keys that survived parsing.
 * Returns the unknown keys that were stripped, each with an optional hint.
 */
function unknownKeyCheck(rawConfig, parsedConfig) {
  const raw = rawConfig && typeof rawConfig === 'object' ? Object.keys(rawConfig) : [];
  const parsed = new Set(
    parsedConfig && typeof parsedConfig === 'object' ? Object.keys(parsedConfig) : []
  );
  return raw
    .filter((k) => !parsed.has(k))
    .map((k) => ({ key: k, hint: KNOWN_MISPLACED_KEYS[k] || null }));
}

/**
 * Merged assertions for a test case, mirroring engine.ts (~L1194):
 * testCase.assert merged with defaultTest.assert.
 */
function mergedAssertions(testCase, defaultTest) {
  const own = Array.isArray(testCase && testCase.assert) ? testCase.assert : [];
  const inherited =
    defaultTest && Array.isArray(defaultTest.assert) ? defaultTest.assert : [];
  return own.concat(inherited);
}

/**
 * Strict zero-assertion gate. Returns a failure reason when the merged
 * assertion list is empty, or null when the case may pass.
 */
function zeroAssertionCheck(testCase, defaultTest) {
  if (mergedAssertions(testCase, defaultTest).length === 0) {
    return 'no assertions in strict mode';
  }
  return null;
}

/**
 * Full strict gate for one eval run.
 * Returns { ok, problems: [{code, message}] } — ok=false means the run
 * should FAIL under strict mode.
 */
function strictGate(rawConfig, parsedConfig, testCases, defaultTest, mode) {
  if (currentMode(mode) === STRICT_MODE.OFF) {
    return { ok: true, problems: [] };
  }
  const problems = [];
  for (const u of unknownKeyCheck(rawConfig, parsedConfig)) {
    problems.push({
      code: 'unknown_config_key',
      message: `unknown top-level key "${u.key}" was silently stripped${u.hint ? ' — ' + u.hint : ''}`,
    });
  }
  const cases = Array.isArray(testCases) ? testCases : [];
  cases.forEach((tc, i) => {
    const reason = zeroAssertionCheck(tc, defaultTest);
    if (reason) {
      problems.push({
        code: 'zero_assertions',
        message: `test case #${i}: ${reason}`,
      });
    }
  });
  return { ok: problems.length === 0, problems };
}

module.exports = {
  KNOWN_MISPLACED_KEYS,
  STRICT_MODE,
  currentMode,
  unknownKeyCheck,
  mergedAssertions,
  zeroAssertionCheck,
  strictGate,
};
