"""
AgentSUMO configuration.

Centralizes environment-variable lookup for API keys and runtime paths.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is optional. If it is not installed, callers must set
    # environment variables (ANTHROPIC_API_KEY, MAPBOX_TOKEN, SUMO_HOME)
    # directly in the shell.
    pass


class AgentSUMOConfig:
    """Global AgentSUMO configuration."""

    # Project root
    PROJECT_ROOT = Path(__file__).parent.parent.parent

    # Source checkout by default; installed wheels use the invoking workspace.
    WORKSPACE_ROOT = Path(os.environ.get("AGENTSUMO_MCP_OUTPUT_BASE", str(PROJECT_ROOT if (PROJECT_ROOT / 'pyproject.toml').exists() else Path.cwd()))).resolve()
    os.environ.setdefault("AGENTSUMO_MCP_OUTPUT_BASE", str(WORKSPACE_ROOT))
    DATA_DIR = WORKSPACE_ROOT / "data"
    OUTPUT_DIR = WORKSPACE_ROOT / "output"
    CHECKPOINTS_DIR = WORKSPACE_ROOT / "checkpoints"

    # Legacy token files (still honored as a fallback for existing setups
    # that predate the .env-based workflow). New installations should set
    # ANTHROPIC_API_KEY and MAPBOX_TOKEN via environment variables or .env.
    CLAUDE_API_KEY_FILE = PROJECT_ROOT / "claude_api.txt"
    MAPBOX_TOKEN_FILE = PROJECT_ROOT / "mapbox_token.txt"

    # Default Claude model
    DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
    # A tool-capable model listed on Groq's free-tier rate-limit page.
    DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"

    @classmethod
    def get_llm_provider(cls) -> str:
        """Return the configured reasoning provider (``anthropic`` or ``groq``)."""
        provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
        if provider not in {"anthropic", "groq"}:
            raise RuntimeError("LLM_PROVIDER must be either 'anthropic' or 'groq'.")
        return provider

    @classmethod
    def get_groq_api_key(cls) -> str:
        """Resolve the Groq API key when ``LLM_PROVIDER=groq``."""
        key = os.environ.get("GROQ_API_KEY")
        if key:
            return key
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add it to .env after creating a key at "
            "https://console.groq.com/keys"
        )

    @classmethod
    def get_claude_api_key(cls) -> str:
        """Resolve the Claude API key.

        Resolution order:
          1. ANTHROPIC_API_KEY environment variable (or .env file).
          2. Legacy ``claude_api.txt`` file at the project root.
        """
        env_key = os.environ.get("ANTHROPIC_API_KEY")
        if env_key:
            return env_key

        if cls.CLAUDE_API_KEY_FILE.exists():
            with open(cls.CLAUDE_API_KEY_FILE, "r") as f:
                key = f.read().strip()
                if key:
                    import warnings
                    warnings.warn(
                        "Reading the Claude API key from claude_api.txt is "
                        "deprecated and will be removed in 0.2.0. Move the "
                        "key into ANTHROPIC_API_KEY in your .env instead.",
                        DeprecationWarning,
                        stacklevel=2,
                    )
                    return key

        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add "
            "your Claude API key, or export ANTHROPIC_API_KEY in your shell. "
            "Get a key at https://console.anthropic.com/settings/keys"
        )

    @classmethod
    def get_mapbox_token(cls) -> str:
        """Resolve the Mapbox access token.

        Resolution order:
          1. MAPBOX_TOKEN or MAPBOX_ACCESS_TOKEN environment variable.
          2. Legacy ``mapbox_token.txt`` file at the project root.
        """
        env_token = os.environ.get("MAPBOX_TOKEN") or os.environ.get("MAPBOX_ACCESS_TOKEN")
        if env_token:
            if not env_token.startswith('pk.'):
                raise RuntimeError('Use a public Mapbox token (pk.), never a private token in the browser.')
            return env_token

        if cls.MAPBOX_TOKEN_FILE.exists():
            with open(cls.MAPBOX_TOKEN_FILE, "r") as f:
                token = f.read().strip()
                if token:
                    if not token.startswith('pk.'):
                        raise RuntimeError('Use a public Mapbox token (pk.), never a private token in the browser.')
                    import warnings
                    warnings.warn(
                        "Reading the Mapbox token from mapbox_token.txt is "
                        "deprecated and will be removed in 0.2.0. Move the "
                        "token into MAPBOX_TOKEN in your .env instead.",
                        DeprecationWarning,
                        stacklevel=2,
                    )
                    return token

        raise RuntimeError(
            "MAPBOX_TOKEN is not set. Copy .env.example to .env and add your "
            "Mapbox access token, or export MAPBOX_TOKEN in your shell. "
            "Get a token at https://account.mapbox.com/access-tokens/"
        )

    @classmethod
    def ensure_directories(cls):
        """Create the output directory tree if it does not yet exist."""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        for sub in ("networks", "trips", "simulations", "analysis", "reports", "visualizations"):
            (cls.OUTPUT_DIR / sub).mkdir(exist_ok=True)
