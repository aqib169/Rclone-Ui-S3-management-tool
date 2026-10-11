#!/usr/bin/env python3
"""The Add Remote form must be simple by default and searchable.

Two layers:

* **structural (always runs)** — renders the modal the way the app serves it and
  asserts the shape: the four S3 essentials (plus the provider) up front, every
  other option behind a searchable "Advanced options" panel, endpoint/region as
  free-text boxes, and the provider as a searchable combo rather than a
  `<select>` nobody can scan.
* **behavioural (needs jsdom)** — runs `tests/ui/remote_form_dom.js` against the
  real modal in a real DOM to verify the searchable combos filter/rank and the
  provider presets fill region/endpoint/notes. Skipped when jsdom is missing;
  point `JSDOM_PATH` at a node_modules directory to enable it.

Run:
    .venv/bin/python -m pytest tests/test_remote_form_ui.py -v
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "app" / "templates"

# What an S3-compatible remote must show without opening anything.
S3_ESSENTIALS = ["provider", "endpoint", "region", "access_key_id", "secret_access_key"]


@pytest.fixture(scope="module")
def catalogue():
    os.environ.setdefault("DATA_DIR", "/tmp/orch-form-ui-test")
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import asyncio

    from app.providers import get_provider, load_catalogue, reset_cache

    reset_cache()
    asyncio.run(load_catalogue(force_refresh=True))
    spec = get_provider("s3")
    if spec is None:
        pytest.skip("rclone returned no s3 backend")
    return spec


@pytest.fixture(scope="module")
def s3_detail(catalogue):
    from app.presets import presets_payload

    detail = catalogue.detail()
    detail.update(presets_payload(catalogue))
    return detail


@pytest.fixture(scope="module")
def rendered_modal(s3_detail):
    """The Add Remote modal exactly as the endpoint serves it."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    env.globals["asset_version"] = "test"
    env.globals["app_version"] = "2.0.0"
    providers = [
        {"name": n, "description": d, "tier": "Tier 1", "hidden": False, "wrapper": w}
        for n, d, w in [
            ("s3", "Amazon S3 compatible", False),
            ("b2", "Backblaze B2", False),
            ("sftp", "SSH SFTP", False),
            ("local", "Local disk", False),
            ("crypt", "Encrypt", True),
        ]
    ]
    template = env.from_string((TEMPLATES / "modals" / "remote_form.html").read_text())
    return template.render(
        remote=None,
        providers=providers,
        provider=None,
        options={},
        cur_opts={},
        current_type="s3",
        remotes=[],
        remotes_error="",
        source_buckets=[],
        dest_buckets=[],
        cron={"min": "0", "hour": "2", "day": "*", "month": "*", "dow": "*"},
        cron_raw="",
        op=None,
        flag_groups=[],
    )


def _option(detail, name):
    return next(o for o in detail["options"] if o["name"] == name)


# --------------------------------------------------------------- structural
def test_provider_payload_lists_the_essentials_and_the_rest(s3_detail):
    """The API must tell the client which options are the essentials."""
    assert s3_detail["simple"] == S3_ESSENTIALS
    assert len(s3_detail["options"]) > len(S3_ESSENTIALS)
    presets = s3_detail["presets"]
    for wanted in ("AWS", "Cloudflare", "Minio", "Other"):
        assert wanted in presets, f"no preset for {wanted}"
    # Every preset must be a value rclone actually knows, otherwise rclone only
    # warns ("s3 provider \"…\" not known") and guesses.
    from app.presets import S3_PROVIDER_ALIASES

    enum = {e["value"] for e in _option(s3_detail, "provider")["examples"]}
    for name in presets:
        if name in S3_PROVIDER_ALIASES:
            continue  # hint-only: rclone has no such provider value
        assert name in enum, f"preset {name} is not an rclone provider value"


