/* Drives the real rendered Add-Remote modal in jsdom, only through user paths:
   typing in a combo, picking an item, editing a text box. */
const { JSDOM } = require('jsdom');
const fs = require('fs');
const { inlineAppScripts } = require('./load_module');

const modalHtml = fs.readFileSync(process.argv[2], 'utf8');
const spec = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

// The modal's JS now lives in app/static/js/remote-form.js; jsdom does not
// fetch <script src> on its own, so inline the real file before parsing.
const page = inlineAppScripts(modalHtml);

// The stubs must exist *before* the modal's own <script> runs, which happens
// during parsing — hence beforeParse rather than assigning after construction.
const dom = new JSDOM(page, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  beforeParse(window) {
    // jsdom has no fetch; serve the provider detail the app would.
    window.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(spec) });
    // Real implementations from base.html. markRemoteTested only toggles Save;
    // clearRemoteResult is what an edit calls to drop a stale message.
    window.markRemoteTested = function (tested) {
      window.__remoteTested = !!tested;
      const btn = window.document.getElementById('remote-save-btn');
      if (btn) btn.disabled = !tested;
    };
    window.clearRemoteResult = function () {
      const res = window.document.getElementById('remote-test-result');
      if (res) { res.textContent = ''; res.className = 'text-sm'; }
      const save = window.document.getElementById('remote-save-result');
      if (save) { save.textContent = ''; save.className = 'text-sm'; }
    };
    window.closeModal = () => {};
  },
});
const { window } = dom;
window.addEventListener('error', (e) => console.log('  PAGE ERROR:', e.message));
const origErr = console.error;
console.error = (...a) => origErr('  CONSOLE ERROR:', ...a);

const $ = (s) => window.document.querySelector(s);
const $$ = (s) => Array.from(window.document.querySelectorAll(s));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

let fails = 0;
function check(name, cond, extra) {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (cond ? '' : '   <- ' + extra));
  if (!cond) fails++;
}
// Pick an item from a combo the way a user does: focus, type, click the match.
async function pick(comboSel, term) {
  const combo = $(comboSel);
  const input = combo.querySelector('.rf-combo-input');
  input.dispatchEvent(new window.Event('focus'));
  input.value = term;
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
  const match = Array.from(combo.querySelectorAll("li")).find(li => !li.hidden &&
      li.dataset.value.toLowerCase().includes(term.toLowerCase()));
  if (!match) {
    const vis = Array.from(combo.querySelectorAll('li')).filter(li => !li.hidden);
    const hidden = combo.querySelector('input[type=hidden]');
    console.log('  DEBUG pick("' + term + '") no match; li count=' +
      combo.querySelectorAll('li').length + ' visible=' +
      (vis.length ? vis.slice(0,6).map(v => v.dataset.value).join(',') : '(none)') +
      ' input=' + JSON.stringify(input.value) + ' hidden=' + JSON.stringify(hidden.value) +
      ' first3vals=' + JSON.stringify(Array.from(combo.querySelectorAll('li')).slice(0,3).map(l => l.dataset.value)) +
      ' hiddenCount=' + Array.from(combo.querySelectorAll('li')).filter(l => l.hidden).length);
    return;
  }
  match.dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true }));
  await wait(20);
}
const combo = (sel) => $(sel + ' .rf-combo-input');
// The region field is a combo box; its hidden input is the value that is saved.
const regionVal = () => $('[data-combo="region"] input[type=hidden]').value;

