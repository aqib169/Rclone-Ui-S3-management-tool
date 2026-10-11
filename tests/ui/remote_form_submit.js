/* Drives Test First the way a user does: fill the form, click the real button.
   Loads base.html's real functions, because the modal fragment does not carry
   them — the previous harness stubbed markRemoteTested out, so a bug where
   the test wrote a message and then wiped it went unnoticed. */
const { JSDOM } = require('jsdom');
const fs = require('fs');
const { inlineAppScripts } = require('./load_module');

const modal = fs.readFileSync(process.argv[2], 'utf8');
const spec = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const base = fs.readFileSync(process.argv[4], 'utf8');

let nextResponse = { ok: true, buckets: [], warning: 'Bucket reachable.' };
const posted = [];
// Inline app/static/js/*.js first (jsdom does not fetch <script src>), then
// append base.html's shared functions exactly as the page would load them.
const page = inlineAppScripts(modal)
  + '<script>' + base.replace(/<\//g, '<\\/') + '</script>';
const dom = new JSDOM(page, {
  runScripts: 'dangerously', pretendToBeVisual: true,
  beforeParse(w) {
    w.htmx = { config: { allowScriptTags: true }, on() {}, trigger() {} };
    w.fetch = (url, opts) => {
      if (url.includes('/api/providers/')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(spec) });
      }
      if (opts && opts.body) posted.push(JSON.parse(opts.body));
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(nextResponse) });
    };
    w.closeModal = () => {};
  },
});
const { window } = dom;
window.addEventListener('error', (e) => console.log('  PAGE ERROR:', e.message));
const $ = (s) => window.document.querySelector(s);
let fails = 0;
function check(name, cond, extra) {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (cond ? '' : '   <- ' + extra));
  if (!cond) fails++;
}