def test_presets_cover_the_real_world_cases():
    sys.path.insert(0, str(REPO))
    from app.presets import preset_for

    # Cloudflare R2: region auto + the R2 endpoint shape, endpoint required.
    r2 = preset_for("Cloudflare")
    assert r2["region"] == "auto"
    assert "r2.cloudflarestorage.com" in r2["endpoint_placeholder"]
    assert r2["endpoint_required"] is True
    # AWS: no endpoint needed, region preset.
    aws = preset_for("AWS")
    assert aws["endpoint_required"] is False and aws["endpoint"] == ""
    assert "leave blank" in aws["endpoint_placeholder"].lower()
    # Garage is only a hint (rclone has no Garage provider) and says so.
    garage = preset_for("Garage")
    assert garage["region"] == "garage" and "no 'Garage' provider" in garage["note"]
    # Unknown provider -> the generic S3 catch-all, never a crash.
    assert preset_for("SomethingBrandNew")["endpoint_required"] is True
    assert preset_for("")["note"]


def test_simple_option_sets(catalogue):
    """`simple_options` is the contract the renderer follows."""
    sys.path.insert(0, str(REPO))
    from app.presets import simple_options
    from app.providers import get_provider

    assert simple_options(get_provider("s3")) == S3_ESSENTIALS
    # A non-S3 backend shows what rclone marks required, plus a few companions.
    b2 = simple_options(get_provider("b2"))
    assert b2[:2] == ["account", "key"]
    assert len(b2) <= 6
    assert simple_options(get_provider("webdav"))[0] == "url"
    assert simple_options(get_provider("sftp"))[0] == "host"
    assert simple_options(None) == []


def test_modal_renders_a_searchable_backend_picker(rendered_modal):
    assert 'data-combo="type"' in rendered_modal
    assert '<select name="type"' not in rendered_modal, "backend picker is a plain select again"
    assert rendered_modal.count('role="option"') >= 5
    assert "Type to search" in rendered_modal


def test_free_form_options_stay_text_boxes(rendered_modal):
    """endpoint must be typeable (IP:port or a URL); region is a dropdown.

    `region` was moved out of FREE_TEXT_OPTIONS on purpose: a wrong-case or
    misspelled region is a classic S3 mistake, and rclone supplies 128 real
    region names, so a dropdown loses nothing.

    These assertions read app/static/js/remote-form.js, where the renderer now
    lives — it used to be inline in this template.
    """
    source = (REPO / "app" / "static" / "js" / "remote-form.js").read_text()
    assert "FREE_TEXT_OPTIONS" in source
    assert "'endpoint'" in source
    free_text = re.search(r"FREE_TEXT_OPTIONS = \[(.*?)\];", source, re.S)
    assert free_text, "FREE_TEXT_OPTIONS list not found"
    assert "'region'" not in free_text.group(1), "region must not be free text"
    # ...and it is rendered as a combo explicitly.
    assert "option.name === 'region'" in source


def test_advanced_panel_is_searchable_and_collapsed(rendered_modal):
    source = (REPO / "app" / "static" / "js" / "remote-form.js").read_text()
    assert "Advanced options (" in source
    assert "'id=\"rf-adv-search\"'" in source or 'id="rf-adv-search"' in source
    assert "filterAdvanced" in source
    # The panel is a collapsed <details> whose summary names it; the markup is
    # assembled in JS, so check the pieces rather than one contiguous string.
    assert "<details" in source and "Advanced options (" in source
    panel = re.search(r"<details[^>]*>\s*'", source)
    assert panel, "advanced options panel is not a collapsed <details>"


def test_modal_bootstrap_defers_to_the_external_module(rendered_modal):
    """The template must load the module and delegate, not re-inline the logic.

    This is the regression guard for the Day-3 extraction: if the 540-line inline
    block ever creeps back, the redeclaration hazard returns with it. The module
    is referenced by src (fetched once per page) while the bootstrap stays a
    thin IIFE, which is what makes re-opening the modal safe.
    """
    source = (TEMPLATES / "modals" / "remote_form.html").read_text()
    assert '/static/js/remote-form.js' in source, "modal no longer loads the module"
    assert "window.RemoteForm.init(" in source, "bootstrap does not call the module"

    # The heavy lifting must NOT be back in the template.
    assert "FREE_TEXT_OPTIONS" not in source, "renderer logic re-inlined into the template"
    assert "function renderBackendFields" not in source, "renderer re-inlined"

    # And the module file itself must actually exist and parse as an IIFE.
    module = REPO / "app" / "static" / "js" / "remote-form.js"
    assert module.exists(), "app/static/js/remote-form.js is missing"
    js = module.read_text()
    assert js.lstrip().startswith("/*"), "module should open with a comment"
    assert "window.RemoteForm" in js, "module does not publish window.RemoteForm"