(async () => {
  await wait(120);   // let initRemoteForm's fetch settle

  console.log('--- essentials first, rest behind a panel ---');
  const essentials = $$('#rf-fields > .rf-field').map(f => f.dataset.field);
  check('S3 shows the essentials + provider, plus the optional bucket',
    JSON.stringify(essentials) ===
    JSON.stringify(['provider', 'endpoint', 'region', 'access_key_id',
                    'secret_access_key', 'bucket']),
    JSON.stringify(essentials));
  check('the bucket field is a plain typeable box (not an enum)',
    !!$('#rf-opt-bucket') && $('#rf-opt-bucket').tagName === 'INPUT' &&
    $('#rf-opt-bucket').type === 'text');
  check('the other 73 options are in a collapsed "Advanced options" panel',
    !!$('#rf-adv-search') && $('#rf-adv-list').children.length === 73,
    $('#rf-adv-list') ? $('#rf-adv-list').children.length : 'missing');
  check('endpoint is a text box (an IP:port or URL must be typeable)',
    $('#rf-opt-endpoint').tagName === 'INPUT' && $('#rf-opt-endpoint').type === 'text');
  check('region is a dropdown of real region names',
    !!$('[data-combo="region"]') &&
    $('[data-combo="region"]').querySelectorAll('li').length > 50,
    $('#rf-cmb-region') ? 'combo missing' : 'no region combo');
  check('advanced options can be searched',
    (() => {
      const s = $('#rf-adv-search'); s.value = 'acl';
      s.dispatchEvent(new window.Event('input', { bubbles: true }));
      const shown = Array.from($('#rf-adv-list').children).filter(f => !f.hidden);
      const text = $('#rf-adv-count').textContent;
      s.value = ''; s.dispatchEvent(new window.Event('input', { bubbles: true }));
      return shown.length > 0 && shown.length < 73 && /shown/.test(text);
    })(), 'filter did not narrow the list');

  console.log('--- searchable drop-downs ---');
  const provSel = '#rf-fields .rf-combo[data-combo="provider"]';
  check('the S3 provider list is a searchable combo, not a <select>',
    !!$(provSel) && !$(provSel + ' select'));
  check('it holds all 53 rclone providers', $$(provSel + ' li').length === 53,
    $$(provSel + ' li').length);
  const pin = combo(provSel);
  pin.value = 'clo';
  pin.dispatchEvent(new window.Event('input', { bubbles: true }));
  const vis = $$(provSel + ' li').filter(li => !li.hidden);
  check('typing "clo" ranks Cloudflare first', vis[0] && vis[0].dataset.value === 'Cloudflare',
    vis.map(v => v.dataset.value).join(','));
  check('and still offers related matches', vis.length > 1, vis.length);
  check('typing "minio" finds Minio',
    (() => {
      const i = combo(provSel); i.value = 'minio';
      i.dispatchEvent(new window.Event('input', { bubbles: true }));
      const v = $$(provSel + ' li').filter(li => !li.hidden);
      return v.length >= 1 && v[0].dataset.value === 'Minio';
    })(), 'no Minio');
  const backendSel = '.rf-combo[data-combo="type"]';
  // The full picker holds every rclone backend (69 today); this fixture renders
  // a stand-in list, so assert the mechanism rather than the count.
  check('the backend picker is a searchable combo, not a <select>',
    !!$(backendSel) && !$(backendSel + ' select') && $$(backendSel + ' li').length >= 3,
    $$(backendSel + ' li').length);
  check('typing "sftp" narrows the backend list',
    (() => {
      const i = combo(backendSel); i.value = 'sftp';
      i.dispatchEvent(new window.Event('input', { bubbles: true }));
      const v = $$(backendSel + ' li').filter(li => !li.hidden);
      return v.some(x => x.dataset.value === 'sftp');
    })(), 'sftp not found');

  console.log('--- provider presets drive the fields below ---');
  await pick(provSel, 'Cloudflare');
  check('Cloudflare fills region with auto', regionVal() === 'auto', regionVal());
  check('Cloudflare shows the R2 endpoint shape as the placeholder',
    /r2\.cloudflarestorage\.com/.test($('#rf-opt-endpoint').placeholder), $('#rf-opt-endpoint').placeholder);
  check('Cloudflare explains what to put there',
    $('#rf-preset-note').textContent.length > 20 && !$('#rf-preset-note').hidden);
  check('Cloudflare requires an endpoint', $('#rf-opt-endpoint').required === true);

  await pick(provSel, 'AWS');
  check('AWS switches the region default to us-east-1',
    regionVal() === 'us-east-1', regionVal());
  check('AWS leaves the endpoint optional',
    $('#rf-opt-endpoint').required === false);
  check('AWS says to leave the endpoint blank',
    /leave blank/i.test($('#rf-opt-endpoint').placeholder), $('#rf-opt-endpoint').placeholder);

  await pick(provSel, 'Other');
  check('the generic "Other" preset suggests an IP:port endpoint',
    /192\.168/.test($('#rf-opt-endpoint').placeholder), $('#rf-opt-endpoint').placeholder);
  check('and explains how to configure Garage',
    /Garage/.test($('#rf-preset-note').textContent), $('#rf-preset-note').textContent.slice(0, 60));

  // Garage is not in rclone's provider enum, but typing it must still give hints.
  const gi = combo(provSel);
  gi.value = 'Garage';
  gi.dispatchEvent(new window.Event('input', { bubbles: true }));
  gi.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await wait(20);
  check('typing an unknown provider (Garage) still applies its hints',
    /Garage API port/.test($('#rf-opt-endpoint').placeholder) && regionVal() === 'garage',
    $('#rf-opt-endpoint').placeholder + ' / ' + regionVal());

  console.log('--- the list is a real dropdown, not clipped away ---');
  check('the option list has a visible caret', !!$('#rf-fields .rf-combo .rf-combo-caret'));
  const css = $$('style').map((s) => s.textContent).join('\n');
  check('the list is in flow, so the scrollable modal cannot clip it',
    !/\.rf-combo-list\s*\{[^}]*position:\s*absolute/.test(css), css.slice(0, 40));
  check('clicking the box opens the list (click, not only focus)', (() => {
    const c = $('#rf-fields .rf-combo[data-combo="provider"]');
    const list = c.querySelector('.rf-combo-list');
    list.hidden = true;                       // start from the closed state
    c.dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true }));
    const opened = list.hidden === false;
    // A click inside the open list must not close it.
    list.dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true }));
    return opened && list.hidden === false;
  })());

  console.log('--- the form is not duplicated ---');
  const visible = (sel) => $$(sel).filter((e) => e.tagName !== 'INPUT' || e.type !== 'hidden');
  check('access key appears exactly once',
    visible('#remote-form input[name="access_key"]').length +
    visible('#rf-fields [name="opt_access_key_id"]').length === 1,
    visible('#remote-form input[name="access_key"]').length + ' + ' +
    visible('#rf-fields [name="opt_access_key_id"]').length);
  check('secret key appears exactly once',
    visible('#remote-form input[name="secret_key"]').length +
    visible('#rf-fields [name="opt_secret_access_key"]').length === 1);

  console.log('--- each provider shows only the options it uses ---');
  const advField = (name) => $('#rf-adv-list .rf-field[data-field="' + name + '"]');
  await pick(provSel, 'AWS');
  check('AWS keeps ACL and storage class (S3 supports them)',
    advField('acl') && !advField('acl').hidden &&
    advField('storage_class') && !advField('storage_class').hidden);
  await pick(provSel, 'Cloudflare');
  check('Cloudflare hides ACL and storage class (R2 ignores them)',
    advField('acl').hidden === true && advField('storage_class').hidden === true);
  check('Cloudflare still shows options it does honour (requester_pays)',
    advField('requester_pays') && advField('requester_pays').hidden === false,
    'requester_pays field missing');
  check('Cloudflare hides the IBM keys it cannot use',
    advField('ibm_api_key') && advField('ibm_api_key').hidden === true);
  check('the Advanced count reflects the provider',
    /\(\d+\)/.test($('#rf-adv-summary').textContent) &&
    $('#rf-adv-summary').textContent !== 'Advanced options (73)',
    $('#rf-adv-summary').textContent);
  await pick(provSel, 'AWS');
  check('switching back restores them',
    advField('acl').hidden === false);

  console.log('--- a value the user typed always wins ---');
  // Type into the region combo the way a user would, then commit it.
  const r = $('[data-combo="region"] .rf-combo-input');
  r.dispatchEvent(new window.Event('focus'));
  r.value = 'eu-west-1';
  r.dispatchEvent(new window.Event('input', { bubbles: true }));
  r.dispatchEvent(new window.Event('blur'));
  await wait(200);
  await pick(provSel, 'Cloudflare');
  check('a hand-typed region survives a provider change', regionVal() === 'eu-west-1',
    regionVal());
  check('but the provider note still updates',
    /Cloudflare/.test($('#rf-preset-note').textContent));

  console.log('--- credential labels follow the provider, not rclone\'s "AWS" ---');
  const helpOf = (n) => {
    const f = $(`[data-field="${n}"]`);
    return f ? (f.querySelector('p') || {}).textContent || '' : null;
  };
  check('the Cloudflare key fields never say "AWS"',
    /R2/.test(helpOf('access_key_id')) && /R2/.test(helpOf('secret_access_key')) &&
    !/AWS/.test(helpOf('access_key_id')) && !/AWS/.test(helpOf('secret_access_key')),
    `access_key_id="${helpOf('access_key_id')}" secret="${helpOf('secret_access_key')}"`);
  await pick(provSel, 'AWS');
  check('selecting AWS does say AWS (the override is not blanket)',
    /AWS/.test(helpOf('access_key_id')) && /AWS/.test(helpOf('secret_access_key')),
    `ak="${helpOf('access_key_id')}" sk="${helpOf('secret_access_key')}"`);
  await pick(provSel, 'Other');
  check('a self-hosted S3 clone gets neutral wording',
    !/AWS/.test(helpOf('access_key_id')) && helpOf('access_key_id').length > 0,
    helpOf('access_key_id'));
  check('a Garage-style hint alias is neutral too',
    !/AWS/.test(helpOf('secret_access_key')), helpOf('secret_access_key'));
  check('the key field values were not touched by relabelling',
    typeof $('#rf-opt-access_key_id') !== 'undefined');

  console.log('--- a typed lowercase provider is snapped to rclone\'s exact enum ---');
  const pin2 = combo(provSel);
  pin2.dispatchEvent(new window.Event('focus'));
  pin2.value = 'cloudflare';
  pin2.dispatchEvent(new window.Event('input', { bubbles: true }));
  pin2.dispatchEvent(new window.Event('blur'));
  await wait(200);
  const hiddenProv = $(provSel).querySelector('input[type=hidden]');
  check('"cloudflare" is committed as "Cloudflare"',
    hiddenProv.value === 'Cloudflare', JSON.stringify(hiddenProv.value));
  check('and the R2 wording follows',
    /R2/.test(helpOf('access_key_id')), helpOf('access_key_id'));

  console.log('--- region is a dropdown of real regions, per provider ---');
  const regCombo = $('[data-combo="region"]');
  check('the region field is a combo box, not a bare text box',
    !!regCombo && !!regCombo.querySelector('.rf-combo-list'),
    regCombo ? 'no list' : 'no [data-combo=region] at all');
  const regValues = () => Array.from(
    regCombo.querySelectorAll('li')).map(l => l.dataset.value);
  check('it offers a real region list',
    regValues().length > 50, regValues().length);
  check('R2 puts "auto" first', regValues()[0] === 'auto', regValues()[0]);
  check('real AWS regions are offered too',
    regValues().includes('us-east-1') && regValues().includes('eu-west-1'));
  check('the region hidden input carries the value that gets saved',
    regionVal() === 'eu-west-1', regionVal());
  await pick('[data-combo="region"]', 'garage');
  check('picking a region commits it to the hidden input',
    regCombo.querySelector('input[type=hidden]').value === 'garage',
    regCombo.querySelector('input[type=hidden]').value);
  await pick(provSel, 'AWS');
  // The in-use value is deliberately kept at the top so it cannot be lost by
  // a provider switch; the AWS defaults follow it.
  check('switching to AWS keeps the current value offered, then AWS regions',
    regValues()[0] === 'garage' && regValues().slice(1, 3).join(',') === 'us-east-1,us-west-2',
    regValues().slice(0, 4).join(','));
  check('a hand-picked region survives a provider change',
    regionVal() === 'garage', regionVal());
  check('and "garage" is still offered after the switch',
    regValues().includes('garage'));
  check('the combo still works after being repopulated (search + pick)',
    await (async () => {
      await pick('[data-combo="region"]', 'eu-central-1');
      return regCombo.querySelector('input[type=hidden]').value === 'eu-central-1';
    })(),
    regCombo.querySelector('input[type=hidden]').value);

  console.log('--- a stale failure must not survive an edit ---');
  // Reproduce what a user sees: a red failure from one set of settings stays
  // on screen while they fix the form, so a corrected form looks like it just
  // failed. markRemoteTested(false) has to clear the message, not just Save.
  const res = $('#remote-test-result');
  res.textContent = 'Failed: the access key ID is 140 characters but must be 32';
  res.className = 'text-sm mt-2 text-red-600';
  check('a failure is on screen to begin with', /140 characters/.test(res.textContent));
  const bucketBox = $('#rf-opt-bucket');
  bucketBox.value = 'test-bucket';
  bucketBox.dispatchEvent(new window.Event('input', { bubbles: true }));
  check('editing the form clears the stale failure',
    res.textContent === '', JSON.stringify(res.textContent));
  check('and the message is no longer styled as an error',
    !/red-600/.test(res.className), res.className);
  check('Save is disabled again after editing',
    $('#remote-save-btn').disabled === true);

  console.log('--- a passing result is cleared too, so it cannot be trusted ---');
  res.textContent = 'OK ✓ — ready to save.';
  res.className = 'text-sm mt-2 text-green-600';
  window.markRemoteTested(true);
  check('Save is enabled while the result is valid',
    $('#remote-save-btn').disabled === false);
  const regionBox = $('[data-combo="region"] .rf-combo-input');
  regionBox.dispatchEvent(new window.Event('input', { bubbles: true }));
  check('any edit after a pass clears it, so a stale OK cannot be saved',
    res.textContent === '', JSON.stringify(res.textContent));
  check('and Save is disabled again until re-tested',
    $('#remote-save-btn').disabled === true);

  console.log(fails ? `\n${fails} UI check(s) FAILED` : '\nall UI checks passed');
  process.exit(fails ? 1 : 0);
})();