(async () => {
  await new Promise((r) => setTimeout(r, 400));

  console.log('--- form controls are readable ---');
  // HTMLFormElement has built-in `type`/`name` properties that shadow named
  // controls, so f.type.value used to throw and the test never ran at all.
  const f = $('#remote-form');
  check('a control named "type" is shadowed by the form built-in (why formField exists)',
    f.type === undefined && !!f.elements['type']);
  check('the form still resolves it by name',
    !!window.formField(f, 'type'), 'formField cannot find name=type');
  check('remoteFormData() does not throw', (() => {
    try { window.remoteFormData(); return true; } catch (e) { return e.message; }
  })(), 'threw');

  console.log('--- Save is never disabled without saying why ---');
  const hint = $('#remote-save-hint');
  check('there is a hint next to Save', !!hint, 'no #remote-save-hint');
  check('it explains the initial disabled state',
    /Test First before saving/.test(hint.textContent), hint.textContent);
  check('and Save really is disabled to begin with',
    $('#remote-save-btn').disabled === true);

  console.log('--- filling the form and clicking Test First ---');
  $('#remote-form [name="name"]').value = 'cloudflarearecto';
  $('#rf-fields [name="opt_access_key_id"]').value = 'a'.repeat(32);
  $('#rf-fields [name="opt_secret_access_key"]').value = 'b'.repeat(64);
  $('#rf-fields [name="opt_endpoint"]').value = 'https://' + '0'.repeat(32) + '.r2.cloudflarestorage.com';
  $('#rf-opt-bucket').value = 'test-bucket';
  $('[data-combo="provider"] input[type=hidden]').value = 'Cloudflare';
  $('[data-combo="region"] input[type=hidden]').value = 'auto';

  const res = $('#remote-test-result');
  const btn = window.document.querySelector('button[onclick*="testRemoteInline"]');
  check('the Test First button exists and is wired', !!btn);
  btn.click();
  await new Promise((r) => setTimeout(r, 500));

  check('a passing test shows a result', /OK/.test(res.textContent), JSON.stringify(res.textContent));
  check('and enables Save', $('#remote-save-btn').disabled === false);
  check('the payload carried the bucket', posted.length && posted[0].bucket === 'test-bucket',
    JSON.stringify(posted[0] || {}).slice(0, 120));
  check('the payload carried the 32-char key', posted.length && posted[0].options.access_key_id === 'a'.repeat(32));

  console.log('--- a failure must stay on screen ---');
  // The regression: the handler wrote the error and then markRemoteTested(false)
  // cleared it, so the user saw nothing at all with Save still disabled.
  nextResponse = { ok: false, error: 'Not found - check the endpoint' };
  btn.click();
  await new Promise((r) => setTimeout(r, 500));
  check('the failure message is visible', /Not found/.test(res.textContent), JSON.stringify(res.textContent));
  check('it is styled as an error', /red-600/.test(res.className), res.className);
  check('Save is disabled after a failure', $('#remote-save-btn').disabled === true);

  console.log('--- a network error is reported, not swallowed ---');
  nextResponse = { ok: false, error: 'boom' };
  const origFetch = window.fetch;
  window.fetch = (u, o) => (u.includes('/api/providers/') ? origFetch(u, o) : Promise.reject(new Error('network down')));
  btn.click();
  await new Promise((r) => setTimeout(r, 500));
  check('a rejected fetch still produces a message',
    /network down/.test(res.textContent), JSON.stringify(res.textContent));
  window.fetch = origFetch;

  console.log('--- a wrong paste must be visible, not hidden behind dots ---');
  // A masked field cannot show that a 32-character key id arrived as 140
  // characters, which is what a password manager's autofill produces.
  const key = $('#rf-opt-access_key_id');
  check('the key field opts out of password managers',
    key.getAttribute('autocomplete') === 'new-password', key.getAttribute('autocomplete'));
  check('it has a reveal toggle', !!$('.rf-reveal-btn[data-for="rf-opt-access_key_id"]'));
  check('and it starts masked', key.type === 'password', key.type);
  $('.rf-reveal-btn[data-for="rf-opt-access_key_id"]').click();
  check('clicking show reveals the value', key.type === 'text', key.type);
  key.value = 'a'.repeat(140);
  key.dispatchEvent(new window.Event('input', { bubbles: true }));
  check('a live character count makes the length obvious',
    /140 characters/.test($('.rf-count').textContent), $('.rf-count').textContent);
  key.value = 'a'.repeat(32);
  key.dispatchEvent(new window.Event('input', { bubbles: true }));
  check('and reads 32 for a correct key',
    /32 characters/.test($('.rf-count').textContent), $('.rf-count').textContent);

  console.log('--- the result reports what the server received ---');
  nextResponse = { ok: false, error: 'must be 32', received: "access key id: 140 chars, starting 'eyJhbG'\u2026" };
  btn.click();
  await new Promise((r) => setTimeout(r, 500));
  check('the received summary is shown', /Received/.test(res.textContent), JSON.stringify(res.textContent));
  check('and it names the length actually received', /140 chars/.test(res.textContent));
  check('and a masked prefix, never the secret', /eyJhbG/.test(res.textContent) && !/secret key: 64/.test(res.textContent));

  nextResponse = { ok: true, buckets: [] };
  btn.click();
  await new Promise((r) => setTimeout(r, 500));
  check('after a passing test the hint says it is safe to save',
    /can save/i.test(hint.textContent), hint.textContent);
  $('#rf-opt-bucket').value = 'changed-bucket';
  $('#rf-opt-bucket').dispatchEvent(new window.Event('input', { bubbles: true }));
  check('after an edit the hint says to test again',
    /changed/i.test(hint.textContent), hint.textContent);
  check('and Save is disabled again', $('#remote-save-btn').disabled === true);

  console.log('--- editing the form clears the stale result ---');
  nextResponse = { ok: true, buckets: [] };
  btn.click();
  await new Promise((r) => setTimeout(r, 500));
  check('result shown before editing', /OK/.test(res.textContent));
  $('#rf-opt-bucket').value = 'other-bucket';
  $('#rf-opt-bucket').dispatchEvent(new window.Event('input', { bubbles: true }));
  check('an edit clears it', res.textContent === '', JSON.stringify(res.textContent));

  console.log(fails ? `\n${fails} submit check(s) FAILED` : '\nall submit checks passed');
  process.exit(fails ? 1 : 0);
})();
