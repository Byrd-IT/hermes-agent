"""Settings › Subagents routes: ``delegation.backend`` and ``delegation.backends.*`` are read, validated and
written in the TARGET profile (``?profile=``). Real plugin directory registering real delegation providers, the
real loader and real config files under a temp home.
"""

from __future__ import annotations

import hermes_yaml as yaml
import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient

ROUTES_PLUGIN = '''
from agent.delegation_provider import DelegationProvider


class _Routes(DelegationProvider):
    def __init__(self, name, route_keys=()):
        self._name = name
        self._route_keys = route_keys

    @property
    def name(self):
        return self._name

    @property
    def route_keys(self):
        return self._route_keys

    def route_task(self, task, config):
        return dict(config)


def register(ctx):
    ctx.register_delegation_provider(_Routes("model_map", ("daybreak", "", 3, "daybreak")))
    ctx.register_delegation_provider(_Routes("profile_routes"))
'''

SECRET = "sk-route-secret-0123456789abcdef"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    from hermes_cli import plugins as plugins_mod

    root = tmp_path / "hermes_home"
    work = root / "profiles" / "work"
    plugin_dir = work / "plugins" / "routes"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump({"name": "routes", "version": "1.0"}), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(ROUTES_PLUGIN, encoding="utf-8")
    (work / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["routes"]},
        "delegation": {"backend": "model_map", "backends": {"model_map": {"options": [
            {"name": "fast", "description": "Quick", "provider": "openrouter", "model": "m-fast", "api_key": SECRET,
             "request_overrides": {"extra_body": {"seed": 7}}},
            {"name": "deep", "description": "Careful", "provider": "anthropic", "model": "m-deep"},
        ], "default": "deep"}}},
    }), encoding="utf-8")
    # The launch profile saved a plugin backend it does not have: configured, but nothing can run it.
    (root / "config.yaml").write_text(yaml.safe_dump({"delegation": {"backend": "model_map"}}), encoding="utf-8")
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    from agent import secret_scope
    from tui_gateway import launch_profile_policy as lpp
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setattr(lpp, "_snapshot", None)
    plugins_mod._reset_plugin_managers_for_tests()
    yield root, work
    plugins_mod._reset_plugin_managers_for_tests()


@pytest.fixture
def client(homes):
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _delegation(home) -> dict:
    return (yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}).get("delegation") or {}


def test_routing_reads_the_target_profile_and_never_returns_a_route_key(client):
    work = client.get("/api/delegation/routing?profile=work").json()
    assert {b["name"] for b in work["backends"]} == {"default", "model_map", "profile_routes"}
    assert (work["configured"], work["blocked_reason"]) == ("model_map", None)
    assert [o["name"] for o in work["model_map"]["options"]] == ["fast", "deep"]
    assert work["model_map"]["default"] == "deep"
    assert SECRET not in str(work) and work["model_map"]["options"][0]["api_key"]
    assert {"default", "work"} <= set(work["profiles"])

    # Core fails closed on a saved backend no plugin provides: the page must say why, not show Default.
    launch = client.get("/api/delegation/routing").json()
    assert [b["name"] for b in launch["backends"]] == ["default"]
    assert launch["configured"] == "model_map" and "not installed" in launch["blocked_reason"]


def test_a_backend_can_only_be_chosen_where_its_plugin_is_registered(client, homes):
    root, work = homes
    resp = client.put("/api/delegation/backend?profile=work", json={"backend": "profile_routes"})
    assert resp.status_code == 200, resp.text
    assert _delegation(work)["backend"] == "profile_routes"

    before = (root / "config.yaml").read_text(encoding="utf-8")
    resp = client.put("/api/delegation/backend", json={"backend": "profile_routes"})
    assert resp.status_code == 400
    assert "not installed" in resp.json()["detail"]
    assert (root / "config.yaml").read_text(encoding="utf-8") == before


