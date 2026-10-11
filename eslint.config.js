/* ESLint 9 flat config — https://eslint.org/docs/latest/use/configure/configuration-files
 *
 * Scope, and what is deliberately NOT linted:
 *
 *   tests/ui/*.js      Node CommonJS jsdom harnesses (run by pytest via `node`).
 *   app/static/js/*.js Hand-written browser scripts served from /static/js.
 *                      Each MUST be wrapped in an IIFE — see the note below.
 *
 * Not linted on purpose:
 *   * Inline <script> blocks inside app/templates/*.html. They are embedded in
 *     Jinja markup and cannot be parsed as JS without a templating plugin. They
 *     are guarded instead by tests/test_htmx_scripts.py, which renders the real
 *     template and evaluates the script twice.
 *   * app/static/vendor/*. The htmx bundle is third-party and minified.
 *
 * IIFE rule (this is the bug class that bit us): htmx re-executes a swapped
 * partial's <script> every time the fragment is swapped in, so a top-level
 * `const`/`let`/`class` throws "Identifier has already been declared" on the
 * second open and the script dies silently. Both modals hit this. Any new file
 * in app/static/js must therefore be `(function () { ... })();`.
 */
const globals = require('globals');

module.exports = [
  {
    // Build/tooling output and vendored code — never our source.
    ignores: [
      'node_modules/',
      '.venv/',
      '__pycache__/',
      'data/',
      'app/static/vendor/',
      '.pytest_cache/',
      'package-lock.json',
    ],
  },

  // Node-side test harnesses.
  {
    files: ['tests/ui/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'commonjs',
      globals: { ...globals.node },
    },
    rules: {
      ...require('@eslint/js').configs.recommended.rules,

      // Harnesses report PASS/FAIL lines and page errors on stdout; that is
      // the whole point of them.
      'no-console': 'off',
      'no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      eqeqeq: ['error', 'smart'],
      'prefer-const': 'error',
      'no-var': 'error',
    },
  },

  // Browser-side scripts served from /static/js.
  {
    files: ['app/static/js/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'script',
      globals: {
        ...globals.browser,
        htmx: 'readonly',

        // Defined in app/templates/base.html (the shared shell) and consumed
        // here. Still inline, so ESLint cannot see them — declared explicitly
        // rather than silenced, so the coupling is documented instead of hidden.
        // Both are called behind a `typeof` guard, so a missing definition
        // degrades quietly instead of throwing.
        markRemoteTested: 'readonly',
        clearRemoteResult: 'readonly',
      },
    },
    rules: {
      ...require('@eslint/js').configs.recommended.rules,

      'no-console': ['warn', { allow: ['warn', 'error'] }],
      'no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      eqeqeq: ['error', 'smart'],

      // These run in the browser only, so top-level `let`/`const` is legal
      // ES — but it is banned here anyway because of the htmx re-execution
      // trap described at the top of this file. Use `var` or an IIFE instead.
      'no-var': 'off',
      'prefer-const': 'off',

      // DOM properties (`form.type`, `input.name`) silently shadow named
      // controls. The codebase routes around this with a formField() helper;
      // this rule keeps the habit from decaying.
      'no-shadow-restricted-names': 'error',
    },
  },
];