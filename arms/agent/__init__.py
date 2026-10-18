"""agent arm: NL understanding + shopper chat (AgentService) and the per-robot managing LLM (RobotBrainService).

Public API (used only by the gateway composition root):
    create(services)            -> real Claude-backed AgentService (falls back to rules without ANTHROPIC_API_KEY)
    create_fake(services)       -> deterministic rule-based AgentService (base deps only)
    create_brain(services)      -> Claude robot brain (tool loop; scripted fallback without a key / on LLM failure)
    create_brain_fake(services) -> deterministic scripted robot brain (same tool sequence, no LLM)
None of them calls another service at construction time.
"""

from contracts.services import AgentService, RobotBrainService, Services


def create(services: Services) -> AgentService:
    from arms.agent.service import ClaudeAgent

    return ClaudeAgent(services)


def create_fake(services: Services) -> AgentService:
    from arms.agent.fake import FakeAgent

    return FakeAgent(services)


def create_brain(services: Services) -> RobotBrainService:
    from arms.agent.brain import ClaudeBrain

    return ClaudeBrain(services)


def create_brain_fake(services: Services) -> RobotBrainService:
    from arms.agent.brain import ScriptedBrain

    return ScriptedBrain(services)


__all__ = ["create", "create_brain", "create_brain_fake", "create_fake"]