@pytest.mark.parametrize(("path", "body"), [
    ("/api/delegation/backend", {"backend": "default"}),
    ("/api/delegation/backends/model_map", {"options": []}),
    ("/api/delegation/backends/profile_routes", {"profiles": []}),
])
def test_a_managed_install_refuses_every_routing_write_with_the_reason(client, homes, monkeypatch, path, body):
    """config.yaml of a package-manager-managed install is not ours: each write is a 409 naming the manager,
    never a silent ``ok`` over an untouched file."""
    _root, work = homes
    before = (work / "config.yaml").read_text(encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED", "nixos")
    resp = client.put(f"{path}?profile=work", json=body)
    assert resp.status_code == 409, resp.text
    assert "managed by nixos" in resp.json()["detail"] and "\n" not in resp.json()["detail"]
    assert (work / "config.yaml").read_text(encoding="utf-8") == before


@pytest.mark.parametrize(("options", "problem"), [
    ([{"name": "a", "description": "x", "provider": "p", "model": "m"},
      {"name": "a", "description": "y", "provider": "p", "model": "m"}], "unique"),
    ([{"name": "a", "provider": "p", "model": "m"}], "description"),
    ([{"name": "a", "description": "x", "model": "m"}], "provider"),
    ([{"name": "a", "description": "x", "provider": "p", "model": "m", "service_tier": "warp"}], "service tier"),
    ([{"name": "a", "description": "x", "provider": "p", "model": "m", "reasoning_effort": "huge"}], "reasoning"),
])
def test_invalid_model_routes_are_refused_before_anything_is_written(client, homes, options, problem):
    _root, work = homes
    before = (work / "config.yaml").read_text(encoding="utf-8")
    resp = client.put("/api/delegation/backends/model_map?profile=work", json={"options": options})
    assert resp.status_code == 400
    assert problem in resp.json()["detail"]
    assert (work / "config.yaml").read_text(encoding="utf-8") == before


def test_saving_model_routes_replaces_the_block_and_keeps_the_stored_key(client, homes):
    root, work = homes
    shown = client.get("/api/delegation/routing?profile=work").json()["model_map"]["options"][0]
    # Rename and edit "fast" as the renderer would (preview key and untouched fields sent back, the old name in
    # original_name), delete "deep", add "local".
    body = {"options": [{**shown, "name": "speedy", "original_name": "fast", "model": "m-faster",
                         "reasoning_effort": "low"},
                        {"name": "local", "description": "Private", "base_url": "http://127.0.0.1:8080/v1",
                         "model": "qwen", "service_tier": "fast"}],
            "default": "local"}
    resp = client.put("/api/delegation/backends/model_map?profile=work", json=body)
    assert resp.status_code == 200, resp.text
    assert SECRET not in resp.text

    block = _delegation(work)["backends"]["model_map"]
    assert block["default"] == "local"
    assert [o["name"] for o in block["options"]] == ["speedy", "local"]
    assert block["options"][0] == {"name": "speedy", "description": "Quick", "provider": "openrouter",
                                   "model": "m-faster", "reasoning_effort": "low", "api_key": SECRET,
                                   "request_overrides": {"extra_body": {"seed": 7}}}
    assert "backends" not in _delegation(root)


def test_routing_lists_each_backends_declared_route_keys(client):
    backends = {b["name"]: b["route_keys"] for b in client.get("/api/delegation/routing?profile=work").json()["backends"]}
    # Blank, non-string and repeated declarations are dropped; a backend that declares nothing lists nothing.
    assert backends == {"default": [], "model_map": ["daybreak"], "profile_routes": []}


def test_a_declared_route_key_round_trips_and_removing_it_turns_it_off(client, homes):
    _root, work = homes
    route = {"name": "daybreak", "description": "Sol, reduced guardrails", "provider": "openai-codex",
             "model": "gpt-6-sol", "service_tier": "fast"}
    resp = client.put("/api/delegation/backends/model_map?profile=work", json={"options": [{**route, "daybreak": True}]})
    assert resp.status_code == 200, resp.text
    assert _delegation(work)["backends"]["model_map"]["options"][0]["daybreak"] is True
    # Re-saving what the renderer was shown keeps it (an unrelated edit must never silently drop it).
    shown = client.get("/api/delegation/routing?profile=work").json()["model_map"]["options"]
    resp = client.put("/api/delegation/backends/model_map?profile=work",
                      json={"options": [{**shown[0], "original_name": "daybreak", "reasoning_effort": "high"}]})
    assert resp.status_code == 200, resp.text
    assert _delegation(work)["backends"]["model_map"]["options"][0]["daybreak"] is True
    # The editor turns it off by leaving the key out.
    assert client.put("/api/delegation/backends/model_map?profile=work", json={"options": [route]}).status_code == 200
    assert "daybreak" not in _delegation(work)["backends"]["model_map"]["options"][0]


def test_an_undeclared_key_survives_only_an_untouched_round_trip(client, homes):
    _root, work = homes
    stored = {"name": "deep", "description": "Careful", "provider": "anthropic", "model": "m-deep", "legacy": {"a": 1}}
    assert client.put("/api/delegation/backends/model_map?profile=work", json={"options": [stored]}).status_code == 200
    # A key nobody stored (no backend declares it) is dropped, however the renderer sends it.
    assert "legacy" not in _delegation(work)["backends"]["model_map"]["options"][0]

    block = _delegation(work)
    block["backends"]["model_map"]["options"][0]["legacy"] = {"a": 1}  # hand-written in config.yaml
    (work / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["routes"]}, "delegation": block}),
                                      encoding="utf-8")
    shown = client.get("/api/delegation/routing?profile=work").json()["model_map"]["options"][0]
    resp = client.put("/api/delegation/backends/model_map?profile=work",
                      json={"options": [{**shown, "original_name": "deep", "model": "m-deeper"}]})
    assert resp.status_code == 200, resp.text
    assert _delegation(work)["backends"]["model_map"]["options"][0]["legacy"] == {"a": 1}
    changed = {**shown, "legacy": {"a": 2}}
    assert client.put("/api/delegation/backends/model_map?profile=work", json={"options": [changed]}).status_code == 200
    assert "legacy" not in _delegation(work)["backends"]["model_map"]["options"][0]


