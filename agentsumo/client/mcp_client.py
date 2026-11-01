"""
MCP Client for AgentSUMO
"""

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from typing import List, Dict, Any, Optional
from pathlib import Path
from agentsumo.core import AgentSUMOConfig
import asyncio
import sys
import logging
from datetime import datetime


# Logger setup
logger = logging.getLogger("agentsumo.mcp_client")


class MCPClientError(Exception):
    """MCP Client related error"""
    pass


class MCPConnectionError(MCPClientError):
    """MCP connection error"""
    pass


class MCPToolCallError(MCPClientError):
    """MCP Tool invocation error"""
    pass


class SUMOMCPClient:
    """
    Client that communicates with the SUMO MCP Server.

    Features:
    - Timeout configuration
    - Automatic reconnection
    - Detailed error handling
    - Logging
    - Health check

    Usage:
        async with SUMOMCPClient(
            connection_timeout=10.0,
            tool_timeout=300.0,
            max_retries=3
        ) as client:
            tools = await client.list_tools()
            result = await client.call_tool("net_generate", {...})
    """
    
    def __init__(
        self,
        python_path: str = None,
        connection_timeout: float = 10.0,
        tool_timeout: float = 300.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        enable_logging: bool = True
    ):
        """
        Args:
            python_path: Path to the Python executable
            connection_timeout: Connection timeout in seconds
            tool_timeout: Tool execution timeout in seconds
            max_retries: Maximum number of retries
            retry_delay: Delay between retries in seconds
            enable_logging: Whether to enable logging
        """
        self.python_path = python_path or sys.executable
        self.connection_timeout = connection_timeout
        self.tool_timeout = tool_timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        # Logging configuration
        if enable_logging:
            logging.basicConfig(
                level=logging.INFO,
                format='%(asctime)s [%(levelname)s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )

        # State management
        self.session: Optional[ClientSession] = None
        self._stdio_context_manager = None
        self._session_context_manager = None
        self._connected = False
        self._connection_attempts = 0
        self._last_error = None
        
        # This repository bundles its MCP execution server. Keep the launch
        # target explicit so local development and deployed copies behave alike.
        import importlib.util

        def _has_module(name: str) -> bool:
            try:
                return importlib.util.find_spec(name) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                return False

        if _has_module("agentsumo_mcp.main"):
            self.server_module = "agentsumo_mcp.main"
        else:
            raise ModuleNotFoundError(
                "The bundled agentsumo_mcp.main server is not importable. "
                "Run 'uv sync' to install the project dependencies."
            )

        logger.info(f"SUMOMCPClient initialized (server module: {self.server_module})")
    
    async def __aenter__(self):
        """Context manager entry"""
        await self._connect_with_retry()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        await self._disconnect()

        # Log if an exception occurred
        if exc_type is not None:
            logger.error(f"Exception in context manager - {exc_type.__name__} - {exc_val}")

        return False  # Re-raise the exception

    async def _connect_with_retry(self) -> None:
        """Connection with retry logic"""
        for attempt in range(self.max_retries):
            try:
                self._connection_attempts = attempt + 1

                if attempt > 0:
                    logger.info(f"Reconnect attempt {attempt + 1}/{self.max_retries}")
                    await asyncio.sleep(self.retry_delay)

                await self._connect()
                logger.info(f"Connected successfully on attempt {self._connection_attempts}")
                return

            except asyncio.TimeoutError:
                self._last_error = f"Connection timeout ({self.connection_timeout}s)"
                logger.warning(f"Attempt {attempt + 1} failed - {self._last_error}")

            except Exception as e:
                self._last_error = str(e)
                logger.error(f"Attempt {attempt + 1} failed - {type(e).__name__} - {e}")

        # All retries failed
        raise MCPConnectionError(
            f"MCP Server connection failed after {self.max_retries} attempts. "
            f"Last error - {self._last_error}"
        )

    async def _connect(self) -> None:
        """Actual connection logic (single attempt)"""
        if self._connected:
            logger.warning("Already connected.")
            return

        logger.info("Connecting to MCP Server")

        # Server parameters (run module via python -m)
        # Pass environment variables (ANTHROPIC_API_KEY etc.)
        import os
        env = os.environ.copy()

        server_params = StdioServerParameters(
            command=self.python_path,
            args=["-m", self.server_module],
            env=env,  # Pass environment variables
            cwd=str(AgentSUMOConfig.PROJECT_ROOT)
        )

        # STDIO context manager (with timeout)
        self._stdio_context_manager = stdio_client(server_params)
        read, write = await asyncio.wait_for(
            self._stdio_context_manager.__aenter__(),
            timeout=self.connection_timeout / 2
        )

        # Session context manager
        self._session_context_manager = ClientSession(read, write)
        self.session = await self._session_context_manager.__aenter__()

        # Initialization handshake (with timeout)
        await asyncio.wait_for(
            self.session.initialize(),
            timeout=self.connection_timeout / 2
        )

        self._connected = True
        logger.info("MCP Server connection established")

    async def _disconnect(self) -> None:
        """Close connection"""
        if not self._connected:
            return

        logger.info("Closing MCP Server connection")

        try:
            # Close session
            if self._session_context_manager:
                await self._session_context_manager.__aexit__(None, None, None)

            # Close STDIO
            if self._stdio_context_manager:
                await self._stdio_context_manager.__aexit__(None, None, None)

            self._connected = False
            logger.info("Connection closed")

        except Exception as e:
            logger.error(f"Error while closing connection - {e}")
            self._connected = False
    
    async def list_tools(self) -> List[Any]:
        """
        Get the list of available tools.

        Returns:
            List[Tool]: List of tools

        Raises:
            MCPConnectionError: If not connected
            MCPClientError: If listing tools fails
        """
        if not self._connected or not self.session:
            raise MCPConnectionError(
                "Not connected to MCP Server. Use the 'async with' construct."
            )

        try:
            logger.debug("Listing tools")
            result = await self.session.list_tools()
            logger.info(f"Found {len(result.tools)} tools")
            return result.tools

        except Exception as e:
            logger.error(f"Failed to list tools - {e}")
            raise MCPClientError(f"Failed to list tools - {e}") from e

    async def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        timeout: Optional[float] = None
    ) -> Any:
        """
        Execute a tool (with timeout and error handling).

        Args:
            name: Tool name
            arguments: Tool arguments
            timeout: Execution timeout (None uses the default)

        Returns:
            Tool execution result

        Raises:
            MCPConnectionError: If not connected
            MCPToolCallError: If tool execution fails
            asyncio.TimeoutError: On timeout
        """
        if not self._connected or not self.session:
            raise MCPConnectionError(
                "Not connected to MCP Server. Use the 'async with' construct."
            )

        timeout = timeout or self.tool_timeout

        try:
            logger.debug(f"Calling tool - {name}")
            logger.debug(f"  parameters - {arguments}")

            # Apply timeout
            result = await asyncio.wait_for(
                self.session.call_tool(name, arguments),
                timeout=timeout
            )

            logger.debug(f"Tool completed - {name}")
            return result

        except asyncio.TimeoutError:
            error_msg = f"Tool execution timeout - {name} (exceeded {timeout}s)"
            logger.error(error_msg)
            raise asyncio.TimeoutError(error_msg)

        except Exception as e:
            error_msg = f"Tool execution failed - {name} - {type(e).__name__} - {e}"
            logger.error(error_msg)
            raise MCPToolCallError(error_msg) from e

    async def health_check(self) -> bool:
        """
        Check server status.

        Returns:
            bool: True if the server is healthy
        """
        try:
            if not self._connected:
                return False

            # Simple health check via list_tools
            tools = await asyncio.wait_for(
                self.list_tools(),
                timeout=5.0
            )

            logger.debug(f"Health check passed ({len(tools)} tools)")
            return True

        except Exception as e:
            logger.warning(f"Health check failed - {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """
        Client statistics.

        Returns:
            Dict: Statistics
        """
        return {
            "connected": self._connected,
            "connection_attempts": self._connection_attempts,
            "last_error": self._last_error,
            "server_file": str(self.server_file),
            "connection_timeout": self.connection_timeout,
            "tool_timeout": self.tool_timeout,
            "max_retries": self.max_retries
        }
    
    @property
    def is_connected(self) -> bool:
        """Check connection status"""
        return self._connected


# =============== Utility Functions ===============

async def create_mcp_client(
    connection_timeout: float = 10.0,
    tool_timeout: float = 300.0,
    max_retries: int = 3
) -> SUMOMCPClient:
    """
    Create an MCP Client and return it as a context manager.

    Usage:
        async with await create_mcp_client() as client:
            ...

    Or more simply:
        async with SUMOMCPClient() as client:
            ...

    Args:
        connection_timeout: Connection timeout in seconds
        tool_timeout: Tool execution timeout in seconds
        max_retries: Maximum number of retries

    Returns:
        SUMOMCPClient: MCP Client instance
    """
    return SUMOMCPClient(
        connection_timeout=connection_timeout,
        tool_timeout=tool_timeout,
        max_retries=max_retries
    )


async def test_mcp_connection() -> bool:
    """
    Quick MCP connection test.

    Returns:
        bool: True on successful connection
    """
    try:
        async with SUMOMCPClient(connection_timeout=5.0) as client:
            tools = await client.list_tools()
            print(f"MCP connection test passed ({len(tools)} tools)")
            return True
    except Exception as e:
        print(f"MCP connection test failed - {e}")
        return False
