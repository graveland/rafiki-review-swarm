# review-swarm: checked-in seats pass the daemon's per-kind preset rule
"""The daemon refuses a claude- or script-kind preset that sets any field the
kind's engine cannot honour (rafiki pkg/presets Validate), even an empty list.
The stage tests run on fakes that accept anything, so this is the only check
that a seat file will survive `presets sync`."""

import pytest

from review_swarm.presets import checkout_seats

# Mirrors rafiki pkg/presets Validate. Present-but-empty counts as set there.
CLAUDE_FORBIDDEN = ("thinking", "tools", "skills", "mcp_servers", "context_files", "system_prompt")
SCRIPT_FORBIDDEN = CLAUDE_FORBIDDEN + ("model", "provider")


@pytest.mark.parametrize("name,spec", sorted(checkout_seats().items()))
def test_seat_sets_no_field_its_kind_refuses(name, spec):
    kind = spec.get("kind", "fundi")
    forbidden = {"claude": CLAUDE_FORBIDDEN, "script": SCRIPT_FORBIDDEN}.get(kind, ())
    set_fields = [f for f in forbidden if f in spec and spec[f] not in (None, "")]
    assert not set_fields, f"{name} ({kind}) sets {set_fields}"