# --------------------------------------------------------------- behavioural
def _jsdom_path():
    """Where jsdom lives, if it is available at all."""
    candidate = os.environ.get("JSDOM_PATH")
    if candidate and Path(candidate).exists():
        return candidate
    local = REPO / "node_modules"
    return str(local) if (local / "jsdom").exists() else None


@pytest.mark.skipif(_jsdom_path() is None, reason="jsdom not installed (set JSDOM_PATH)")
def test_modal_behaviour_in_a_real_dom(rendered_modal, s3_detail, tmp_path):
    """Drive the rendered modal in jsdom: searching and provider presets."""
    script = Path(__file__).resolve().parent / "ui" / "remote_form_dom.js"
    if not script.exists():
        pytest.skip("DOM script missing")
    page = tmp_path / "modal.html"
    page.write_text(rendered_modal)
    detail = tmp_path / "provider.json"
    detail.write_text(json.dumps(s3_detail))

    env = dict(os.environ)
    jsdom = _jsdom_path()
    env["NODE_PATH"] = jsdom + os.pathsep + env.get("NODE_PATH", "")
    result = subprocess.run(
        ["node", str(script), str(page), str(detail)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        "DOM checks failed:\n" + result.stdout[-4000:] + result.stderr[-2000:]
    )
    assert "all UI checks passed" in result.stdout


@pytest.mark.skipif(_jsdom_path() is None, reason="jsdom not installed (set JSDOM_PATH)")
def test_test_first_button_actually_works_in_a_real_dom(
    rendered_modal, s3_detail, tmp_path
):
    """Fill the form, click the real Test First button, check what happens.

    This covers base.html's half of the flow, which the other DOM test cannot
    reach: the modal fragment carries no testRemoteInline/remoteFormData, and
    the previous harness stubbed markRemoteTested out entirely. Both bugs that
    left a user stuck with a dead button lived in exactly that gap.
    """
    import re as _re

    script = Path(__file__).resolve().parent / "ui" / "remote_form_submit.js"
    if not script.exists():
        pytest.skip("submit script missing")
    # base.html's real inline script, extracted verbatim.
    base_src = (TEMPLATES / "base.html").read_text()
    blocks = _re.findall(
        r"<script(?![^>]*src=)[^>]*>(.*?)</script>", base_src, _re.S
    )
    assert len(blocks) >= 2, "base.html inline script blocks not found"
    base_js = tmp_path / "base_block.js"
    base_js.write_text(blocks[1])

    page = tmp_path / "modal.html"
    page.write_text(rendered_modal)
    detail = tmp_path / "provider.json"
    detail.write_text(json.dumps(s3_detail))

    env = dict(os.environ)
    jsdom = _jsdom_path()
    env["NODE_PATH"] = jsdom + os.pathsep + env.get("NODE_PATH", "")
    result = subprocess.run(
        ["node", str(script), str(page), str(detail), str(base_js)],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (
        "submit checks failed:\n" + result.stdout[-4000:] + result.stderr[-2000:]
    )
    assert "all submit checks passed" in result.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

def test_credentials_are_not_duplicated():
    """The form must show one key input per credential, not the pair twice."""
    source = (TEMPLATES / "modals" / "remote_form.html").read_text()
    # No visible legacy Access Key / Secret Key blocks (hidden carriers are fine).
    assert "S3-compatible backends only" not in source
    assert 'name="access_key" required' not in source
    # They are still submitted, as hidden carriers, so the API contract holds.
    assert '<input type="hidden" name="access_key"' in source
    assert '<input type="hidden" name="secret_key"' in source


def test_each_provider_declares_the_options_it_hides():
    sys.path.insert(0, str(REPO))
    from app.presets import preset_for, provider_options, visible_options
    from app.providers import get_provider

    s3 = get_provider("s3")
    names = {o.name for o in s3.options}
    # R2 ignores object ACLs and storage classes, so they must be hidden.
    cloudflare = provider_options("s3", "Cloudflare")
    assert {"acl", "storage_class", "server_side_encryption"} <= set(cloudflare)
    assert all(name in names for name in cloudflare), "hid a non-existent option"
    # AWS really does use ACLs and storage classes.
    assert "acl" not in provider_options("s3", "AWS")
    # The hide lists never touch the four essentials.
    essentials = set(S3_ESSENTIALS)
    for provider in ("AWS", "Cloudflare", "Minio", "Other", "Garage"):
        assert not essentials & set(provider_options("s3", provider))
    # visible_options shrinks the catalogue to what applies.
    assert "acl" not in visible_options(s3, "Cloudflare")
    assert "acl" in visible_options(s3, "AWS")
    # Non-S3 backends are unaffected.
    assert provider_options("b2", "anything") == []
    assert preset_for("Cloudflare")["hide"] == cloudflare


# ------------------------------------------------------------------ UI hygiene
def test_dashboard_getting_started_card():
    """A new user must be told what the app does and what to do first."""
    src = (TEMPLATES / "dashboard.html").read_text()
    assert "Getting started" in src
    # Plain-language explanation, not just jargon.
    assert "somewhere your files" in src.lower() or "copies files" in src.lower()
    # The three steps, in order, each linking somewhere.
    for target in ("remotes", "jobs", "history"):
        assert f'"/{{{{ target }}}}"' in src or f"/{target}" in src, target
    # The card is only for a fresh install; once a job exists the normal
    # actions return.
    assert "job_count" in src and "remote_count" in src


def test_dashboard_uses_only_defined_utility_classes():
    """A missing class silently renders unstyled — fail loudly instead.

    bg-ok-fg and border-app-border were invented and do not exist; the real
    tokens are bg-ok and border-line.
    """
    import re as _re

    css = (REPO / "app" / "static" / "vendor" / "tailwind.css").read_text()
    # The compiled sheet is purged, so a class no template used before is simply
    # absent — using one renders unstyled with no error at all. Class names are
    # escaped there (".md\:grid-cols-4"), so unescape before comparing.
    defined = {
        _re.sub(r"\\(.)", r"\1", name)
        for name in _re.findall(r"\.((?:[a-zA-Z0-9_-]|\\.)+)", css)
    }
    for name in (
        "base.html",
        "dashboard.html",
        "transfers.html",
        "modals/remote_form.html",
    ):
        src = (TEMPLATES / name).read_text()
        # A template may define its own component classes in a <style> block.
        # Compound selectors count too: ".rf-combo-list li.is-selected .cmb-value"
        # defines is-selected, not just rf-combo-list.
        styles = _re.findall(r"<style[^>]*>(.*?)</style>", src, _re.S)
        local = set(_re.findall(r"\.([a-zA-Z0-9_-]+)", "\n".join(styles)))
        for cls in _re.findall(r'class="([^"{}]+)"', src):
            for token in cls.split():
                # The full token is compared, variant prefix included: "sm:flex-none"
                # is emitted as ".sm\:flex-none" inside a media query, while the bare
                # "flex-none" may not exist at all. Opacity variants are emitted
                # whole ("bg-black/50"), so that form is tried first.
                if token in defined or token in local:
                    continue
                if _re.sub(r"/\d+$", "", token) in defined:
                    continue
                assert False, (
                    f"{name}: '{token}' is not in the compiled stylesheet and is "
                    f"not defined locally — it would render unstyled"
                )


def test_path_browser_enter_key_does_not_throw():
    """Regression: this.form was null (no <form>), so Enter threw."""
    src = (TEMPLATES / "transfers.html").read_text()
    assert "this.form.browser_remote" not in src, "the broken handler is still there"
    assert "browsePath()" in src
    # browsePath must read by id, not via a form.
    base = (TEMPLATES / "base.html").read_text()
    fn = base[base.index("function browsePath()"):][:1200]
    assert "getElementById('browser-remote')" in fn, fn[:200]
    assert "getElementById('browser-path')" in fn, fn[:200]
    assert "this.form" not in fn, "must not rely on a form that does not exist"
    # And the select must actually have the id it reads.
    assert 'id="browser-remote"' in src
