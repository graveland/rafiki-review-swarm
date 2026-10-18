# tests: running the review CLI as a rafiki script child.
"""The contract these tests pin down: a stage run takes its data dir from
REVIEW_SWARM_DATA when --data is not passed, and still refuses to run with
neither; and a live Target inside a rafiki child (RAFIKI_CHILD_CONNECT set)
talks to the daemon over the child's own socket and records the daemon version
as unread instead of shelling out to a CLI profile the executor does not have,
while a sandbox Target keeps using its profile."""
import pytest

from review_swarm import cli, sandbox
from review_swarm.common import Fail
from review_swarm.runs import Home
from review_swarm.sandbox import Target


def test_stage_parser_accepts_a_stage_without_data():
    args = cli.stage_parser("review").parse_args(["--home", "/h", "compare-pr", "r1"])
    assert args.data is None


def test_review_main_takes_the_data_dir_from_the_environment(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("REVIEW_SWARM_DATA", str(data))
    seen = {}
    monkeypatch.setattr(cli, "compare_pr_cmd", lambda home, args: seen.setdefault("home", home))
    assert cli.review_main(["--home", str(tmp_path), "compare-pr", "r1"]) == 0
    assert seen["home"].data == data.resolve()


def test_review_main_refuses_with_neither_data_flag_nor_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEW_SWARM_DATA", raising=False)
    monkeypatch.setattr(cli, "compare_pr_cmd", lambda home, args: pytest.fail("ran without a data dir"))
    with pytest.raises(Fail, match="no data dir"):
        cli.review_main(["--home", str(tmp_path), "compare-pr", "r1"])


@pytest.fixture
def daemon(monkeypatch):
    """Records which constructor a Target used, and fails any CLI shell-out."""
    calls = []
    monkeypatch.setattr(sandbox.Rafiki, "inside", classmethod(lambda cls: calls.append("inside") or "inside-client"))
    monkeypatch.setattr(sandbox.Rafiki, "from_profile",
                        classmethod(lambda cls, profile=None: calls.append(("profile", profile)) or "profile-client"))
    return calls


def test_live_target_inside_a_child_uses_the_child_socket(tmp_path, monkeypatch, daemon):
    monkeypatch.setenv("RAFIKI_CHILD_CONNECT", "/run/child.sock")
    t = Target("live", Home(tmp_path, tmp_path / "data"))
    assert t.client() == "inside-client"
    assert daemon == ["inside"]


def test_live_target_outside_a_child_uses_its_profile(tmp_path, monkeypatch, daemon):
    monkeypatch.delenv("RAFIKI_CHILD_CONNECT", raising=False)
    t = Target("live", Home(tmp_path, tmp_path / "data"), profile="work")
    assert t.client() == "profile-client"
    assert daemon == [("profile", "work")]


def test_sandbox_target_inside_a_child_still_uses_its_profile(tmp_path, monkeypatch, daemon):
    monkeypatch.setenv("RAFIKI_CHILD_CONNECT", "/run/child.sock")
    monkeypatch.setattr(sandbox.Sandbox, "env", lambda self: {"RAFIKI_CHILD_CONNECT": "/run/child.sock"})
    monkeypatch.setattr(sandbox.os, "environ", dict(sandbox.os.environ))
    t = Target("sandbox", Home(tmp_path, tmp_path / "data"))
    assert t.client() == "profile-client"
    assert daemon == [("profile", sandbox.PROFILE)]


def test_live_target_inside_a_child_records_the_version_as_unread(tmp_path, monkeypatch):
    monkeypatch.setenv("RAFIKI_CHILD_CONNECT", "/run/child.sock")
    t = Target("live", Home(tmp_path, tmp_path / "data"))
    monkeypatch.setattr(t, "cli", lambda *a, **k: pytest.fail("shelled out to the rafiki CLI inside a child"))
    assert t.describe() == {"target": "live", "version": "unread (run inside a rafiki child)"}


def test_live_target_outside_a_child_reads_the_version_from_status(tmp_path, monkeypatch):
    monkeypatch.delenv("RAFIKI_CHILD_CONNECT", raising=False)
    t = Target("live", Home(tmp_path, tmp_path / "data"))
    monkeypatch.setattr(t, "cli", lambda *a, **k: type("R", (), {"stdout": "version: v0.2.0\n"})())
    assert t.describe() == {"target": "live", "version": "v0.2.0"}
