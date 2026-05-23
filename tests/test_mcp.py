"""Unit tests for the pure MCP/IDE-bridge logic (T9).

Port extraction, workspace-folder normalisation, the daemon-free arg-rewrite
branches (inline JSON / missing file), and the lockfile patch (pure file I/O).
The device/proxy/sentinel paths are I/O against the incus daemon and are covered
by a throwaway integration run, like the rest of the lifecycle layer.
"""

import json

from claude_wrapper.mcp import (
    Bridge,
    extract_loopback_ports_from_text,
    normalize_workspace_folders,
)


# --- extract_loopback_ports_from_text ----------------------------------------


def test_ports_finds_127_and_localhost():
    js = json.dumps({"a": "http://127.0.0.1:8080/x", "b": "ws://localhost:9000"})
    assert extract_loopback_ports_from_text(js) == ["8080", "9000"]


def test_ports_nested_and_deduped_sorted():
    js = json.dumps({
        "servers": [{"url": "127.0.0.1:300"}, {"url": "localhost:300"}],
        "extra": {"deep": ["127.0.0.1:100"]},
    })
    assert extract_loopback_ports_from_text(js) == ["100", "300"]


def test_ports_ignores_non_loopback():
    js = json.dumps({"url": "https://example.com:443"})
    assert extract_loopback_ports_from_text(js) == []


def test_ports_bad_json_is_empty():
    assert extract_loopback_ports_from_text("not json {{{") == []


# --- normalize_workspace_folders ---------------------------------------------


def test_normalize_strips_trailing_slash():
    assert normalize_workspace_folders(["/home/u/proj/"]) == ["/home/u/proj"]


def test_normalize_keeps_root_and_non_strings():
    assert normalize_workspace_folders(["/", 5, "/a/"]) == ["/", 5, "/a"]


def test_normalize_passthrough_non_list():
    assert normalize_workspace_folders("nope") == "nope"
    assert normalize_workspace_folders(None) is None


# --- _rewrite_mcp_args (daemon-free branches) --------------------------------


def _bridge():
    return Bridge("inst", home="/home/u")


def test_rewrite_inline_json_passes_through_and_records():
    b = _bridge()
    sources, inline = [], []
    inline_json = '{"mcpServers": {"x": {"url": "127.0.0.1:5000"}}}'
    out = b._rewrite_mcp_args(["--mcp-config", inline_json], sources, inline)
    assert out == ["--mcp-config", inline_json]
    assert inline == [inline_json] and sources == []


def test_rewrite_missing_file_passes_through():
    b = _bridge()
    sources, inline = [], []
    out = b._rewrite_mcp_args(["--mcp-config", "/no/such/file.json"], sources, inline)
    assert out == ["--mcp-config", "/no/such/file.json"]
    assert sources == [] and inline == []


def test_rewrite_equals_form_inline():
    b = _bridge()
    sources, inline = [], []
    out = b._rewrite_mcp_args(["--mcp-config={\"a\":1}"], sources, inline)
    assert out == ["--mcp-config={\"a\":1}"]
    assert inline == ['{"a":1}']


def test_rewrite_leaves_other_args_untouched():
    b = _bridge()
    out = b._rewrite_mcp_args(["-p", "hi", "--model", "x"], [], [])
    assert out == ["-p", "hi", "--model", "x"]


# --- _patch_lockfile (pure file I/O) -----------------------------------------


def _write_lock(tmp_path, port, payload):
    ide = tmp_path / ".claude" / "ide"
    ide.mkdir(parents=True)
    p = ide / f"{port}.lock"
    p.write_text(json.dumps(payload))
    return p


def test_patch_lockfile_sets_pid_and_strips_slash(tmp_path):
    lock = _write_lock(tmp_path, "5005", {"pid": 99999, "workspaceFolders": ["/home/u/proj/"]})
    b = Bridge("inst", home=str(tmp_path))
    b._patch_lockfile("5005", 4242)
    data = json.loads(lock.read_text())
    assert data["pid"] == 4242
    assert data["workspaceFolders"] == ["/home/u/proj"]


def test_patch_lockfile_noop_when_already_correct(tmp_path):
    lock = _write_lock(tmp_path, "5005", {"pid": 4242, "workspaceFolders": ["/p"]})
    before = lock.read_text()
    Bridge("inst", home=str(tmp_path))._patch_lockfile("5005", 4242)
    assert lock.read_text() == before


def test_patch_lockfile_missing_file_is_silent(tmp_path):
    # No lock file written → must not raise.
    Bridge("inst", home=str(tmp_path))._patch_lockfile("5005", 4242)
