import pytest


@pytest.mark.parametrize("source", ["generated", "handwritten"])
def test_guidance_guard_rejects_unknown_tool_name(source):
    from tests.docs import test_goal_work_dir_guidance as guard

    from booley.goals.rules import goal_mode_rules
    from booley.mcp import catalog

    available = {tool["name"] for tool in catalog._default_interactive_definitions()}
    text = goal_mode_rules(routing_guidance=catalog.work_dir_guidance())
    if source == "handwritten":
        text = (guard.REPO_ROOT / guard.ROUTING_DOCUMENTS[0]).read_text(encoding="utf-8")
    guard._assert_guidance_names(text, available, source)
    marker = "accept no `work_dir`."
    text = text.replace(marker, marker + " Reference: `booley_not_a_tool`.", 1)
    with pytest.raises(AssertionError, match="booley_not_a_tool"):
        guard._assert_guidance_names(text, available, source)
