/* Inlines the app's real /static/js modules into a modal fragment so jsdom can
   execute them.
   Why this exists: the Add Remote modal used to carry its ~540 lines of JS inline.
   It now lives in app/static/js/remote-form.js and the template just references
   it with <script src="…">. jsdom does NOT fetch external scripts unless it is
   built with `resources: 'usable'` *and* given a resolvable base URL — and with
   the default options it silently skips them, so the form would come up empty
   and the harness would fail for the wrong reason.

   Splicing the real file's contents inline keeps these tests honest: they still
   exercise the exact source the browser downloads, so a syntax error or a broken
   export here fails the suite instead of passing vacuously.
*/
const fs = require('fs');
const path = require('path');

const STATIC_JS = path.resolve(__dirname, '..', '..', 'app', 'static', 'js');

/** Replace every <script src="/static/js/NAME"> with its inline contents. */
function inlineAppScripts(html) {
  return html.replace(
    /<script\s+src="\/static\/js\/([A-Za-z0-9._-]+)\.js(\?[^"]*)?"><\/script>/g,
    (match, name) => {
      const file = path.join(STATIC_JS, name + '.js');
      const src = fs.readFileSync(file, 'utf8');
      // `</script>` inside a string literal would close the tag early.
      return '<script>\n' + src.replace(/<\/script>/gi, '<\\/script>') + '\n</script>';
    }
  );
}

module.exports = { inlineAppScripts };