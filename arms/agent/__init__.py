"""agent arm: the LLM brain (intent expansion, split sourcing, chat with tools, reply classification, follow-ups).

Public API (used only by the gateway composition root):
    create(services)      -> real Claude-backed AgentService (falls back to rules without ANTHROPIC_API_KEY)
    create_fake(services) -> deterministic rule-based AgentService (base deps only)
Neither calls another service at construction time.
"""

from contracts.services import AgentService, Services


def create(services: Services) -> AgentService:
    from arms.agent.service import ClaudeAgent

    return ClaudeAgent(services)


def create_fake(services: Services) -> AgentService:
    from arms.agent.fake import FakeAgent

    return FakeAgent(services)


__all__ = ["create", "create_fake"]
