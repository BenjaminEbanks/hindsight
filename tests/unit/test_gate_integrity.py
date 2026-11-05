"""Gate stage 0 integrity tests."""

from hindsight.gate.integrity import check_integrity


def _patch(lines: list[str]) -> str:
    return "\n".join(lines)


def test_deleted_test_rejected():
    patch = _patch([
        "+++ b/test_rate_limiter.py",
        "-def test_limit():",
        "     assert True",
    ])
    r = check_integrity(patch)
    assert not r.passed
    assert r.findings[0]["kind"] == "deleted_test"


def test_skip_marker_rejected():
    patch = _patch([
        "+++ b/test_rate_limiter.py",
        "+@pytest.mark.skip(reason='flaky')",
        "+def test_limit():",
    ])
    r = check_integrity(patch)
    assert not r.passed
    assert r.findings[0]["kind"] == "skip_marker"


def test_xfail_and_assert_true_rejected():
    patch = _patch([
        "+++ b/test_x.py",
        "+@pytest.mark.xfail",
        "+def test_a(): assert True",
    ])
    r = check_integrity(patch)
    assert not r.passed
    kinds = {f["kind"] for f in r.findings}
    assert "assert_true" in kinds


def test_selection_config_rejected():
    patch = _patch([
        "+++ b/pytest.ini",
        "+addopts = -q -k limiter",
    ])
    r = check_integrity(patch)
    assert not r.passed


def test_clean_patch_passes():
    patch = _patch([
        "+++ b/limiter.py",
        "+class RateLimiter:",
        "+    pass",
        "+++ b/test_limiter.py",
        "+def test_new_thing():",
        "+    assert RateLimiter() is not None",
    ])
    r = check_integrity(patch)
    assert r.passed


def test_forbidden_dependency_rejected():
    patch = _patch([
        "+++ b/requirements.txt",
        "+redis==5.0.0",
    ])
    r = check_integrity(patch, forbidden_deps=["redis"])
    assert not r.passed
    assert r.findings[0]["kind"] == "forbidden_dependency"
