"""SUMO services. Importing a utility does not initialize the MCP server."""
__version__ = '0.1.0'

def __getattr__(name):
    if name == 'mcp':
        from .main import mcp
        return mcp
    raise AttributeError(name)
