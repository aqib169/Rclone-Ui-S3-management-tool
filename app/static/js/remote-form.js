/* Add/Edit Remote modal — backend picker, generated option fields, presets.
 *
 * Extracted verbatim from app/templates/modals/remote_form.html. The template now
 * holds only a bootstrap that calls `window.RemoteForm.init(bootstrapData)`.
 *
 * WHY THIS IS A FILE AND NOT AN INLINE BLOCK
 * ------------------------------------------
 * htmx re-executes a swapped partial's <script> every single time the modal is
 * opened, so this code used to be a 542-line inline block that had to be wrapped
 * in an IIFE purely to survive the second open (a top-level `const` throws
 * "Identifier has already been declared" and silently kills the script, leaving
 * the backend fields empty — a bug that really happened).
 *
 * Loaded via <script src> it is fetched ONCE per page, so:
 *   - there is no redeclaration hazard at all, and
 *   - it is linted by `npm run lint` (see eslint.config.js).
 *
 * Everything the template's inline `onclick`/`onsubmit` handlers need is
 * published on `window` below: re-assigning a window property is safe to repeat.
 */
(function () {
  'use strict';

  window.__remoteTested = false;
  var saveBtn = document.getElementById('remote-save-btn');
  if (saveBtn) saveBtn.disabled = true;

  function esc(v) {
    return String(v === undefined || v === null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // A searchable drop-down: type a few letters, pick from the matches.
  // `choices` is [{value, help}]; free text is allowed so unknown values can
  // still be typed (it is only a convenience over knowing the exact name).
  function comboHtml(name, choices, value, placeholder) {
    var id = 'rf-cmb-' + name;
    var items = choices.map(function (c) {
      return '<li role="option" data-value="' + esc(c.value) + '"' +
             (c.value === value ? ' class="is-selected"' : '') + '>' +
             '<span class="cmb-value">' + esc(c.value) + '</span>' +
             (c.help ? '<span class="cmb-help">' + esc(String(c.help).split('\n')[0]) + '</span>' : '') +
             '</li>';
    }).join('');
    return '<div class="rf-combo" data-combo="' + esc(name) + '">' +
      '<span class="rf-combo-input-wrap">' +
        '<input type="text" id="' + id + '" class="rf-combo-input" role="combobox"' +
        ' aria-expanded="false" aria-autocomplete="list" autocomplete="off"' +
        ' placeholder="' + esc(placeholder || 'Start typing to search…') + '"' +
        ' value="' + esc(value || '') + '">' +
        '<span class="rf-combo-caret">▼</span>' +
      '</span>' +
      '<ul class="rf-combo-list" role="listbox" hidden>' + items + '</ul>' +
      '<input type="hidden" name="opt_' + esc(name) + '" value="' + esc(value || '') + '">' +
      '<p class="rf-combo-hint" hidden></p>' +
      '</div>';
  }

  function initCombos(root) {
    (root || document).querySelectorAll('.rf-combo').forEach(function (combo) {
      if (combo.dataset.wired) return;
      combo.dataset.wired = '1';
      var input = combo.querySelector('.rf-combo-input');
      var list = combo.querySelector('.rf-combo-list');
      var hidden = combo.querySelector('input[type=hidden]');
      var hint = combo.querySelector('.rf-combo-hint');
      var all = Array.prototype.slice.call(list.querySelectorAll('li'));
      var active = -1;

      function label(item) { return item.dataset.value.toLowerCase(); }

      function score(item, term) {
        var value = label(item);
        if (!term) return 2;
        if (value === term) return 4;           // exact
        if (value.indexOf(term) === 0) return 3; // starts with what you typed
        if (value.indexOf(term) !== -1) return 2; // inside the name
        if (item.textContent.toLowerCase().indexOf(term) !== -1) return 1; // in the help text
        return 0;
      }

      function filter(term) {
        var t = (term || '').trim().toLowerCase();
        var ranked = all.map(function (item) {
          return { item: item, score: score(item, t) };
        }).filter(function (r) { return r.score > 0; })
          .sort(function (a, b) {
            if (b.score !== a.score) return b.score - a.score;
            return label(a.item).localeCompare(label(b.item));
          });
        all.forEach(function (item) { item.hidden = true; });
        ranked.forEach(function (r) { r.item.hidden = false; });
        // Keep the DOM order in sync with the ranking.
        ranked.forEach(function (r) { list.appendChild(r.item); });
        if (hint) {
          hint.textContent = t && !ranked.length
            ? 'No match — your text will be used as typed.' : '';
          hint.hidden = !(t && !ranked.length);
        }
        return ranked.length;
      }

      function open() {
        filter(input.value);
        list.hidden = false;
        combo.dataset.open = '1';
        input.setAttribute('aria-expanded', 'true');
      }
      function close() {
        list.hidden = true;
        combo.dataset.open = '0';
        input.setAttribute('aria-expanded', 'false');
        active = -1;
      }
      function commit(item) {
        var value = item ? item.dataset.value : input.value.trim();
        // rclone matches the S3 provider enum case-sensitively, so typed
        // "cloudflare" would be rejected. Snap to the real enum value.
        if (!item && combo.dataset.combo === 'provider') {
          var hit = Array.prototype.find.call(list.querySelectorAll('li'), function (li) {
            return String(li.dataset.value).toLowerCase() === value.toLowerCase();
          });
          if (hit) value = hit.dataset.value;
        }
        hidden.value = value;
        input.value = hidden.value;
        close();
        // Selecting a backend re-renders its fields; selecting a provider
        // fills in the defaults that provider needs.
        if (combo.dataset.combo === 'type' && typeof loadBackendFields === 'function') {
          loadBackendFields(hidden.value);
        } else if (combo.dataset.combo === 'provider' && typeof applyProviderPreset === 'function') {
          applyProviderPreset(hidden.value);
        }
      }

      input.addEventListener('focus', open);
      // A click anywhere on the box opens it, not only a focus event: clicking
      // an already-focused input fires no `focus`, and the list looked broken.
      combo.addEventListener('mousedown', function (e) {
        if (e.target.closest('.rf-combo-caret') || e.target.closest('.rf-combo-list')) return;
        if (list.hidden) { e.preventDefault(); open(); }
      });
      input.addEventListener('click', function () { if (list.hidden) open(); });
      input.addEventListener('input', function () {
        hidden.value = input.value.trim();
        open();
      });
      input.addEventListener('blur', function () { setTimeout(function () {
        commit(null); close();
      }, 120); });
      input.addEventListener('keydown', function (e) {
        var visible = all.filter(function (i) { return !i.hidden; });
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          if (list.hidden) { open(); return; }
          var idx = visible.indexOf(visible[active]);
          active = e.key === 'ArrowDown'
            ? Math.min(visible.length - 1, idx + 1)
            : Math.max(0, idx - 1);
          visible.forEach(function (i) { i.classList.remove('is-active'); });
          if (visible[active]) {
            visible[active].classList.add('is-active');
            visible[active].scrollIntoView({ block: 'nearest' });
          }
        } else if (e.key === 'Enter') {
          e.preventDefault();
          commit(list.hidden ? null : (visible[active] || visible[0]));
        } else if (e.key === 'Escape') {
          close();
        }
      });
      list.addEventListener('mousedown', function (e) {
        var item = e.target.closest('li');
        if (item) { e.preventDefault(); commit(item); }
      });
    });
  }

  // One option -> one labelled input. Enumerations and any list longer than a
  // handful of entries become searchable combo boxes.
  // rclone lists "examples" for many options, but for free-form ones (endpoint,
// region, host, …) those are only samples for a particular provider — an IP:port
// or a URL must still be typeable. So these always render as a text box (with
// the samples offered as suggestions); real enumerations become searchable
// combo boxes.
  // Free-form values that must stay typeable. `region` is deliberately NOT
  // here any more: it renders as a dropdown, because a typo or wrong-case
  // region is one of the most common S3 mistakes. rclone lists 128 real region
  // names, so nothing is lost — and a self-hosted server can still have its
  // own value typed, since the combo accepts free text.
  var FREE_TEXT_OPTIONS = ['endpoint', 'location_constraint', 'location',
    'url', 'host', 'remote', 'path', 'account', 'user', 'bucket', 'container',
    'description', 'project_number', 'service_account_file', 'service_account_credentials',
    'key_file', 'key_pem', 'pubkey_file', 'token', 'client_id', 'client_secret',
    'key', 'key2', 'pass', 'password', 'access_key_id', 'secret_access_key',
    'base_url', 'domain', 'server', 'address', 'certificate_authority'];

  function fieldHtml(option, values) {
    var stored = values[option.name];
    var shown = (stored && stored.value !== null && stored.value !== undefined) ? stored.value : '';
    var id = 'rf-opt-' + option.name;
    var label = '<label class="block text-xs text-gray-600" for="' + id + '">' +
                esc(option.name) + (option.required ? ' <span class="text-red-500">*</span>' : '') +
                ' <span class="text-gray-400 font-mono">' + esc(option.type) + '</span></label>';
    var examples = option.examples || [];
    var freeText = FREE_TEXT_OPTIONS.indexOf(option.name) !== -1;
    var input;
    if (option.type === 'bool' || option.type === 'Tristate') {
      input = '<input type="checkbox" id="' + id + '" name="opt_' + option.name + '" value="true"' +
              ((shown === 'true' || stored === true) ? ' checked' : '') + '>';
    } else if (option.name === 'region' && examples.length) {
      // A dropdown of real region names, with the value still typeable.
      input = comboHtml(option.name, examples, shown,
                        'Type or pick a region… (' + examples.length + ')');
    } else if (examples.length && !freeText) {
      input = comboHtml(option.name, examples, shown,
                        'Type to search… (' + examples.length + ')');
    } else {
      // Free-form value: a text box, with the samples as browser suggestions.
      // Credential fields get autocomplete disabled and a reveal toggle. A bare
      // type="password" invites a password manager to fill the *key id* with a
      // saved site password — which is how a 140-character value reached a
      // field that must hold 32 — and it hides the value from the user, so a
      // wrong paste is invisible.
      var secret = !!option.password;
      input = '<input type="' + (secret ? 'password' : 'text') + '" id="' + id +
              '" name="opt_' + option.name + '" maxlength="4096" value="' + esc(secret ? '' : shown) + '"' +
              (secret ? ' autocomplete="new-password" data-secret="1" spellcheck="false" autocapitalize="off"' : '') +
              (option.password && stored ? ' placeholder="(unchanged)"' : '') +
              (examples.length ? ' list="rf-dl-' + esc(option.name) + '"' : '') +
              ' class="mt-1 w-full border border-gray-300 rounded px-3 py-2 text-sm rf-secret">' +
              (secret
                ? '<button type="button" data-for="' + id + '" ' +
                  'title="Show this value" class="rf-reveal-btn">show</button>'
                : '') +
              (examples.length
                ? '<datalist id="rf-dl-' + esc(option.name) + '">' +
                  examples.slice(0, 60).map(function (e) {
                    return '<option value="' + esc(e.value) + '"></option>';
                  }).join('') + '</datalist>'
                : '');
    }
    var help = option.help ? '<p class="text-xs text-gray-400 mt-1">' + esc(String(option.help).split('\n')[0]) + '</p>' : '';
    return '<div data-field="' + esc(option.name) + '" class="rf-field">' + label + input + help + '</div>';
  }

  // Apply the defaults a provider needs (region, endpoint shape, a note).
  // Only fills fields the user has not touched, so custom values always win.
  function applyProviderPreset(providerName) {
    var spec = window.__rfSpec;
    if (!spec || spec.name !== 's3') return;
    var presets = spec.presets || {};
    var preset = presets[providerName];
    if (!preset) {
      for (var known in presets) {
        if (known.toLowerCase() === String(providerName || '').toLowerCase()) {
          preset = presets[known];
          break;
        }
      }
    }
    if (!preset) preset = presets['Other'] || {};
    window.__rfPreset = preset;

    // The region is now a combo, so it has a visible input (rf-cmb-region) and
    // a hidden input that carries the submitted value. Set both.
    var regionInput = document.getElementById('rf-cmb-region') ||
                      document.getElementById('rf-opt-region');
    var regionHidden = document.querySelector('[data-combo="region"] input[type=hidden]');
    if (regionInput) {
      if (!regionInput.dataset.touched && preset.region !== undefined) {
        regionInput.value = preset.region;
        if (regionHidden) regionHidden.value = preset.region;
      }
      regionInput.placeholder = preset.region
        ? 'default: ' + preset.region
        : 'Type or pick a region…';
    }
    // Swap the offered regions so the provider's own value is at the top.
    applyRegionChoices(providerName, preset);
    var endpoint = document.getElementById('rf-opt-endpoint');
    if (endpoint && preset.endpoint_placeholder) {
      endpoint.placeholder = preset.endpoint_placeholder;
      if (!endpoint.dataset.touched && preset.endpoint) endpoint.value = preset.endpoint;
      endpoint.required = !!preset.endpoint_required;
    }
    var note = document.getElementById('rf-preset-note');
    if (note) {
      note.textContent = preset.note || '';
      note.hidden = !preset.note;
    }
    // Repopulate the region dropdown for the chosen provider, so R2 offers
  // "auto" first, Garage offers "garage" first, and AWS offers real regions.
  // The current value is always kept, even if it is a custom one.
  function applyRegionChoices(providerName, preset) {
    var spec = window.__rfSpec;
    var combo = document.querySelector('[data-combo="region"]');
    if (!spec || !combo) return;
    var table = spec.regions || {};
    var choices = table[providerName];
    if (!choices) {
      for (var known in table) {
        if (known.toLowerCase() === String(providerName || '').toLowerCase()) {
          choices = table[known];
          break;
        }
      }
    }
    if (!choices) choices = table[''] || [];
    if (!choices.length) return;

    var input = combo.querySelector('.rf-combo-input');
    var hidden = combo.querySelector('input[type=hidden]');
    var list = combo.querySelector('.rf-combo-list');
    var current = (hidden && hidden.value) || (input && input.value) || '';
    var hint = combo.querySelector('.rf-combo-hint');

    // Make sure the value in use is always offered, so it can be re-picked
    // after switching providers.
    var values = choices.map(function (c) { return c.value; });
    if (current && values.indexOf(current) === -1) {
      choices = [{value: current, help: 'Current value'}].concat(choices);
    }

    list.innerHTML = choices.map(function (c) {
      return '<li role="option" data-value="' + esc(c.value) + '"' +
             (c.value === current ? ' class="is-selected"' : '') + '>' +
             '<span class="cmb-value">' + esc(c.value) + '</span>' +
             (c.help ? '<span class="cmb-help">' + esc(String(c.help).split('\n')[0]) + '</span>' : '') +
             '</li>';
    }).join('');
    // The old wiring captured the previous <li> list, so rebuild the combo.
    combo.dataset.wired = '';
    if (typeof initCombos === 'function') initCombos(combo.parentNode || combo);
    if (input) input.placeholder = 'Type or pick a region… (' + choices.length + ')';
    if (hint) { hint.textContent = ''; hint.hidden = true; }
    void preset;
  }

  // Relabel the shared S3 credential fields: rclone calls them "AWS Access
    // Key ID" for every provider, which is wrong and confusing for R2 or a
    // self-hosted S3 clone.
    applyOptionLabels(providerName);
    // Only show the options this provider actually uses.
    filterAdvancedForProvider(preset.hide || []);
  }

  // Swap the help text under the shared S3 options to match the provider.
  function applyOptionLabels(providerName) {
    var spec = window.__rfSpec;
    if (!spec) return;
    var table = (spec.labels || {});
    var labels = table[providerName];
    if (!labels) {
      for (var known in table) {
        if (known.toLowerCase() === String(providerName || '').toLowerCase()) {
          labels = table[known];
          break;
        }
      }
    }
    if (!labels) labels = table['Other'] || {};
    Object.keys(labels).forEach(function (name) {
      var field = document.querySelector('[data-field="' + name + '"]');
      if (!field) return;
      var help = field.querySelector('p');
      if (help) help.textContent = labels[name];
    });
  }

  // Hide the options a provider does not support, keeping the search filter.
  function filterAdvancedForProvider(hiddenNames) {
    var hidden = {};
    (hiddenNames || []).forEach(function (n) { hidden[n] = true; });
    window.__rfHidden = hidden;
    var search = document.getElementById('rf-adv-search');
    filterAdvanced(search ? search.value : '');
  }

  // `filterAdvanced` now hides a field when the search misses OR the selected
  // provider does not use that option.
  function filterAdvanced(term) {
    var list = document.getElementById('rf-adv-list');
    if (!list) return;
    var t = (term || '').trim().toLowerCase();
    var hidden = window.__rfHidden || {};
    var shown = 0;
    var total = 0;
    list.querySelectorAll('.rf-field').forEach(function (field) {
      var name = field.dataset.field;
      var bySearch = !t || field.textContent.toLowerCase().indexOf(t) !== -1;
      var byProvider = !hidden[name];
      var show = bySearch && byProvider;
      field.hidden = !show;
      total += 1;
      if (show) shown++;
    });
    var count = document.getElementById('rf-adv-count');
    if (count) count.textContent = t ? shown + ' of ' + total + ' shown' : '';
    // The summary says how many options apply to the chosen provider.
    var summary = document.getElementById('rf-adv-summary');
    if (summary) {
      var applicable = list.querySelectorAll('.rf-field:not([hidden])').length;
      summary.textContent = 'Advanced options (' + applicable + ')';
    }
  }

  // Render a backend's options: the essentials up front, the rest behind a
  // searchable "Advanced options" panel. `values` holds the stored options of
  // the remote being edited (secrets arrive as null).
  function renderBackendFields(spec, values) {
    values = values || {};
    var box = document.getElementById('rf-fields');
    if (!box) return;
    if (!spec) { box.innerHTML = ''; return; }
    window.__rfSpec = spec;

    // `local` has no config-level root option: its path lives in the legacy
    // `endpoint` column and is inlined into the target by the app.
    if (spec.name === 'local') {
      box.innerHTML =
        '<div><label class="block text-sm font-medium text-gray-700">Folder path</label>' +
        '<input type="text" name="endpoint" required maxlength="256" class="mt-1 w-full border border-gray-300 rounded px-3 py-2 text-sm" ' +
        'placeholder="/data/bucket" value="' + esc(values.endpoint || '') + '">' +
        '<p class="text-xs text-gray-400 mt-1">Absolute path inside the container (mount host folders in).</p></div>';
      return;
    }

    var simple = (spec.simple && spec.simple.length)
      ? spec.simple
      : spec.options.map(function (o) { return o.name; });
    var byName = {};
    spec.options.forEach(function (o) { byName[o.name] = o; });
    var rest = spec.options.filter(function (o) { return simple.indexOf(o.name) === -1; });

    var html = '<p class="text-xs text-gray-500">These are the settings this ' +
               'backend needs' + (rest.length ? '. Anything else is under <em>Advanced options</em>' : '') + '.</p>';
    simple.forEach(function (name) {
      if (byName[name]) html += fieldHtml(byName[name], values);
    });
    // The bucket is a separate, optional field: rclone's S3 config has no
    // bucket option (you type `remote:bucket`), and a bucket-scoped R2 token
    // cannot list buckets, so it has to be recorded with the remote.
    if (spec.bucket_based) {
      html += '<div data-field="bucket" class="rf-field">' +
        '<label class="block text-xs text-gray-600" for="rf-opt-bucket">bucket ' +
        '<span class="text-gray-400 font-mono">string</span></label>' +
        '<input type="text" id="rf-opt-bucket" name="bucket" maxlength="128" ' +
        'value="' + esc(values.bucket || '') + '" ' +
        'list="rf-dl-bucket" ' +
        'class="mt-1 w-full border border-gray-300 rounded px-3 py-2 text-sm" ' +
        'placeholder="' + (spec.scoped ? 'required for this key' : 'optional') + '">' +
        '<datalist id="rf-dl-bucket"></datalist>' +
        '<p class="text-xs text-gray-400 mt-1">Bucket to use as this remote’s root. ' +
        'Leave blank to use every bucket the key can see. A Cloudflare R2 API token ' +
        'is scoped to one bucket and cannot list buckets, so name it here.</p></div>';
    }
    if (spec.name === 's3') {
      html += '<p id="rf-preset-note" class="text-xs text-blue-700 bg-blue-50 rounded px-2 py-1" hidden></p>';
    }
    if (rest.length) {
      html += '<details class="border border-gray-300 rounded px-3 py-2 mt-3">' +
        '<summary class="text-sm font-medium text-gray-700 cursor-pointer" id="rf-adv-summary">Advanced options (' + rest.length + ')</summary>' +
        '<input type="text" id="rf-adv-search" autocomplete="off" placeholder="Search ' +
        rest.length + ' options… e.g. acl, encryption, storage_class" ' +
        'class="mt-2 w-full border border-gray-300 rounded px-3 py-2 text-sm">' +
        '<p class="text-xs text-gray-400 mt-1" id="rf-adv-count"></p>' +
        '<div class="space-y-3 mt-2" id="rf-adv-list">' +
        rest.map(function (o) { return fieldHtml(o, values); }).join('') +
        '</div></details>';
    }
    box.innerHTML = html;
    initCombos(box);
    // Credential fields: a reveal toggle and a live character count. The count
    // is what makes a mis-paste obvious — a 32-character key id showing "140"
    // is visible at a glance, where dots alone hide it.
    box.querySelectorAll('[data-secret="1"]').forEach(function (input) {
      var count = document.createElement('p');
      count.className = 'text-xs text-gray-400 mt-1 rf-count';
      input.parentNode.appendChild(count);
      var update = function () {
        var n = input.value.length;
        count.textContent = n
          ? n + ' characters' + (input.value !== input.value.trim() ? ' — leading/trailing spaces!' : '')
          : '';
        if (input.type === 'text') count.textContent += '  (shown)';
      };
      input.addEventListener('input', update);
      update();
      var btn = box.querySelector('.rf-reveal-btn[data-for="' + input.id + '"]');
      if (btn) {
        btn.addEventListener('click', function () {
          var showing = input.type === 'text';
          input.type = showing ? 'password' : 'text';
          btn.textContent = showing ? 'show' : 'hide';
          update();
        });
      }
    });
    // A field the user edited must never be overwritten by a preset default.
    box.addEventListener('input', function (e) {
      if (e.target && e.target.id) e.target.dataset.touched = '1';
    });
    if (spec.name === 's3') applyProviderPreset((values.provider && values.provider.value) || '');
    var search = document.getElementById('rf-adv-search');
    if (search) search.addEventListener('input', function () { filterAdvanced(this.value); });
    filterAdvanced('');
  }

  async function loadBackendFields(name, values) {
    try {
      const res = await fetch('/api/providers/' + encodeURIComponent(name));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const spec = await res.json();
      renderBackendFields(spec, values || {});
      const hint = document.getElementById('rf-type-hint');
      if (hint) hint.textContent = spec.description || '';
    } catch (err) {
      const box = document.getElementById('rf-fields');
      if (box) box.innerHTML = '<p class="text-sm text-red-600">Could not load options for ' + esc(name) + ' (' + esc(err) + ')</p>';
    }
  }

  // Published on `window` (a property, not a lexical binding) so re-opening the
  // modal simply overwrites it instead of throwing.
  window.loadBackendFields = loadBackendFields;

  // --------------------------------------------------------------- bootstrap
  // Called by the template every time the modal opens. `bootstrapData` is built
  // by Jinja (the remote's stored options, and whether we are editing or adding).
  function init(bootstrapData) {
    var data = bootstrapData || {};
    var values = data.values || {};

    window.__remoteTested = false;
    var saveBtn = document.getElementById('remote-save-btn');
    if (saveBtn) saveBtn.disabled = true;

    var box = document.getElementById('rf-fields');
    if (!box) return;              // not the remote modal — nothing to do
    initCombos(document);          // the backend picker is rendered server-side

    if (data.endpoint !== undefined && data.endpoint !== null) {
      values.endpoint = { value: data.endpoint };
    }
    if (data.remoteType) loadBackendFields(data.remoteType, values);
    else loadBackendFields('s3', {});

    var form = document.getElementById('remote-form');
    if (form) form.addEventListener('input', function () {
      // Invalidate the previous result: the settings have changed, so what is
      // on screen no longer describes them. markRemoteTested only toggles the
      // Save button, so the stale message is cleared separately — and only
      // here, never from the test handlers that write a fresh message.
      if (typeof markRemoteTested === 'function') markRemoteTested(false);
      if (typeof clearRemoteResult === 'function') clearRemoteResult();
      var hint = document.getElementById('remote-save-hint');
      if (hint) {
        hint.textContent = 'You changed the settings — test again before saving.';
        hint.className = 'text-xs text-gray-500 w-full sm:w-auto sm:flex-1';
      }
    });
  }

  // Published on `window` (properties, not lexical bindings) so re-running the
  // bootstrap simply overwrites them instead of throwing.
  window.RemoteForm = {
    init: init,
    loadBackendFields: loadBackendFields,
    applyProviderPreset: applyProviderPreset,
  };
})();
