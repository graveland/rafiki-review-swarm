# tests: the example home as a fixture - a project that resolves, a house lens
# that only appears as the third layer, PR seats scoped to review-pr, and a
# launcher shim that passes its own root as --home.
import json
from pathlib import Path

from review_swarm import lenses as lenses_mod
from review_swarm.common import LENSES
from review_swarm.runs import Home

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "home"


def test_example_project_resolves(tmp_path):
    assert Home(EXAMPLES, tmp_path).project_dir("example") == EXAMPLES.resolve() / "projects" / "example"


def test_example_home_lens_is_a_third_layer(tmp_path):
    home = Home(EXAMPLES, tmp_path)
    built = lenses_mod.build(LENSES, None, set(), tmp_path, home_dir=home.lens_dir)
    names = {lens.name for lens in built}
    assert "example-house-rule" in names
    assert {lens.name for lens in lenses_mod.load_layers(LENSES)} <= names
    assert (tmp_path / "lenses.manifest.json").exists()


def test_example_home_lens_absent_without_home_layer(tmp_path):
    built = lenses_mod.build(LENSES, None, set(), tmp_path)
    assert "example-house-rule" not in {lens.name for lens in built}


def test_example_pr_json_names_only_review_pr_seats():
    config = json.loads((EXAMPLES / "projects" / "example" / "pr.json").read_text())
    seats = [config["finder"], config["gather"], *config["panel"]]
    for verifier, target in config["verifiers"].items():
        seats.extend([verifier, target])
    assert seats
    assert all(seat.startswith("review-pr:") for seat in seats)


def test_example_shim_passes_its_own_root_as_home():
    shim = (EXAMPLES / "scripts" / "review.py").read_text()
    assert "parents[1]" in shim
    assert '"--home"' in shim
    assert '"--data"' not in shim
