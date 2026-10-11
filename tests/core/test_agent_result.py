from booley.core.models import AgentResult


class TestAgentResult:
    def test_defaults(self):
        r = AgentResult()
        assert r.output == ""
        assert r.structured is None
