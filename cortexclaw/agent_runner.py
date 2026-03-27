"""Agent runner — bridges the orchestrator to the Cortex Code Agent SDK.

Replaces NanoClaw's container-runner.ts: instead of spinning up Docker
containers, we call the SDK's ``query()`` directly as a subprocess.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Coroutine, Optional

from cortex_code_agent_sdk import (
    AssistantMessage,
    CortexCodeAgentOptions,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
    ToolPermissionContext,
    query,
)

from .config import CORTEX_CLI_PATH, CORTEX_CONNECTION, GROUPS_DIR
from .types import AgentOutput, RegisteredGroup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Permission callback
# ---------------------------------------------------------------------------


async def _auto_approve(
    tool_name: str,
    tool_input: dict,
    context: ToolPermissionContext,
) -> PermissionResultAllow:
    """Auto-approve all tool calls (mirrors NanoClaw's --dangerously-skip-permissions)."""
    logger.debug("Auto-approved tool: %s", tool_name)
    return PermissionResultAllow()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

OnAgentOutput = Callable[[AgentOutput], Coroutine[None, None, None]]


async def run_agent(
    group: RegisteredGroup,
    prompt: str,
    chat_jid: str,
    on_output: OnAgentOutput | None = None,
    continue_conversation: bool = False,
) -> AgentOutput:
    """Run a Cortex Code agent for the given group and prompt.

    When *continue_conversation* is ``True`` the SDK passes ``--continue``
    to the CLI which resumes the last session in the group's cwd.

    Returns an :class:`AgentOutput` containing the status, result text,
    and — crucially — the ``session_id`` reported by the CLI so the
    orchestrator can persist it for future continuations.
    """
    group_dir = GROUPS_DIR / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    # Prepend CLAUDE.md instructions if present
    claude_md = group_dir / "CLAUDE.md"
    full_prompt = prompt
    if claude_md.exists():
        instructions = claude_md.read_text()
        full_prompt = (
            f"<system-instructions>\n{instructions}\n</system-instructions>\n\n"
            f"{prompt}"
        )

    options = CortexCodeAgentOptions(
        cwd=str(group_dir),
        connection=CORTEX_CONNECTION or None,
        can_use_tool=_auto_approve,
        cli_path=CORTEX_CLI_PATH,
        continue_conversation=continue_conversation,
    )

    collected_text: list[str] = []
    status = "success"
    error_msg: str | None = None
    session_id: str | None = None

    try:
        async for message in query(prompt=full_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text") and block.text:
                        collected_text.append(block.text)

            elif isinstance(message, ResultMessage):
                session_id = message.session_id
                if message.subtype == "error":
                    status = "error"
                    error_msg = "Agent returned error result"

                # Emit final output
                result_text = "".join(collected_text).strip() or None
                output = AgentOutput(
                    result=result_text,
                    status=status,
                    error=error_msg,
                    session_id=session_id,
                )
                if on_output:
                    await on_output(output)

            elif isinstance(message, SystemMessage):
                # Log system messages for debugging
                if hasattr(message, "message") and message.message:
                    logger.debug("System: %s", message.message)

    except Exception as e:
        logger.error("Agent run failed for group %s: %s", group.folder, e)
        status = "error"
        error_msg = str(e)
        output = AgentOutput(result=None, status="error", error=error_msg)
        if on_output:
            await on_output(output)
        return output

    return AgentOutput(
        result="".join(collected_text).strip() or None,
        status=status,
        error=error_msg,
        session_id=session_id,
    )
