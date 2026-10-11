#!/usr/bin/env python3
"""Guard against a class of htmx bug: scripts in swapped partials.

htmx re-executes a `<script>` block every time the fragment is swapped into the
page, so *every* modal can be opened repeatedly. A top-level `const`/`let`/`class`
in such a script therefore throws
`SyntaxError: Identifier '…' has already been declared` on the second open, and
the whole script dies silently — the user just sees a half-built form.

This was real: the Add Remote modal lost all its generated backend fields on the
second open, and the New Job modal lost its cron builder.

Two checks:
  1. static  — no top-level lexical declarations in a swapped template,
  2. dynamic — when node is available, evaluate each rendered script twice and
     require it to survive (this is what the browser really does).

Run:
    .venv/bin/python -m pytest tests/test_htmx_scripts.py -v
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "app" / "templates"

# Fragments swapped into an already-loaded page (htmx partials + modals).
# Full pages are loaded by navigation, so their scripts run once.
SWAPPED = [
    "modals/remote_form.html",
    "modals/job_form.html",
    "components/run_table.html",
    "components/job_table.html",
    "components/remote_table.html",
    "components/audit_table.html",
    "components/archive_tree.html",
    "components/archive_dates.html",
    "components/restore_started.html",
    "components/run_status.html",
    "components/stats.html",
    "components/active_runs.html",
    "components/log.html",
    "components/pagination.html",
    "components/path_listing.html",
    "components/progress.html",
    "components/bucket_options.html",
    "components/remote_options.html",
    "components/transfer_form.html",
]

SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)
_DECL_RE = re.compile(r"(const|let|class)[ \t]")


def _top_level_declarations(js: str) -> list:
    """`const`/`let`/`class` declared at brace depth 0.

    A declaration inside a function (typically an IIFE wrapper) is harmless:
    it lives in a fresh scope on every evaluation, so only depth-0 bindings
    collide on the second run. Comments and string literals are skipped so the
    HTML these templates build cannot skew the depth.
    """
    found, depth, at_line_start, i, n = [], 0, True, 0, len(js)
    while i < n:
        ch = js[i]
        if ch == "/" and i + 1 < n and js[i + 1] == "/":
            end = js.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch == "/" and i + 1 < n and js[i + 1] == "*":
            end = js.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n:
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == quote:
                    i += 1
                    break
                i += 1
            at_line_start = False
            continue
        if at_line_start and depth == 0:
            match = _DECL_RE.match(js, i)
            if match and (i == 0 or not (js[i - 1].isalnum() or js[i - 1] == "$")):
                found.append(match.group(0).strip())
                i = match.end()
                at_line_start = False
                continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "\n":
            at_line_start = True
        elif ch in " \t\r":
            pass  # indentation does not end the statement
        else:
            at_line_start = False
        i += 1
    return found


def _scripts(path: Path):
    return SCRIPT_RE.findall(path.read_text())


@pytest.mark.parametrize("relative", SWAPPED)
def test_swapped_template_has_no_top_level_declarations(relative):
    """A `const`/`let` at the top level of a swapped script breaks re-opening."""
    path = TEMPLATES / relative
    assert path.exists(), f"missing template {relative}"
    for index, js in enumerate(_scripts(path)):
        offenders = _top_level_declarations(js)
        assert not offenders, (
            f"{relative} script block {index} declares {offenders} at the top "
            "level; htmx re-executes this script on every open, so the second "
            "open throws 'already been declared'. Wrap it in an IIFE and expose "
            "what inline handlers need via `window.x = …`."
        )


def _render_modal(relative: str, **context) -> str:
    """Render one modal template standalone (enough context for its fields)."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    env.globals["asset_version"] = "test"
    env.globals["app_version"] = "2.0.0"
    defaults = {
        "remote": None,
        "job": None,
        "providers": [],
        "provider": None,
        "options": {},
        "remotes": ["src", "dst"],
        "remotes_error": "",
        "source_buckets": [],
        "dest_buckets": [],
        "cron": {"min": "0", "hour": "2", "day": "*", "month": "*", "dow": "*"},
        "cron_raw": "",
        "op": None,
        "flag_groups": [],
    }
    defaults.update(context)
    return env.from_string((TEMPLATES / relative).read_text()).render(**defaults)


# Evaluates each <script> block twice in one global scope, with a DOM stub
# permissive enough that only redeclaration failures can surface.
_TWICE_JS = r"""
const vm = require('vm');
const html = require('fs').readFileSync(process.argv[2], 'utf8');
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
// A DOM stub permissive enough that only redeclaration failures can surface.
function makeEl() {
  const el = {
    value: '', checked: false, disabled: false, textContent: '',
    hidden: false, dataset: {}, options: [], selectedIndex: 0, children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, removeEventListener() {}, appendChild() {},
    setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    scrollIntoView() {}, focus() {}, click() {},
    querySelector: () => null, querySelectorAll: () => [],
    closest: () => null, getElementsByTagName: () => [],
  };
  return el;
}
const ctx = vm.createContext({
  console, fetch: () => Promise.resolve({ ok: false, json: () => ({}) }),
  window: {}, htmx: {}, markRemoteTested: () => {}, closeModal: () => {},
  document: {
    getElementById: () => makeEl(),
    querySelectorAll: () => [],
    querySelector: () => null,
  },
});
vm.runInContext('var window = globalThis.window;', ctx);
for (let pass = 1; pass <= 2; pass++) {
  blocks.forEach((js, i) => {
    try { vm.runInContext(js, ctx, { filename: 'pass' + pass + ':' + i }); }
    catch (e) {
      console.log('pass ' + pass + ' block ' + i + ': ' + e.name + ': ' + e.message);
      process.exit(1);
    }
  });
}
console.log('ok');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    "relative", ["modals/remote_form.html", "modals/job_form.html"]
)
def test_modal_script_survives_a_second_evaluation(relative, tmp_path):
    """Evaluate each modal's script twice, exactly as a second open does."""
    html = _render_modal(relative)
    blocks = SCRIPT_RE.findall(html)
    assert blocks, f"{relative} has no <script> block to test"

    runner = tmp_path / "twice.js"
    runner.write_text(_TWICE_JS)
    page = tmp_path / "modal.html"
    page.write_text(html)
    result = subprocess.run(
        ["node", str(runner), str(page)], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"{relative} dies when opened a second time:\n{result.stdout.strip()}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