def test_a_null_extra_key_is_a_value_and_leaving_it_out_removes_it(client, homes):
    _root, work = homes
    route = {"name": "deep", "description": "Careful", "provider": "anthropic", "model": "m-deep"}

    def save(**extra):
        resp = client.put("/api/delegation/backends/model_map?profile=work", json={"options": [{**route, **extra}]})
        assert resp.status_code == 200, resp.text
        return _delegation(work)["backends"]["model_map"]["options"][0]

    # A declared key sent as null is kept as sent; leaving it out removes it.
    assert save(daybreak=None) == {**route, "daybreak": None}
    assert "daybreak" not in save()

    # A stored null in an undeclared key survives a save that sends it back, and goes when the save leaves it out.
    block = _delegation(work)
    block["backends"]["model_map"]["options"][0]["legacy"] = None  # hand-written in config.yaml
    (work / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["routes"]}, "delegation": block}),
                                      encoding="utf-8")
    assert save(legacy=None) == {**route, "legacy": None}
    assert "legacy" not in save()


def test_profile_routes_only_name_existing_profiles(client, homes):
    _root, work = homes
    before = (work / "config.yaml").read_text(encoding="utf-8")
    resp = client.put("/api/delegation/backends/profile_routes?profile=work",
                      json={"profiles": [{"name": "ghost"}]})
    assert resp.status_code == 400 and "does not exist" in resp.json()["detail"]
    assert (work / "config.yaml").read_text(encoding="utf-8") == before

    resp = client.put("/api/delegation/backends/profile_routes?profile=work",
                      json={"profiles": [{"name": "default", "description": "  The main one  "}], "default": "default"})
    assert resp.status_code == 200, resp.text
    assert _delegation(work)["backends"]["profile_routes"] == {
        "profiles": [{"name": "default", "description": "The main one"}], "default": "default"}


# --- nested route secrets (fallback_providers[].api_key, request_overrides headers) ---

FB_KEY, FB2_KEY, HDR = "sk-synth-fallback-0001", "sk-synth-fallback-0002", "Bearer synth-header-token-0003"
XKEY = "synth-xapikey-0004"
RAW_NESTED = (FB_KEY, FB2_KEY, HDR, XKEY, "sk-synth-deep-0005", "synth-header-token")
DOTS = "." * 3
STAR = "*" * 3
SHAPES = ["\u00abredacted:" + "abcd" + DOTS + "wxyz" + "\u00bb", "\u00abredacted-secret\u00bb", STAR, "abcd" + DOTS + "wxyz"]


