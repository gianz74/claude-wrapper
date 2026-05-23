"""Unit tests for the pure template-naming logic in lifecycle (T5).

The rest of T5 (CoW copy, device adds, provision, prune) is I/O against the
incus daemon and is verified by a throwaway integration run, like T3/T4.
"""

import pytest

from claude_wrapper.lifecycle import (
    SetupError,
    _check_template_name,
    _template_name,
)


def test_template_name_prefix():
    assert _template_name("api") == "claude-sandbox-api"
    assert _template_name("a-b") == "claude-sandbox-a-b"


@pytest.mark.parametrize("ctx", ["api", "a-b", "ctx1", "x", "WORK"])
def test_valid_context_names_accepted(ctx):
    _check_template_name(ctx)  # must not raise


@pytest.mark.parametrize(
    "ctx",
    [
        "with_underscore",  # underscore illegal in incus names
        "has space",  # space illegal
        "trailing-",  # full name ends with a dash
        "uçt",  # non-ASCII
        "a" * 60,  # claude-sandbox- (15) + 60 = 75 > 63
    ],
)
def test_invalid_context_names_rejected(ctx):
    with pytest.raises(SetupError):
        _check_template_name(ctx)
