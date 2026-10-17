# review-swarm: a child with no captured transcript yet is not an error
"""The daemon answers not_found for a child whose first model request has not
been captured. A claude child reports idle in that window, so settled() must
keep waiting on it; once the child has exited or run out of time there is
nothing more to wait for, and it settles without a transcript."""

import pytest
from rafiki import ConnectError

from review_swarm.stages import settled


class _NoTranscript:
    def __init__(self, status, code="not_found"):
        self.status, self.code = status, code

    def child_info(self, cid):
        return {"child_id": cid, "status": self.status}

    def export(self, cid):
        raise ConnectError(self.code, f"child {cid} has no captured conversation yet; retry")


def test_idle_child_without_a_transcript_is_not_settled():
    assert settled(_NoTranscript("idle"), "c_1") is None


@pytest.mark.parametrize("status,timed_out", [("exited", False), ("idle", True)])
def test_finished_child_without_a_transcript_settles_empty(status, timed_out):
    info, conv = settled(_NoTranscript(status), "c_1", timed_out=timed_out)
    assert info["status"] == status and conv is None


def test_other_export_errors_still_raise():
    with pytest.raises(ConnectError):
        settled(_NoTranscript("exited", code="unavailable"), "c_1")