def _seed_nested(work) -> None:
    cfg = yaml.safe_load((work / "config.yaml").read_text(encoding="utf-8"))
    cfg["delegation"]["backends"]["model_map"]["options"][0].update({
        "fallback_providers": [
            {"provider": "openai", "model": "gpt-a", "api_key": FB_KEY},
            {"provider": "groq", "model": "llama-b", "api_key": FB2_KEY,
             "fallbacks": [{"provider": "x", "model": "y", "api_key": "sk-synth-deep-0005"}]},
        ],
        "request_overrides": {"extra_headers": {"Authorization": HDR, "X-Api-Key": XKEY, "X-Trace": "keep-me"},
                              "extra_body": {"seed": 7, "max_tokens": 100}}})
    (work / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _fast(work) -> dict:
    return _delegation(work)["backends"]["model_map"]["options"][0]


def _get_options(client) -> list:
    return client.get("/api/delegation/routing?profile=work").json()["model_map"]["options"]


def _put(client, options, default="deep"):
    return client.put("/api/delegation/backends/model_map?profile=work", json={"options": options, "default": default})


def _is_preview(value) -> bool:
    from hermes_cli.web_routers._common import is_redacted_credential_preview
    return is_redacted_credential_preview(value)


def test_routing_masks_nested_route_secrets_but_keeps_plain_values(client, homes):
    _, work = homes
    _seed_nested(work)
    resp = client.get("/api/delegation/routing?profile=work")
    for raw in RAW_NESTED:
        assert raw not in resp.text
    fast = resp.json()["model_map"]["options"][0]
    assert _is_preview(fast["fallback_providers"][0]["api_key"])
    assert _is_preview(fast["fallback_providers"][1]["fallbacks"][0]["api_key"])
    headers = fast["request_overrides"]["extra_headers"]
    assert _is_preview(headers["Authorization"]) and _is_preview(headers["X-Api-Key"])
    assert headers["X-Trace"] == "keep-me"
    assert fast["request_overrides"]["extra_body"] == {"seed": 7, "max_tokens": 100}
    assert fast["fallback_providers"][0]["provider"] == "openai"


def test_saving_the_untouched_form_keeps_every_nested_secret(client, homes):
    _, work = homes
    _seed_nested(work)
    stored = _fast(work)
    resp = _put(client, _get_options(client))
    assert resp.status_code == 200, resp.text
    for raw in RAW_NESTED:
        assert raw not in resp.text
    assert _fast(work) == stored
    assert "redacted" not in (work / "config.yaml").read_text(encoding="utf-8")


def test_editing_one_nested_secret_leaves_its_siblings(client, homes):
    _, work = homes
    _seed_nested(work)
    options = _get_options(client)
    options[0]["fallback_providers"][0]["api_key"] = "sk-synth-new-0006"
    options[0]["request_overrides"]["extra_headers"]["X-Api-Key"] = "synth-new-xapikey-0007"
    assert _put(client, options).status_code == 200
    fast = _fast(work)
    assert fast["fallback_providers"][0]["api_key"] == "sk-synth-new-0006"
    assert fast["fallback_providers"][1]["api_key"] == FB2_KEY
    assert fast["fallback_providers"][1]["fallbacks"][0]["api_key"] == "sk-synth-deep-0005"
    assert fast["request_overrides"]["extra_headers"] == {
        "Authorization": HDR, "X-Api-Key": "synth-new-xapikey-0007", "X-Trace": "keep-me"}


def test_renaming_a_route_keeps_its_nested_secrets(client, homes):
    _, work = homes
    _seed_nested(work)
    options = _get_options(client)
    options[0].update({"name": "speedy", "original_name": "fast"})
    assert _put(client, options).status_code == 200
    fast = _fast(work)
    assert fast["name"] == "speedy"
    assert fast["fallback_providers"][0]["api_key"] == FB_KEY
    assert fast["request_overrides"]["extra_headers"]["Authorization"] == HDR


def test_reordering_or_editing_fallbacks_never_moves_a_key_to_another_entry(client, homes):
    _, work = homes
    _seed_nested(work)
    options = _get_options(client)
    fallbacks = options[0]["fallback_providers"]
    options[0]["fallback_providers"] = [fallbacks[1], {**fallbacks[0], "model": "gpt-other"}]
    assert _put(client, options).status_code == 200
    saved = _fast(work)["fallback_providers"]
    assert saved[0]["api_key"] == FB2_KEY and saved[0]["provider"] == "groq"
    # same provider, different model: no stored counterpart, so the preview is dropped, not kept
    assert saved[1]["model"] == "gpt-other" and "api_key" not in saved[1]


def test_editing_only_a_fallback_model_keeps_its_key_but_a_new_host_does_not(client, homes):
    _, work = homes
    _seed_nested(work)
    options = _get_options(client)
    fb = options[0]["fallback_providers"]
    fb[0] = {**fb[0], "model": "gpt-renamed"}
    fb[1] = {**fb[1], "base_url": "https://elsewhere.example/v1"}
    assert _put(client, options).status_code == 200
    saved = _fast(work)["fallback_providers"]
    assert saved[0]["model"] == "gpt-renamed" and saved[0]["api_key"] == FB_KEY
    assert saved[1]["base_url"] == "https://elsewhere.example/v1" and "api_key" not in saved[1]


def test_a_new_same_provider_fallback_above_an_existing_one_never_takes_its_key(client, homes):
    _, work = homes
    _seed_nested(work)
    options = _get_options(client)
    fb = options[0]["fallback_providers"]
    options[0]["fallback_providers"] = [{"provider": "openai", "model": "gpt-new"}, fb[0], fb[1]]
    assert _put(client, options).status_code == 200
    saved = _fast(work)["fallback_providers"]
    assert "api_key" not in saved[0]
    assert saved[1]["api_key"] == FB_KEY and saved[2]["api_key"] == FB2_KEY


def test_an_empty_base_url_is_the_same_endpoint_as_a_missing_one(client, homes):
    _, work = homes
    _seed_nested(work)
    options = _get_options(client)
    options[0]["fallback_providers"][0]["base_url"] = ""
    assert _put(client, options).status_code == 200
    assert _fast(work)["fallback_providers"][0]["api_key"] == FB_KEY


def test_a_container_emptied_by_a_dropped_preview_is_not_written(client, homes):
    _, work = homes
    mask = SHAPES[0]
    new = {"name": "fresh", "description": "New", "provider": "openai", "model": "m",
           "request_overrides": {"extra_headers": {"Authorization": mask}, "extra_body": {}}}
    assert _put(client, [new], default="fresh").status_code == 200
    overrides = _delegation(work)["backends"]["model_map"]["options"][0].get("request_overrides", {})
    assert "extra_headers" not in overrides


def test_header_style_secret_names_are_masked_but_env_var_names_are_not(client, homes):
    _, work = homes
    cfg = yaml.safe_load((work / "config.yaml").read_text(encoding="utf-8"))
    cfg["delegation"]["backends"]["model_map"]["options"][0]["request_overrides"] = {
        "extra_headers": {"X-Jwt": "syn-jwt-value-0001", "Bearer-Auth": "syn-bearer-value-0002",
                          "api_key_env": "MY_ENV_NAME"}}
    (work / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    resp = client.get("/api/delegation/routing?profile=work")
    assert "syn-jwt-value-0001" not in resp.text and "syn-bearer-value-0002" not in resp.text
    assert resp.json()["model_map"]["options"][0]["request_overrides"]["extra_headers"]["api_key_env"] == "MY_ENV_NAME"


def test_removing_a_header_removes_it(client, homes):
    _, work = homes
    _seed_nested(work)
    options = _get_options(client)
    del options[0]["request_overrides"]["extra_headers"]["Authorization"]
    assert _put(client, options).status_code == 200
    headers = _fast(work)["request_overrides"]["extra_headers"]
    assert "Authorization" not in headers and headers["X-Api-Key"] == XKEY


@pytest.mark.parametrize("mask", SHAPES)
def test_a_preview_with_no_stored_counterpart_is_never_persisted(client, homes, mask):
    _, work = homes
    new = {"name": "fresh", "description": "New", "provider": "openai", "model": "m",
           "fallback_providers": [{"provider": "openai", "model": "gpt-a", "api_key": mask}],
           "request_overrides": {"extra_headers": {"Authorization": mask, "X-Trace": "t"}}}
    assert _put(client, [new], default="fresh").status_code == 200
    saved = _delegation(work)["backends"]["model_map"]["options"][0]
    assert saved["fallback_providers"] == [{"provider": "openai", "model": "gpt-a"}]
    assert saved["request_overrides"] == {"extra_headers": {"X-Trace": "t"}}


def test_a_stale_preview_never_overwrites_a_rotated_secret(client, homes):
    _, work = homes
    _seed_nested(work)
    options = _get_options(client)
    cfg = yaml.safe_load((work / "config.yaml").read_text(encoding="utf-8"))
    cfg["delegation"]["backends"]["model_map"]["options"][0]["fallback_providers"][0]["api_key"] = "sk-synth-rotated-0008"
    (work / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert _put(client, options).status_code == 200
    assert _fast(work)["fallback_providers"][0]["api_key"] == "sk-synth-rotated-0008"


def test_a_refused_save_never_echoes_a_nested_secret(client, homes):
    _, work = homes
    _seed_nested(work)
    options = _get_options(client)
    options[0]["model"] = ""
    resp = _put(client, options)
    assert resp.status_code == 400
    for raw in RAW_NESTED:
        assert raw not in resp.text
