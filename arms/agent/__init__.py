"""agent arm: NL understanding + shopper chat (AgentService), the per-robot managing LLM (RobotBrainService) and the
planner -> VLA -> verifier loop's planner (PlannerService) + runner (LoopRunner).

Public API (used only by the gateway composition root):
    create(services)              -> real LLM-backed AgentService (falls back to rules without an LLM key)
    create_fake(services)         -> deterministic rule-based AgentService (base deps only)
    create_brain(services)        -> LLM robot brain (tool loop; scripted fallback without a key / on LLM failure)
    create_brain_fake(services)   -> deterministic scripted robot brain (same tool sequence, no LLM)
    create_planner(services)      -> LLM planner (utils.llm, Gemini by default; scripted draft on any LLM failure)
    create_planner_fake(services) -> deterministic scripted planner (taco rules + catalog lookups)
    create_runner(services)       -> the loop runner (deterministic; settings applied)
    create_runner_fake(services)  -> the same runner class (its fakes are whatever it is wired to)
None of them calls another service at construction time. The brain consults ``loop.resolve_exec_mode`` per goal:
PICKY_EXEC_MODE=plan (default when Services.vla + Services.verifier are wired) runs shop / fetch goals through the
runner; 'direct' is the exact v0.2 path.
"""

from contracts.services import AgentService, LoopRunner, PlannerService, RobotBrainService, Services


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


def create_planner(services: Services) -> PlannerService:
    from arms.agent.planner import LlmPlanner

    return LlmPlanner(services)


def create_planner_fake(services: Services) -> PlannerService:
    from arms.agent.planner_fake import ScriptedPlanner

    return ScriptedPlanner(services)


def create_runner(services: Services) -> LoopRunner:
    from arms.agent.loop import build_runner

    return build_runner(services)


def create_runner_fake(services: Services) -> LoopRunner:
    from arms.agent.loop import build_runner

    return build_runner(services)


__all__ = [
    "create",
    "create_brain",
    "create_brain_fake",
    "create_fake",
    "create_planner",
    "create_planner_fake",
    "create_runner",
    "create_runner_fake",
]
