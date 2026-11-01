"""
AgentSUMO - AI Agent for SUMO Simulations

Version 0.1: Basic version with chat + tool calling
"""

import anthropic
from typing import List, Dict, Any, Optional, Callable
import json
import logging
import os
import asyncio
import re
from types import SimpleNamespace
from datetime import datetime, timezone
from uuid import uuid4

from agentsumo.client import SUMOMCPClient
from agentsumo.core import AgentSUMOConfig
from agentsumo.agent.prompts import PromptBuilder



logger = logging.getLogger("agentsumo.agent")


SPECIALIST_AGENTS = (
    ("Scenario Architect", "Review the full setup: location and study area, required inputs, network generation, demand source, route generation, and assumptions. Identify missing parameters and the next valid setup stage."),
    ("Traffic Analyst", "Review demand and baseline setup: likely congestion mechanisms, whether traffic assumptions fit the study area, and which baseline metrics and outputs are needed."),
    ("Policy Strategist", "Review whether a policy experiment is in scope. Propose at most two testable interventions using routing, lane, speed, or signal changes; keep baseline separate and state trade-offs."),
    ("Impact Analyst", "Review evaluation setup: define comparison metrics such as travel time, waiting time, queues, throughput, and emissions; identify required outputs, limits, and uncertainty."),
)


