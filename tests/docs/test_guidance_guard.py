import pytest


@pytest.mark.parametrize("source", ["generated", "handwritten"])
@pytest.mark.parametrize("unknown", ["booley_not_a_tool", "not_a_registered_tool"])
def test_guidance_guard_rejects_unknown_tool_name(source, unknown):
    from tests.docs import test_goal_work_dir_guidance as guard

    from booley.goals.rules import goal_mode_rules
    from booley.mcp import catalog

    available = {tool["name"] for tool in catalog._default_interactive_definitions()}
    text = goal_mode_rules(routing_guidance=catalog.work_dir_guidance())
    if source == "handwritten":
        text = (guard.REPO_ROOT / guard.ROUTING_DOCUMENTS[0]).read_text(encoding="utf-8")
    guard._assert_guidance_names(text, available, source)
    assert "goal_finish" in text
    text = text.replace("goal_finish", unknown)
    with pytest.raises(AssertionError, match=unknown):
        guard._assert_guidance_names(text, available, source)
