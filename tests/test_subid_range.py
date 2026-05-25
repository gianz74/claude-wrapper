"""Unit tests for the T20 base sub-id *range* predicate (DESIGN §3).

``_subid_range_present`` is **distinct** from ``_subid_covered`` (T4): the latter
asks "is *this* uid mappable" (the idmap prereq — count 1 ok); this asks "can
incus allocate a *container's worth* of ids" (the unprivileged-container prereq
— count >= 65536). A host can pass the first and fail the second (the
2026-05-25 ``barney`` bug: a lone ``root:<uid>:1`` entry, no base range).

The (b) storage-pool / default-profile probe is daemon I/O — verified by a
manual/throwaway host run per the T3/T4 convention, not here.
"""

from claude_wrapper.lifecycle import _subid_range_present


def _write(tmp_path, text):
    p = tmp_path / "subid"
    p.write_text(text)
    return str(p)


def test_only_single_idmap_entry_is_not_a_range(tmp_path):
    # The barney bug: idmap entry present, base range absent.
    assert _subid_range_present(_write(tmp_path, "root:1000:1\n")) is False


def test_base_range_present(tmp_path):
    assert _subid_range_present(_write(tmp_path, "root:1000000:1000000000\n")) is True


def test_both_lines_present(tmp_path):
    # Idmap entry + base range coexist (disjoint ranges) → range satisfied.
    text = "root:1000:1\nroot:1000000:1000000000\n"
    assert _subid_range_present(_write(tmp_path, text)) is True


def test_missing_file_is_false():
    assert _subid_range_present("/nonexistent/subid") is False


def test_count_boundary(tmp_path):
    # A container's idmap span is 65536; one short is not enough.
    assert _subid_range_present(_write(tmp_path, "root:1000000:65535\n")) is False
    assert _subid_range_present(_write(tmp_path, "root:1000000:65536\n")) is True


def test_non_root_owner_range_ignored(tmp_path):
    # A big range owned by a non-root user doesn't help incus's root allocation
    # (the stock `ubuntu:100000:65536` line is exactly this case).
    assert _subid_range_present(_write(tmp_path, "ubuntu:100000:65536\n")) is False


def test_zero_owner_alias_accepted(tmp_path):
    # `0` is accepted as the root owner (parity with _subid_covered).
    assert _subid_range_present(_write(tmp_path, "0:1000000:1000000000\n")) is True


def test_malformed_lines_skipped(tmp_path):
    text = "garbage\nroot:notanumber:alsobad\nroot:1000000:1000000000\n"
    assert _subid_range_present(_write(tmp_path, text)) is True
