"""Tests for criterion source-fingerprint evidence."""

from booley.flows.criterion_freshness import build_criterion_freshness


def test_builds_compatible_freshness_detail(tmp_path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n")

    freshness = build_criterion_freshness(
        tmp_path,
        target="sim",
        categories=("tb", "rtl", "rtl"),
    ).to_detail()

    assert freshness["target"] == "sim"
    assert freshness["categories"] == ["rtl", "tb"]
    assert freshness["fingerprint"]["algorithm"] == "sha256"