class AgentSUMO:
    """
    Claude + MCP based conversational SUMO simulation Agent.

    Version 0.2: Self-Adaptive Prompting
    - Multi-turn conversation
    - Tool calling via MCP
    - State tracking
    - Self-adaptive reasoning (Simple/Complex/Agentic)
    - Prompt caching (90% cost reduction)

    Features:
    - Claude judges task complexity on its own
    - Automatically selects appropriate reasoning mode
    - Chain-of-Thought for complex tasks
    - Extended Thinking for agentic tasks
    """

    def __init__(
        self,
        model: str = None,
        max_tokens: int = 4096,
        enable_logging: bool = True,
        enable_debug: bool = False,
        interface: str = "cli",
        db_path: Optional[str] = None,
        system_prompt: Optional[str] = None,
        enable_sumo_mcp: bool = True,
        enable_sqlite_mcp: bool = None,
        enable_filesystem_mcp: bool = False,
        filesystem_directories: Optional[List[str]] = None,
        inject_state: bool = True  # [Temporary] For SUMOLanguageExpert. Will be removed later.
    ):
        """
        Args:
            model: Claude model (default. config.DEFAULT_MODEL)
            max_tokens: Maximum response tokens
            enable_logging: Enable logging
            enable_debug: Debug mode (show raw response)
            interface: Runtime environment ("cli" or "web")
            db_path: SQLite DB path (if None, auto-detect from default path)
            system_prompt: Custom system prompt (if None, uses unified_system.md)
            enable_sumo_mcp: Whether to enable SUMO MCP (if False, SQLite only)
            enable_sqlite_mcp: Whether to enable SQLite MCP (if None, auto-enable when db_path exists)
            enable_filesystem_mcp: Whether to enable Filesystem MCP (if True, provides file access tools)
            filesystem_directories: List of directory paths the Filesystem MCP can access (required when enable_filesystem_mcp=True)
            inject_state: [Temporary] For SUMOLanguageExpert experiments. Will be removed later.
                         Whether to inject state context into the user message (default. True)
                         Set to False when state is not needed, as in Text-to-SQL experiments.
        """
        self.debug = enable_debug
        self.interface = interface
        self._init_db_path = db_path  # Used in start()
        self._custom_system_prompt = system_prompt  # Custom system prompt
        self._enable_sumo_mcp = enable_sumo_mcp  # Whether to enable SUMO MCP
        self._enable_sqlite_mcp = enable_sqlite_mcp  # Whether to enable SQLite MCP
        self._enable_filesystem_mcp = enable_filesystem_mcp  # Whether to enable Filesystem MCP
        self._filesystem_directories = filesystem_directories  # List of directories the Filesystem MCP can access
        self._inject_state = inject_state  # [Temporary] Whether to inject state. For SUMOLanguageExpert.
        # Reasoning provider initialization. Groq exposes an OpenAI-compatible
        # endpoint, while the existing Anthropic implementation remains intact.
        self.provider = AgentSUMOConfig.get_llm_provider()
        if self.provider == "anthropic":
            self.claude = anthropic.Anthropic(
                api_key=AgentSUMOConfig.get_claude_api_key(), timeout=60.0, max_retries=1
            )
            self.groq = None
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "Groq support requires the 'openai' package. Run 'uv sync'."
                ) from exc
            self.groq = OpenAI(
                api_key=AgentSUMOConfig.get_groq_api_key(),
                base_url="https://api.groq.com/openai/v1", timeout=60.0, max_retries=1,
            )
            self.claude = None

        # Settings
        default_model = (AgentSUMOConfig.DEFAULT_MODEL if self.provider == "anthropic"
                         else AgentSUMOConfig.DEFAULT_GROQ_MODEL)
        self.model = model or os.environ.get("LLM_MODEL") or default_model
        self.max_tokens = max_tokens
        self.multi_agent_enabled = os.environ.get("MULTI_AGENT_MODE", "true").strip().lower() not in {"0", "false", "no"}

        # Logging
        if enable_logging:
            logging.basicConfig(
                level=logging.INFO,
                format='%(asctime)s [%(levelname)s] %(message)s'
            )

        # Multiple MCP Clients
        self.mcp_clients: Dict[str, Any] = {
            "sumo": None,     # SUMO MCP Server (required)
            "sqlite": None,   # SQLite MCP Server (optional)
            "filesystem": None  # Filesystem MCP Server (optional, for ablation)
        }
        self.tools: List[Dict] = []

        # Conversation history (official multi-turn pattern)
        self.conversation_history: List[Dict[str, Any]] = []

        # State tracking (with SQLite support extensions)
        self.simulation_state = {
            "current_network": None,
            "baseline_network": None,     # Original network (for policy comparison)
            "current_routes": None,
            "last_results": None,
            "current_db": None,           # SQLite DB path
            "sqlite_enabled": False,      # SQLite MCP activation state
            "simulations": []             # List of simulations stored in DB
        }

        # Prompt Builder (dynamic prompt generation)
        self.prompt_builder = PromptBuilder()

        # Progress callback for real-time updates (optional)
        self._progress_callback: Optional[Callable[[str, str], None]] = None
        # Durable operation records make the agent's actual tool orchestration
        # inspectable in the web interface and after a server restart.
        self._active_execution: Optional[Dict[str, Any]] = None

        logger.info("AgentSUMO initialization complete")

    def set_progress_callback(self, callback: Callable[[str, str], None]):
        """
        Set a callback for progress updates during tool execution.

        Args:
            callback: Function that takes (tool_name, status) as arguments
                     status can be: "started", "completed", "error"
        """
        self._progress_callback = callback

    async def _emit_progress(self, tool_name: str, status: str, message: str = None):
        """Emit progress update if callback is set"""
        if tool_name != "__intermediate_text__":
            self._record_execution_step(tool_name, status, message)
        if self._progress_callback:
            try:
                import inspect
                result = self._progress_callback(tool_name, status, message)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    def _execution_log_path(self):
        return AgentSUMOConfig.OUTPUT_DIR / ".agent_executions.json"

    def _begin_execution(self, user_message: str) -> None:
        """Start a concise, non-sensitive audit record for a chat operation."""
        self._active_execution = {
            "id": str(uuid4()),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "status": "running",
            "request_preview": user_message.strip().replace("\n", " ")[:180],
            "steps": [],
            "error": None,
        }
        self._save_execution_history()

    def _record_execution_step(self, tool_name: str, status: str, message: Optional[str]) -> None:
        if not self._active_execution:
            return
        self._active_execution["steps"].append({
            "tool": tool_name,
            "status": status,
            "at": datetime.now(timezone.utc).isoformat(),
            "message": (message or "")[:240],
        })
        self._save_execution_history()

    def _finish_execution(self, status: str, error: Optional[Exception] = None) -> None:
        if not self._active_execution:
            return
        self._active_execution["status"] = status
        self._active_execution["finished_at"] = datetime.now(timezone.utc).isoformat()
        self._active_execution["error"] = str(error)[:240] if error else None
        self._save_execution_history()
        self._active_execution = None

    def _save_execution_history(self) -> None:
        """Persist the active execution plus the newest 49 completed runs."""
        try:
            path = self._execution_log_path()
            history = []
            if path.exists():
                history = json.loads(path.read_text())
            if self._active_execution:
                history = [r for r in history if r.get("id") != self._active_execution["id"]]
                history.insert(0, self._active_execution)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(history[:50], indent=2))
        except Exception as exc:
            logger.warning("Could not save execution history: %s", exc)

    async def _run_specialist_agents(self, user_message: str) -> str:
        """Gather independent, advisory findings before the coordinator uses tools.

        Specialists never receive MCP tools or execute side effects. The
        coordinator remains the only agent that can create files or run SUMO.
        """
        if not self.multi_agent_enabled:
            return ""

        state_summary = {
            key: self.simulation_state.get(key)
            for key in ("current_network", "current_routes", "last_results", "current_db")
        }
        findings = []
        for name, role in SPECIALIST_AGENTS:
            await self._emit_progress(name, "started", f"{name} is reviewing the mission")
            try:
                finding = await asyncio.to_thread(self._call_specialist, name, role, user_message, state_summary)
                findings.append(f"### {name}\n{finding}")
                await self._emit_progress(name, "completed", f"{name} submitted a finding")
            except Exception as exc:
                # A specialist failure must not prevent the coordinator from
                # completing a simulation request.
                logger.warning("%s failed: %s", name, exc)
                await self._emit_progress(name, "error", str(exc))

        if not findings:
            return ""
        return "\n\n[Multi-agent advisory findings — verify with tools and simulation results]\n" + "\n\n".join(findings)

    def _call_specialist(
        self, name: str, role: str, user_message: str, state_summary: Dict[str, Any]
    ) -> str:
        """Run one isolated specialist with no tool access or shared history."""
        system = (
            f"You are the {name} in an urban-mobility simulation team. {role} "
            "Return a concise, actionable briefing of at most 140 words. "
            "Do not claim measurements you have not seen, do not call tools, "
            "and clearly mark assumptions."
        )
        prompt = (
            f"Mission: {user_message}\n\n"
            f"Current simulation state: {json.dumps(state_summary, default=str)}"
        )
        if self.provider == "groq":
            response = self.groq.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                max_tokens=260,
            )
            return (response.choices[0].message.content or "No finding returned.").strip()

        response = self.claude.messages.create(
            model=self.model,
            max_tokens=260,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "\n".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip() or "No finding returned."

    async def start(self, db_path: Optional[str] = None):
        """
        Start the agent and connect MCP.

        Args:
            db_path: SQLite DB path (if None, auto-detect from default path)

        Tasks:
        1. Connect SUMO MCP Client (required)
        2. Connect SQLite MCP Client (auto-enabled when db_path exists and file is present)
        3. Load and integrate tools
        4. Convert to Claude tools format
        """
        from pathlib import Path as PathlibPath

        logger.info("AgentSUMO starting...")

        # 0a. Make sure the default vehicle_types.add.xml exists in the
        # simulation directory before any tool runs. This covers the case
        # where the user issues a vType-edit request *before* their first
        # simulation — the agent's read_text_file call would otherwise fail.
        # The PyPI helper is idempotent: it leaves any existing (user-edited)
        # file untouched.
        try:
            from agentsumo_mcp.utils.defaults import ensure_vehicle_types_default
            from agentsumo.core import AgentSUMOConfig
            ensure_vehicle_types_default(AgentSUMOConfig.OUTPUT_DIR / "simulations")
        except Exception as e:
            logger.warning(f"Could not seed default vehicle_types.add.xml: {e}")

        # 0b. Restore state from previous session (B: JSON -> A: file scan)
        self._restore_state()

        # 1. Connect SUMO MCP (only when enable_sumo_mcp=True)
        sumo_tools = []
        if self._enable_sumo_mcp:
            logger.info("Connecting to SUMO MCP Server...")
            self.mcp_clients["sumo"] = SUMOMCPClient(tool_timeout=86400.0)
            await self.mcp_clients["sumo"].__aenter__()

            sumo_tools = await self.mcp_clients["sumo"].list_tools()
            logger.info(f"SUMO MCP: {len(sumo_tools)} tools")
        else:
            logger.info("SUMO MCP disabled (SQLite-only mode)")

        # 2. Connect SQLite MCP
        sqlite_tools = []

        if self._enable_sqlite_mcp is not False:
            # Use restored state's current_db if available
            if db_path is None and self.simulation_state.get("current_db"):
                restored_db = self.simulation_state["current_db"]
                if PathlibPath(restored_db).exists() and PathlibPath(restored_db).stat().st_size > 0:
                    db_path = restored_db
                    logger.info(f"DB path restored from state. {PathlibPath(restored_db).name}")

            if db_path is None and self._enable_sqlite_mcp is not True:
                # Auto-detect DB file from default path
                try:
                    from agentsumo_mcp.utils.path_utils import get_output_path

                    analysis_dir = get_output_path("output/analysis")

                    if analysis_dir.exists():
                        db_files = [f for f in analysis_dir.glob("*.db") if f.stat().st_size > 0]

                        if db_files:
                            latest_db = max(db_files, key=lambda p: p.stat().st_mtime)
                            db_path = str(latest_db)
                            logger.info(f"Existing DB file found. {latest_db.name}")
                        else:
                            logger.info("SQLite MCP. No existing DB file. Will auto-enable after DB creation.")
                    else:
                        logger.info("SQLite MCP. analysis directory not found. Will auto-enable after DB creation.")

                except Exception as e:
                    logger.warning(f"SQLite MCP auto-detection failed. {e}. Continuing.")

            if db_path and PathlibPath(db_path).exists() and PathlibPath(db_path).stat().st_size > 0:
                try:
                    from agentsumo.client.sqlite_mcp_client import SQLiteMCPClient

                    logger.info(f"Connecting to SQLite MCP Server... (DB. {db_path})")
                    self.mcp_clients["sqlite"] = SQLiteMCPClient(db_path)
                    await self.mcp_clients["sqlite"].__aenter__()

                    sqlite_tools = await self.mcp_clients["sqlite"].list_tools()

                    self.simulation_state["sqlite_enabled"] = True
                    self.simulation_state["current_db"] = db_path

                    logger.info(f"SQLite MCP: {len(sqlite_tools)} tools")

                except Exception as e:
                    logger.error(f"SQLite MCP connection failed. {e}. Continuing.")
            elif db_path:
                logger.warning(f"DB file does not exist. {db_path}")
        else:
            logger.info("SQLite MCP disabled")

        # 3. Connect Filesystem MCP (only when enable_filesystem_mcp=True)
        filesystem_tools = []

        if self._enable_filesystem_mcp and self._filesystem_directories:
            try:
                from agentsumo.client.filesystem_mcp_client import FilesystemMCPClient

                logger.info(f"Connecting to Filesystem MCP Server... (dirs. {self._filesystem_directories})")
                self.mcp_clients["filesystem"] = FilesystemMCPClient(self._filesystem_directories)
                await self.mcp_clients["filesystem"].__aenter__()

                filesystem_tools = await self.mcp_clients["filesystem"].list_tools()
                logger.info(f"Filesystem MCP: {len(filesystem_tools)} tools")

            except Exception as e:
                logger.error(f"Filesystem MCP connection failed. {e}. Continuing.")
        elif self._enable_filesystem_mcp:
            logger.warning("Filesystem MCP enabled but filesystem_directories not specified.")

        # 4. Integrate tools
        all_mcp_tools = sumo_tools + sqlite_tools + filesystem_tools
        self.tools = self._convert_mcp_to_claude_tools(all_mcp_tools)

        logger.info(f"Loaded {len(self.tools)} tools total")
        logger.info("AgentSUMO ready\n")

    async def close(self):
        """Shut down the agent. Close all MCP connections."""
        logger.info("AgentSUMO shutting down...")

        import asyncio

        for name, client in self.mcp_clients.items():
            if client:
                try:
                    # Attempt shutdown with a 5-second timeout
                    await asyncio.wait_for(
                        client.__aexit__(None, None, None),
                        timeout=5.0
                    )
                    logger.info(f"{name} MCP connection closed")
                except asyncio.TimeoutError:
                    logger.warning(f"{name} MCP shutdown timeout (ignored)")
                except Exception as e:
                    logger.warning(f"{name} MCP shutdown error (ignored). {e}")

        logger.info("AgentSUMO shutdown complete")

    async def __aenter__(self):
        """Context manager enter"""
        await self.start(db_path=self._init_db_path)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        await self.close()
        return False

    def _convert_mcp_to_claude_tools(self, mcp_tools) -> List[Dict]:
        """
        Convert MCP tools to Claude tools format.

        Claude Tool Schema:
        {
            "name": str,
            "description": str,
            "input_schema": dict (JSON Schema)
        }
        """
        claude_tools = []

        for tool in mcp_tools:
            claude_tool = {
                "name": tool.name,
                "description": tool.description or f"Tool: {tool.name}",
                "input_schema": tool.inputSchema
            }
            claude_tools.append(claude_tool)

        logger.debug(f"Converted {len(claude_tools)} tools")
        return claude_tools

    async def chat(self, user_message: str) -> str:
        """
        Converse with the user (multi-turn + self-adaptive prompting).

        Claude judges task complexity on its own and selects the appropriate reasoning mode.

        Args:
            user_message: User message

        Returns:
            str: Agent response
        """
        import time
        start_time = time.time()

        logger.info(f"\nUser: {user_message}")
        self._begin_execution(user_message)

        try:
            # 0. Check for SQLite MCP auto-activation (auto-connect after DB creation)
            await self._check_and_enable_sqlite_if_needed()

            # 1. Append state context to the user message (Anthropic caching optimization)
        # [Temporary] If inject_state=False, do not inject state (for SUMOLanguageExpert experiments)
            if self._inject_state:
                state_context = self.prompt_builder.format_context(self.simulation_state)
                message_with_context = f"{state_context}\n\n---\n\n{user_message}"
            else:
                message_with_context = user_message

            specialist_findings = await self._run_specialist_agents(user_message)
            if specialist_findings:
                message_with_context += specialist_findings

            self.conversation_history.append({
                "role": "user",
                "content": message_with_context
            })

            # 2. Call Claude (system prompt is fixed, state goes in the user message)
            prompt_start = time.time()
            system_prompt = self.prompt_builder.build_unified_prompt(
                interface=self.interface
            )
            # Prepend custom system prompt if provided (e.g., demo mode)
            if self._custom_system_prompt:
                system_prompt = self._custom_system_prompt + "\n\n---\n\n" + system_prompt
            prompt_time = time.time() - prompt_start
            if prompt_time > 0.1:
                logger.debug(f"Prompt build time. {prompt_time:.2f}s")

            claude_start = time.time()
            response = await asyncio.to_thread(self._call_model, system_prompt)
            claude_time = time.time() - claude_start
            logger.debug(f"Claude API call time. {claude_time:.2f}s")

            # Debug. print raw response
            if self.debug:
                self._debug_print_response(response)

            # 3. Handle tool use
            if response.stop_reason == "tool_use":
                tool_start = time.time()
                result = await self._handle_tool_use(response)
                tool_time = time.time() - tool_start
                logger.debug(f"Tool processing time. {tool_time:.2f}s")
                total_time = time.time() - start_time
                logger.info(f"Total response time. {total_time:.2f}s")
                self._finish_execution("completed")
                return result

            # 4. Plain text response (handle all content blocks)
            assistant_text = self._extract_text_from_response(response)

            self.conversation_history.append({
                "role": "assistant",
                "content": response.content
            })

            total_time = time.time() - start_time
            logger.info(f"AgentSUMO: {assistant_text[:100]}...")
            logger.info(f"Total response time. {total_time:.2f}s (Claude. {claude_time:.2f}s)")

            self._finish_execution("completed")
            return assistant_text
        except Exception as exc:
            self._finish_execution("failed", exc)
            raise

    def _call_model(self, system_prompt: str = None):
        """Call the configured model provider and normalize its response."""
        if self.provider == "groq":
            return self._call_groq(system_prompt)
        return self._call_claude(system_prompt)

    def _call_claude(self, system_prompt: str = None):
        """
        Call Claude API (prompt caching + self-adaptive prompting).

        Features:
        - Prompt Caching. 90% cost reduction
        - Self-Adaptive. Claude judges task complexity on its own
        - Unified Prompt. includes all reasoning modes

        Official docs. https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching

        Args:
            system_prompt: Pre-built system prompt (if None, build a new one)
        """
        # Unified self-adaptive system prompt (state excluded, included in user message)
        if system_prompt is None:
            system_prompt = self.prompt_builder.build_unified_prompt(
                interface=self.interface
            )
            if self._custom_system_prompt:
                system_prompt = self._custom_system_prompt + "\n\n---\n\n" + system_prompt

        # System prompt with caching (cached for 5 minutes)
        system_config = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"}  # Caching enabled
            }
        ]

        # Extended Thinking configuration (required)
        # Prompt engineering alone has limits. API parameters are needed.
        # System Prompt guides "when to use", API "enables" the capability.
        thinking_config = None
        if "claude-sonnet-4" in self.model or "claude-opus-4" in self.model:
            thinking_budget = min(10000, self.max_tokens - 1000)
            if thinking_budget > 1000:
                thinking_config = {
                    "type": "enabled",
                    "budget_tokens": thinking_budget
                }

        # Build API call parameters
        api_params = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_config,
            "tools": self.tools,
            "messages": self.conversation_history
        }

        # Extended Thinking is only added for supported models (Sonnet 4, Opus 4)
        if thinking_config is not None:
            api_params["thinking"] = thinking_config

        response = self.claude.messages.create(**api_params)

        return response

    def _call_groq(self, system_prompt: str = None):
        """Call Groq's OpenAI-compatible endpoint and match Claude's response shape."""
        if system_prompt is None:
            system_prompt = self.prompt_builder.build_unified_prompt(interface=self.interface)
            if self._custom_system_prompt:
                system_prompt = self._custom_system_prompt + "\n\n---\n\n" + system_prompt

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self._groq_messages_from_history())
        tools = [
            {"type": "function", "function": {
                "name": tool["name"], "description": tool["description"],
                "parameters": tool["input_schema"],
            }}
            for tool in self.tools
        ]
        params = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        completion = self.groq.chat.completions.create(**params)
        message = completion.choices[0].message
        content = []
        if message.content:
            content.append(SimpleNamespace(type="text", text=message.content))
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            content.append(SimpleNamespace(
                type="tool_use", id=call.id, name=call.function.name, input=arguments
            ))
        return SimpleNamespace(
            content=content,
            stop_reason="tool_use" if message.tool_calls else "end_turn",
            model=completion.model,
            usage=completion.usage,
        )

    def _groq_messages_from_history(self) -> List[Dict[str, Any]]:
        """Translate the project's Claude-style history into OpenAI messages."""
        messages = []
        for item in self.conversation_history:
            role, content = item["role"], item["content"]
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
                continue
            if role == "assistant":
                text = "\n".join(block.text for block in content if block.type == "text")
                tool_calls = []
                for block in content:
                    if block.type == "tool_use":
                        tool_calls.append({
                            "id": block.id, "type": "function",
                            "function": {"name": block.name, "arguments": json.dumps(block.input)}
                        })
                message = {"role": "assistant", "content": text or None}
                if tool_calls:
                    message["tool_calls"] = tool_calls
                messages.append(message)
            elif role == "user":
                for block in content:
                    if block["type"] == "tool_result":
                        messages.append({
                            "role": "tool", "tool_call_id": block["tool_use_id"],
                            "content": str(block["content"]),
                        })
        return messages

    async def _handle_tool_use(self, response, _depth: int = 0) -> str:
        """
        Handle tool use (official pattern).

        1. Execute tool
        2. Append result to conversation
        3. Send back to Claude
        """
        tool_results = []

        # Emit intermediate text only on first call (e.g., execution plan)
        # Skip on recursive calls (intermediate status messages)
        if _depth == 0:
            for content in response.content:
                if content.type == "text" and content.text.strip():
                    await self._emit_progress("__intermediate_text__", "text", content.text.strip())

        # Execute tool
        for content in response.content:
            if content.type == "tool_use":
                logger.info(f"  Tool execution. {content.name}")
                logger.debug(f"     Parameters. {content.input}")

                # Emit progress: tool started
                await self._emit_progress(content.name, "started", f"Executing {content.name}...")

                try:
                    # Execute via MCP Client (auto-selects appropriate client)
                    result = await self._route_tool_call(
                        content.name,
                        content.input
                    )

                    # Update state (simple version)
                    self._update_state(content.name, result)

                    # Activate SQLite MCP immediately after xml_to_sqlite_tool runs
                    if content.name == "xml_to_sqlite_tool":
                        await self._check_and_enable_sqlite_if_needed()


                    # Verify tool result and check for errors
                    result_content = str(result.content[0].text)

                    # Check SQLite tool error messages (Database error, Error. etc.)
                    sqlite_tool_names = ["read_query", "write_query", "create_table", "list_tables", "describe_table", "append_insight"]
                    if content.name in sqlite_tool_names:
                        # SQLite tools return errors in the form "Database error. ..." or "Error. ..."
                        if result_content.startswith("Database error:") or result_content.startswith("Error:"):
                            error_msg = result_content
                            logger.error(f"     SQLite tool error. {error_msg}")
                            # Pass the error message through clearly
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": content.id,
                                "content": error_msg,
                                "is_error": True
                            })
                        else:
                            # Success case
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": content.id,
                                "content": result_content
                            })
                            logger.info(f"     Completed")
                            await self._emit_progress(content.name, "completed", f"{content.name} completed successfully")
                    else:
                        # Generic tool. parse JSON and check status
                        try:
                            result_parsed = json.loads(result_content)
                            # Only check status for dicts. lists and other types are treated as success.
                            if isinstance(result_parsed, dict) and result_parsed.get("status") == "error":
                                error_msg = result_parsed.get("message", "Unknown error")
                                logger.error(f"     Tool error. {error_msg}")
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": content.id,
                                    "content": f"Error: {error_msg}",
                                    "is_error": True
                                })
                            else:
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": content.id,
                                    "content": result_content
                                })
                                logger.info(f"     Completed")
                                await self._emit_progress(content.name, "completed", f"{content.name} completed successfully")
                        except json.JSONDecodeError:
                            # If not JSON, pass through as is
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": content.id,
                                "content": result_content
                            })
                            logger.info(f"     Completed")
                            await self._emit_progress(content.name, "completed", f"{content.name} completed")

                except Exception as e:
                    logger.error(f"     Exception raised. {e}")
                    # Emit progress: tool error
                    await self._emit_progress(content.name, "error", str(e))

                    # Pass the error along as a result
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": content.id,
                        "content": f"Error: {str(e)}",
                        "is_error": True
                    })

        # Update conversation history
        self.conversation_history.append({
            "role": "assistant",
            "content": response.content  # Includes tool use
        })

        self.conversation_history.append({
            "role": "user",
            "content": tool_results  # Tool results
        })

        # Call Claude again with the results
        # If _call_claude() fails, history is corrupted and must be repaired
        try:
            final_response = await asyncio.to_thread(self._call_model)
        except Exception as e:
            # Claude API call failed. tool_use + tool_result are already in history, so
            # append an empty assistant response to keep history consistent
            logger.error(f"Claude API call after tool processing failed. {e}")
            error_text = f"Tool execution completed but an error occurred while generating the response. {str(e)}"
            self.conversation_history.append({
                "role": "assistant",
                "content": [{"type": "text", "text": error_text}]
            })
            raise

        # Debug. print raw response
        if self.debug:
            self._debug_print_response(final_response)

        # Tool use can chain
        if final_response.stop_reason == "tool_use":
            return await self._handle_tool_use(final_response, _depth=_depth + 1)

        # Final text response (handles all content blocks, including thinking)
        final_text = self._extract_text_from_response(final_response)

        self.conversation_history.append({
            "role": "assistant",
            "content": final_response.content
        })

        logger.info(f"AgentSUMO: {final_text[:100]}...")

        return final_text

    async def _route_tool_call(self, tool_name: str, tool_input: dict):
        """
        Route a tool call to the appropriate MCP client.

        Args:
            tool_name: Tool name
            tool_input: Tool arguments

        Returns:
            Tool execution result
        """
        # SQLite MCP tools
        sqlite_tool_names = ["read_query", "write_query", "create_table", "list_tables", "describe_table", "append_insight"]

        if tool_name in sqlite_tool_names:
            if not self.mcp_clients.get("sqlite") or not self.mcp_clients["sqlite"].is_connected:
                raise RuntimeError(
                    f"SQLite MCP is not connected. "
                    f"To use tool '{tool_name}', enable SQLite MCP first."
                )

            logger.debug(f"  -> Routing to SQLite MCP. {tool_name}")
            return await self.mcp_clients["sqlite"].call_tool(tool_name, tool_input)

        # Filesystem MCP tools
        # Mirror the 13 tools exposed by @modelcontextprotocol/server-filesystem
        # (https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem)
        filesystem_tool_names = [
            "read_text_file", "read_media_file", "read_multiple_files",
            "write_file", "edit_file", "create_directory",
            "list_directory", "list_directory_with_sizes", "directory_tree",
            "move_file", "search_files", "get_file_info",
            "list_allowed_directories",
        ]

        if tool_name in filesystem_tool_names:
            if not self.mcp_clients.get("filesystem") or not self.mcp_clients["filesystem"].is_connected:
                raise RuntimeError(
                    f"Filesystem MCP is not connected. "
                    f"To use tool '{tool_name}', enable Filesystem MCP first."
                )

            logger.debug(f"  -> Routing to Filesystem MCP. {tool_name}")
            return await self.mcp_clients["filesystem"].call_tool(tool_name, tool_input)

        # SUMO MCP tools
        else:
            logger.debug(f"  -> Routing to SUMO MCP. {tool_name}")
            return await self.mcp_clients["sumo"].call_tool(tool_name, tool_input)

    def _update_state(self, tool_name: str, result):
        """
        Update state (including SQLite support).
        """
        try:
            # Verify MCP response format
            if not result.content or len(result.content) == 0:
                logger.warning(f"{tool_name}: result.content is empty")
                return

            content_str = str(result.content[0].text)
            logger.debug(f"{tool_name} response content (first 200 chars). {content_str[:200]}")

            # SQLite tools that return list format (not dict) - skip state update
            sqlite_list_tools = ["read_query", "list_tables", "describe_table"]
            if tool_name in sqlite_list_tools:
                # These tools return list of dicts, not a single dict
                # State update is not needed for these tools
                logger.debug(f"   {tool_name} returns list format (state update not needed)")
                return

            # Try JSON parsing first
            try:
                result_dict = json.loads(content_str)
            except json.JSONDecodeError:
                # If JSON parsing fails, try ast.literal_eval for Python dict strings
                # This handles SQLite query results that return Python dict format
                try:
                    import ast
                    parsed = ast.literal_eval(content_str)
                    # Check if it's a list (SQLite query results)
                    if isinstance(parsed, list):
                        logger.debug(f"{tool_name}: returns list format (state update not needed)")
                        return
                    result_dict = parsed
                    logger.debug(f"{tool_name}: Python dict parse succeeded (ast.literal_eval)")
                except (ValueError, SyntaxError) as e:
                    # If both fail, log and skip state update (non-critical)
                    logger.warning(f"{tool_name}: JSON/Python dict parse failed - {e}")
                    logger.debug(f"   Original content. {content_str[:500]}")
                    return

            if tool_name == "net_convert":
                net_file = result_dict.get("net_file")
                logger.debug(f"net_convert result. {result_dict}")

                if not net_file:
                    logger.warning(f"net_convert: net_file is None. Full result. {result_dict}")
                    return

                self.simulation_state["current_network"] = net_file
                # Set baseline if not already set (first network generation)
                if not self.simulation_state.get("baseline_network"):
                    self.simulation_state["baseline_network"] = net_file
                    logger.info(f"Baseline network set: {net_file}")
                else:
                    logger.info(f"Current network updated: {net_file}")

            elif tool_name == "trip_generate":
                self.simulation_state["current_trips"] = result_dict.get("trip_file")

            elif tool_name == "route_generate":
                self.simulation_state["current_routes"] = result_dict.get("route_file")

            elif tool_name in ["reduce_lanes_tool", "edge_edit_tool", "speed_limit_edit_tool"]:
                # Policy tools update current_network (but keep baseline unchanged)
                modified_net = result_dict.get("net_file")
                if modified_net:
                    self.simulation_state["current_network"] = modified_net
                    logger.info(f"Policy applied: current_network = {modified_net}")
                    logger.info(f"Baseline remains: {self.simulation_state.get('baseline_network')}")

            elif tool_name == "sumo_runner":
                self.simulation_state["last_results"] = result_dict.get("output_files")

            elif tool_name == "xml_to_sqlite_tool":
                # When SQLite DB is created
                db_file = result_dict.get("db_file")
                simulation_id = result_dict.get("simulation_id")
                metadata = result_dict.get("metadata", {})

                self.simulation_state["current_db"] = db_file

                # Update simulation list
                sim_info = {
                    "simulation_id": simulation_id,
                    "trip_count": metadata.get("trip_count", 0)
                }

                # Remove duplicates then append
                self.simulation_state["simulations"] = [
                    s for s in self.simulation_state.get("simulations", [])
                    if s.get("simulation_id") != simulation_id
                ]
                self.simulation_state["simulations"].append(sim_info)

                logger.info(f"State update. SQLite DB = {db_file}")
                logger.info(f"   {len(self.simulation_state['simulations'])} simulations in current DB")

        except Exception as e:
            logger.debug(f"State update failed (ignored). {e}")

        # B: Save state to disk after every update
        self._save_state()

    # ============================================
    # State Persistence (B: JSON save/load)
    # ============================================

    def _get_state_file_path(self):
        """Get path to state persistence file"""
        from agentsumo_mcp.utils.path_utils import get_output_path
        return get_output_path("output") / ".agentsumo_state.json"

    def _save_state(self):
        """Save simulation_state to disk (called after every _update_state)"""
        try:
            state_file = self._get_state_file_path()
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(state_file, 'w') as f:
                json.dump(self.simulation_state, f, indent=2, default=str)
            logger.debug(f"State saved to {state_file}")
        except Exception as e:
            logger.debug(f"State save failed (non-critical): {e}")

    def _restore_state(self):
        """
        Restore simulation_state on startup.
        Priority: B (JSON file) -> A (file scan fallback) -> empty state
        """
        # B: Try loading from JSON file
        try:
            state_file = self._get_state_file_path()
            if state_file.exists() and state_file.stat().st_size > 0:
                with open(state_file, 'r') as f:
                    saved = json.load(f)

                # Validate: check that referenced files still exist
                from pathlib import Path as P
                valid = True
                for key in ["current_network", "baseline_network", "current_routes", "current_db"]:
                    path = saved.get(key)
                    if path and not P(path).exists():
                        logger.warning(f"State restore: {key}={path} no longer exists, discarding")
                        saved[key] = None
                        valid = False

                self.simulation_state.update(saved)
                logger.info(f"State restored from {state_file.name}")
                if self.simulation_state.get("current_network"):
                    logger.info(f"   Network: {self.simulation_state['current_network']}")
                if self.simulation_state.get("current_db"):
                    logger.info(f"   DB: {self.simulation_state['current_db']}")
                return
        except Exception as e:
            logger.debug(f"State JSON restore failed: {e}")

        # A: Fallback - scan output directories
        logger.info("State JSON not found, scanning output files...")
        try:
            from agentsumo_mcp.utils.path_utils import get_output_path
            from pathlib import Path as P

            # Scan networks
            networks_dir = get_output_path("output/networks")
            if networks_dir.exists():
                net_files = sorted(networks_dir.glob("*.net.xml"), key=lambda p: p.stat().st_mtime)
                if net_files:
                    # Oldest = baseline, newest = current
                    self.simulation_state["baseline_network"] = str(net_files[0])
                    self.simulation_state["current_network"] = str(net_files[-1])
                    logger.info(f"   Found network: {net_files[-1].name}")

            # Scan routes
            trips_dir = get_output_path("output/trips")
            if trips_dir.exists():
                rou_files = sorted(trips_dir.glob("*.rou.xml"), key=lambda p: p.stat().st_mtime)
                if rou_files:
                    self.simulation_state["current_routes"] = str(rou_files[-1])
                    logger.info(f"   Found routes: {rou_files[-1].name}")

            # Scan simulations (last_results)
            sims_dir = get_output_path("output/simulations")
            if sims_dir.exists():
                tripinfo_files = sorted(sims_dir.glob("*_tripinfo_*.xml"), key=lambda p: p.stat().st_mtime)
                if tripinfo_files:
                    latest = tripinfo_files[-1]
                    base = latest.name.replace("_tripinfo_", "_netstate_").replace("_tripinfo_", "_netstate_")
                    # Try to find matching edgedata files
                    results = [str(latest)]
                    netstate = sims_dir / latest.name.replace("_tripinfo_", "_netstate_")
                    emission = sims_dir / latest.name.replace("_tripinfo_", "_netstate_emission_")
                    if netstate.exists():
                        results.append(str(netstate))
                    if emission.exists():
                        results.append(str(emission))
                    self.simulation_state["last_results"] = results
                    logger.info(f"   Found simulation results: {latest.name}")

            # DB is already auto-detected in start() - skip here

            if self.simulation_state.get("current_network"):
                logger.info("State recovered from file scan")
                self._save_state()  # Save the recovered state
            else:
                logger.info("   No existing files found, starting fresh")

        except Exception as e:
            logger.debug(f"File scan fallback failed: {e}")

    def _debug_print_response(self, response):
        """
        Debug mode. print Claude raw response.

        Args:
            response: Claude API response
        """
        print("\n" + "="*70)
        print("DEBUG: Claude Raw Response")
        print("="*70)

        print(f"\nResponse Info:")
        print(f"  - Model: {response.model}")
        print(f"  - Stop Reason: {response.stop_reason}")
        print(f"  - Usage: {response.usage}")

        print(f"\nContent Blocks ({len(response.content)}):")
        for i, block in enumerate(response.content, 1):
            print(f"\n  Block {i}:")
            print(f"    Type: {block.type if hasattr(block, 'type') else 'unknown'}")

            if hasattr(block, 'type'):
                if block.type == "text":
                    preview = block.text[:200] if len(block.text) > 200 else block.text
                    print(f"    Text: {preview}...")

                elif block.type == "thinking":
                    print(f"    Thinking: {block.thinking}")

                elif block.type == "tool_use":
                    print(f"    Tool: {block.name}")
                    print(f"    Args: {block.input}")

        print("\n" + "="*70 + "\n")

    def _extract_text_from_response(self, response) -> str:
        """
        Extract text from Claude response (excluding thinking blocks).

        A Claude response can consist of multiple content blocks.
        - text blocks. plain response
        - thinking blocks. <thinking> or <extended_thinking> (internal reasoning, not shown to user)

        Args:
            response: Claude API response

        Returns:
            Response string to show to the user (excluding thinking blocks)
        """
        parts = []

        for block in response.content:
            if hasattr(block, 'type'):
                if block.type == "text":
                    # Append plain text only
                    parts.append(block.text)

                # Exclude thinking blocks (internal reasoning, not shown to user)
                # elif block.type == "thinking":
                #     pass

        # Combine text blocks only
        full_text = "\n\n".join(parts)

        # Strip any <thinking> or <extended_thinking> tags inside the text
        full_text = re.sub(r'<thinking>.*?</thinking>', '', full_text, flags=re.DOTALL)
        full_text = re.sub(r'<extended_thinking>.*?</extended_thinking>', '', full_text, flags=re.DOTALL)

        return full_text.strip()

    # The previous _get_system_prompt() and _get_context_summary() have been
    # replaced by PromptBuilder.

    def get_state(self) -> Dict[str, Any]:
        """Get current state"""
        return {
            "simulation_state": self.simulation_state,
            "conversation_turns": len(self.conversation_history) // 2,
            "tools_available": len(self.tools),
            "model": self.model
        }

    def reset_conversation(self):
        """Reset only the conversation history (state preserved)"""
        self.conversation_history = []
        logger.info("Conversation history reset")

    async def _check_and_enable_sqlite_if_needed(self):
        """
        Activate SQLite MCP. Connect if DB exists and MCP is not connected.
        """
        current_db = self.simulation_state.get("current_db")

        # Already connected? Skip.
        if self.mcp_clients.get("sqlite") and self.mcp_clients["sqlite"].is_connected:
            self.simulation_state["sqlite_enabled"] = True
            return

        # DB exists and not yet activated -> first activation
        if current_db:
            logger.info(f"First SQLite MCP activation in progress... (DB. {current_db})")

            try:
                from agentsumo.client.sqlite_mcp_client import SQLiteMCPClient

                # Connect to new DB (first activation, not reconnect)
                self.mcp_clients["sqlite"] = SQLiteMCPClient(current_db)
                await self.mcp_clients["sqlite"].__aenter__()

                # Load and add tools
                sqlite_tools = await self.mcp_clients["sqlite"].list_tools()
                new_claude_tools = self._convert_mcp_to_claude_tools(sqlite_tools)
                self.tools.extend(new_claude_tools)

                # Update state
                self.simulation_state["sqlite_enabled"] = True

                logger.info(f"SQLite MCP activation complete ({len(sqlite_tools)} tools added)")
                logger.info(f"   -> Detailed SQL queries are now available.")

            except Exception as e:
                logger.error(f"SQLite MCP activation failed. {e}")
                import traceback
                logger.debug(traceback.format_exc())
                # Continue even on failure (graceful degradation)

    def reset_all(self):
        """Reset all state"""
        self.conversation_history = []
        self.simulation_state = {
            "current_network": None,
            "baseline_network": None,
            "current_routes": None,
            "last_results": None,
            "current_db": None,
            "sqlite_enabled": False,
            "simulations": []
        }
        # Delete saved state file
        try:
            state_file = self._get_state_file_path()
            if state_file.exists():
                state_file.unlink()
                logger.info("State file deleted")
        except Exception:
            pass
        logger.info("All state reset")

