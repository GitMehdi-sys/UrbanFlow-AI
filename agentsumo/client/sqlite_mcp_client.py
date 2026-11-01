"""
SQLite MCP Client for AgentSUMO

Connects to Anthropic's official SQLite MCP Server for SQL-based analysis.

Usage:
    async with SQLiteMCPClient(db_path="simulation.db") as client:
        tools = await client.list_tools()
        result = await client.call_tool("read_query", {"query": "SELECT ..."})
"""

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from typing import Any, Optional, List
from pathlib import Path
import asyncio
import sys
import importlib.util
import logging

from agentsumo.core.config import AgentSUMOConfig

logger = logging.getLogger("agentsumo.sqlite_mcp_client")


class SQLiteMCPClient:
    """
    Client that communicates with the SQLite MCP Server.

    Uses the Python-based SQLite MCP Server.
    uv run mcp-server-sqlite --db-path <path>

    Note. Uses Anthropic's official MCP server example.
    Original repo - https://github.com/modelcontextprotocol/servers-archived/tree/main/src/sqlite
    (included inside the project as the sqlite-mcp-server folder)

    Features:
    - SQL query execution (read_query / write_query / create_table)
    - Schema inspection (list_tables / describe_table)
    - Analytical memo recording (append_insight)
    - Multi-simulation support (simulation_id based)
    """
    
    def __init__(
        self,
        db_path: str,
        connection_timeout: float = 10.0,
        tool_timeout: float = 30.0
    ):
        """
        Initialize SQLite MCP Client.
        
        Args:
            db_path: SQLite database file path (absolute or relative)
            connection_timeout: Connection timeout in seconds
            tool_timeout: Tool execution timeout in seconds
        """
        self.db_path = db_path
        self.connection_timeout = connection_timeout
        self.tool_timeout = tool_timeout
        
        self.session: Optional[ClientSession] = None
        self._stdio_context_manager = None
        self._session_context_manager = None
        self._connected = False
        
        logger.info(f"SQLiteMCPClient initialized (DB - {db_path})")
    
    async def __aenter__(self):
        """Context manager entry - Connect to server"""
        await self._connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - Disconnect"""
        await self._disconnect()
        
        if exc_type is not None:
            logger.error(f"SQLiteMCPClient context error: {exc_type.__name__}: {exc_val}")
        
        return False
    
    async def _connect(self):
        """Connect to Anthropic's official SQLite MCP Server"""
        if self._connected:
            logger.warning("Already connected to SQLite MCP Server")
            return
        
        logger.info("Connecting to SQLite MCP Server")

        if importlib.util.find_spec('mcp_server_sqlite') is None:
            raise RuntimeError('SQLite assistant tools require mcp-server-sqlite. Install the project dependencies in the active Python environment.')
        server_params = StdioServerParameters(
            command=sys.executable,
            args=['-c', 'from mcp_server_sqlite import main; main()', '--db-path', self.db_path],
            env=None
        )

        try:
            # STDIO connection
            self._stdio_context_manager = stdio_client(server_params)
            read, write = await asyncio.wait_for(
                self._stdio_context_manager.__aenter__(),
                timeout=self.connection_timeout / 2
            )
            
            # Session
            self._session_context_manager = ClientSession(read, write)
            self.session = await self._session_context_manager.__aenter__()
            
            # Initialize handshake
            await asyncio.wait_for(
                self.session.initialize(),
                timeout=self.connection_timeout / 2
            )
            
            self._connected = True
            logger.info("SQLite MCP Server connection established")

        except asyncio.TimeoutError:
            raise RuntimeError(f"SQLite MCP Server connection timeout ({self.connection_timeout}s)")
        except Exception as e:
            raise RuntimeError(f"SQLite MCP Server connection failed - {e}")

    async def _disconnect(self):
        """Disconnect from server (same approach as SUMO MCP Client)"""
        if not self._connected:
            return

        logger.info("Closing SQLite MCP Server connection")

        try:
            # Close session
            if self._session_context_manager:
                await self._session_context_manager.__aexit__(None, None, None)

            # Close STDIO
            if self._stdio_context_manager:
                await self._stdio_context_manager.__aexit__(None, None, None)

            self._connected = False
            logger.info("SQLite MCP connection closed")

        except Exception as e:
            logger.warning(f"Error while closing connection (ignored) - {e}")
            self._connected = False
    
    async def list_tools(self) -> List[Any]:
        """
        List available SQLite tools.
        
        Returns:
            List of tools (read_query, write_query, create_table, list_tables, describe_table, append_insight)
        
        Raises:
            RuntimeError: If not connected
        """
        if not self._connected or not self.session:
            raise RuntimeError("Not connected to SQLite MCP Server")
        
        try:
            logger.debug("Listing SQLite tools...")
            result = await self.session.list_tools()
            logger.info(f"{len(result.tools)} SQLite tools available")
            return result.tools
            
        except Exception as e:
            logger.error(f"Failed to list tools: {e}")
            raise RuntimeError(f"Failed to list SQLite tools: {e}")
    
    async def call_tool(
        self,
        name: str,
        arguments: dict,
        timeout: Optional[float] = None
    ) -> Any:
        """
        Execute a SQLite tool.
        
        Args:
            name: Tool name (e.g., "read_query", "list_tables")
            arguments: Tool arguments (e.g., {"query": "SELECT ..."})
            timeout: Execution timeout (None = use default)
        
        Returns:
            Tool execution result
        
        Raises:
            RuntimeError: If not connected
            asyncio.TimeoutError: If timeout
        """
        if not self._connected or not self.session:
            raise RuntimeError("Not connected to SQLite MCP Server")
        
        timeout = timeout or self.tool_timeout
        
        try:
            logger.debug(f"Calling SQLite tool - {name}")

            # Log query for debugging
            if "query" in arguments:
                query = arguments["query"]
                logger.debug(f"  SQL - {query[:100]}...")

            # Execute with timeout
            result = await asyncio.wait_for(
                self.session.call_tool(name, arguments),
                timeout=timeout
            )

            logger.debug(f"SQLite tool completed - {name}")
            return result

        except asyncio.TimeoutError:
            error_msg = f"SQLite tool timeout - {name} (exceeded {timeout}s)"
            logger.error(error_msg)
            raise asyncio.TimeoutError(error_msg)

        except Exception as e:
            error_msg = f"SQLite tool execution failed - {name} - {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
    
    @property
    def is_connected(self) -> bool:
        """Check connection status"""
        return self._connected
    
    def get_stats(self) -> dict:
        """Get client statistics"""
        return {
            "connected": self._connected,
            "db_path": self.db_path,
            "connection_timeout": self.connection_timeout,
            "tool_timeout": self.tool_timeout
        }

