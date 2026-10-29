"""Doctor warns when a Project's display handle map nears its size cap (#1550)."""

import pytest

from booley.harness import doctor
from booley.runtime import handles

THRESHOLD = handles.MAX_MAP_BYTES * 3 // 4


@pytest.mark.parametrize(
    ("jobs", "excess"),
    [
        pytest.param(False, 0, id="session-at-threshold"),
        pytest.param(False, 1, id="session-above"),
        pytest.param(True, 0, id="job-at-threshold"),
        pytest.param(True, 1, id="job-above"),
    ],
)
def test_handle_map_size_warns_only_above_three_quarters_of_cap(tmp_path, jobs, excess):
    store = handles.HandleStore(tmp_path, jobs=jobs)
    store.root.mkdir(parents=True)
    with store.path.open("wb") as stream:
        stream.truncate(THRESHOLD + excess)
    reporter = doctor._Reporter.create()
    doctor._check_handle_map_size(tmp_path, reporter.warn_)
    findings = reporter.findings
    if not excess:
        assert findings == []
        return
    assert [f.check_id for f in findings] == ["handles.map-size"]
    assert findings[0].subject == ("job" if jobs else "session")
    assert "16 MiB" in findings[0].message
    assert "booley session down" in findings[0].fix
    assert "booley session up" in findings[0].fix
