"""The llama-swap configuration reader: model IDs, matrix vars, co-residency.

The pool only reads this file. A set expression names the combinations that
may be loaded together, and any subset of one combination is allowed, so a
group of models fits when one expansion of one set holds all of them.
"""

from pathlib import Path

import pytest

from specflo.errors import SpecfloError
from specflo.pool import matrix

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "pool" / "llama-swap.yaml"


def _write(tmp_path, text):
    path = tmp_path / "llama-swap.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _config(tmp_path, sets, names="abcdef"):
    """A configuration whose models are ``model-<n>`` with var ``<n>``."""
    lines = ["models:"]
    lines += [f"  model-{n}:\n    cmd: serve {n}" for n in names]
    lines += ["matrix:", "  vars:"]
    lines += [f"    {n}: model-{n}" for n in names]
    lines += ["  sets:"]
    lines += [f'    {name}: "{expr}"' for name, expr in sets.items()]
    return matrix.read(_write(tmp_path, "\n".join(lines) + "\n"))


def test_lists_the_model_ids():
    cfg = matrix.read(FIXTURE)
    assert cfg.model_ids == ("model-a", "model-b", "model-c", "model-d")


def test_resolves_a_var_to_its_model_id():
    cfg = matrix.read(FIXTURE)
    assert cfg.model_id("a") == "model-a"


def test_a_full_model_id_resolves_to_itself():
    cfg = matrix.read(FIXTURE)
    assert cfg.model_id("model-a") == "model-a"


def test_an_unknown_name_is_refused():
    cfg = matrix.read(FIXTURE)
    with pytest.raises(SpecfloError, match="nope"):
        cfg.model_id("nope")


def test_models_on_both_sides_of_an_and_fit():
    cfg = matrix.read(FIXTURE)
    assert cfg.fits(["model-a", "model-c"]) is True


def test_alternatives_do_not_fit_together():
    cfg = matrix.read(FIXTURE)
    assert cfg.fits(["model-a", "model-b"]) is False


def test_a_model_fits_with_itself():
    cfg = matrix.read(FIXTURE)
    assert cfg.fits(["model-a", "model-a"]) is True


def test_a_model_in_no_set_fits_alone_and_with_nothing_else():
    cfg = matrix.read(FIXTURE)
    assert cfg.fits(["model-d"]) is True
    assert cfg.fits(["model-d", "model-d"]) is True
    for other in ("model-a", "model-b", "model-c"):
        assert cfg.fits(["model-d", other]) is False


def test_a_model_in_no_set_with_a_fails_with_a(tmp_path):
    # e sits in a set, but in none that also holds a.
    cfg = _config(tmp_path, {"one": "(a | b) & c", "two": "e & f"})
    assert cfg.fits(["model-e", "model-f"]) is True
    assert cfg.fits(["model-a", "model-e"]) is False


def test_pairwise_allowed_is_not_enough(tmp_path):
    # Each pair has its own set, but no one expansion holds all three.
    cfg = _config(tmp_path, {"ab": "a & b", "bc": "b & c", "ac": "a & c"})
    assert cfg.fits(["model-a", "model-b"]) is True
    assert cfg.fits(["model-b", "model-c"]) is True
    assert cfg.fits(["model-a", "model-c"]) is True
    assert cfg.fits(["model-a", "model-b", "model-c"]) is False


def test_fits_takes_var_names_too():
    cfg = matrix.read(FIXTURE)
    assert cfg.fits(["a", "model-c"]) is True
    assert cfg.fits(["a", "b"]) is False


def test_a_set_may_use_full_model_ids_and_refer_to_another_set(tmp_path):
    # vars are optional in llama-swap, and +name pulls in a named set.
    cfg = matrix.read(
        _write(
            tmp_path,
            "models:\n  m1: {cmd: x}\n  m2: {cmd: x}\n  m3: {cmd: x}\n"
            'matrix:\n  sets:\n    base: "m1 | m2"\n    both: "+base & m3"\n',
        )
    )
    assert cfg.fits(["m1", "m3"]) is True
    assert cfg.fits(["m1", "m2"]) is False


def test_a_configuration_with_no_matrix_lets_each_model_run_alone(tmp_path):
    cfg = matrix.read(_write(tmp_path, "models:\n  m1: {cmd: x}\n  m2: {cmd: x}\n"))
    assert cfg.model_ids == ("m1", "m2")
    assert cfg.fits(["m1"]) is True
    assert cfg.fits(["m1", "m2"]) is False


@pytest.mark.parametrize(
    "expr, needle",
    [
        ("a & zz", "zz"),  # a name that is neither a var nor a model ID
        ("(a | b", "pair"),  # unbalanced
        ("a & ", "pair"),  # dangling operator
        ("a b", "pair"),  # two names with no operator
    ],
)
def test_a_bad_set_expression_names_the_set(tmp_path, expr, needle):
    with pytest.raises(SpecfloError, match=needle):
        _config(tmp_path, {"pair": expr})


def test_a_var_naming_an_absent_model_is_refused(tmp_path):
    text = "models:\n  m1: {cmd: x}\nmatrix:\n  vars:\n    g: ghost\n"
    with pytest.raises(SpecfloError, match="ghost"):
        matrix.read(_write(tmp_path, text))


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(SpecfloError, match="missing.yaml"):
        matrix.read(tmp_path / "missing.yaml")


def test_a_file_that_is_not_utf8_is_refused_naming_the_file(tmp_path):
    path = tmp_path / "llama-swap.yaml"
    path.write_bytes(b"models:\n  m1: {cmd: caf\xe9}\n")

    with pytest.raises(SpecfloError, match="llama-swap.yaml is not UTF-8"):
        matrix.read(path)
