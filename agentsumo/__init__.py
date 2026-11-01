"""
AgentSUMO - AI-Powered SUMO Traffic Simulation Platform

Conversational traffic simulation automation platform built on Claude and MCP.
"""

__version__ = "0.1.0"
__author__ = "AgentSUMO Team"

# Keep map and local scenario startup independent of optional AI client imports.
def __getattr__(name):
    if name == "AgentSUMO":
        from .agent import AgentSUMO
        return AgentSUMO
    if name == "SUMOMCPClient":
        from .client import SUMOMCPClient
        return SUMOMCPClient
    if name == "AgentSUMOConfig":
        from .core import AgentSUMOConfig
        return AgentSUMOConfig
    raise AttributeError(name)

__all__ = [
    "AgentSUMO",
    "SUMOMCPClient",
    "AgentSUMOConfig",
]

