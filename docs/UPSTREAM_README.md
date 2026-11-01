<div align="center">

# AgentSUMO

**An Agentic Framework for Interactive Simulation Scenario Generation in SUMO via Large Language Models**

[![PyPI](https://img.shields.io/pypi/v/agentsumo-mcp.svg)](https://pypi.org/project/agentsumo-mcp/)
[![tests](https://github.com/mw-jeong/AgentSUMO/actions/workflows/tests.yml/badge.svg)](https://github.com/mw-jeong/AgentSUMO/actions/workflows/tests.yml)
[![arXiv](https://img.shields.io/badge/arXiv-2511.06804-b31b1b.svg)](https://arxiv.org/abs/2511.06804)
[![Docs](https://readthedocs.org/projects/agentsumo/badge/?version=latest)](https://agentsumo.readthedocs.io/en/latest/?badge=latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-agentsumo--mcp-purple.svg)](https://registry.modelcontextprotocol.io/v0/servers?search=agentsumo)

<img src="assets/hero_overview.png" alt="AgentSUMO overview" width="850"/>

**[Documentation](https://agentsumo.readthedocs.io)** ·
[Installation](#installation) ·
[Tools](https://agentsumo.readthedocs.io/en/latest/mcp_servers/agentsumo/index.html) ·
[Schema](https://agentsumo.readthedocs.io/en/latest/database/index.html) ·
[Tutorials](https://agentsumo.readthedocs.io/en/latest/tutorials/index.html)

</div>

---

## Overview

**AgentSUMO** lets non-expert stakeholders design, execute, and analyze [SUMO](https://eclipse.dev/sumo/) traffic simulations through natural-language interaction. The Planner Agent translates abstract policy questions into executable simulation plans, drives them via the Model Context Protocol (MCP), and surfaces results through a web dashboard.

- **Conversational scenario design** — describe a policy question, get a runnable simulation
- **Policy experiments** — road closures, lane reductions, signal optimization, demand changes
- **Cross-scenario analysis** — SQL-based comparison across runs, with auto-generated HTML reports
- **Web dashboard** — geospatial visualization, time-series charts, and trip replay

## Demo

<div align="center">

<img src="assets/demo_web_interface.png" alt="Web interface" width="800"/>

*Web interface: conversational planning panel, scenario list, and live simulation status.*

<br/><br/>

<img src="assets/demo_geospatial.png" alt="Geospatial visualization" width="800"/>

*Geospatial visualization: per-edge metrics, congestion overlays, and trip replay on the 2.5D basemap.*

</div>

## Architecture

```
User (natural language)
    |
    v
Planner Agent (Claude LLM, Interactive Planning Protocol)
    |
    +--> AgentSUMO MCP Client --> AgentSUMO MCP Server (PyPI: agentsumo-mcp) --> SUMO
    |
    +--> SQLite MCP Client    --> SQLite MCP Server (Anthropic, open source)  --> simulations.db
    |
    +--> Filesystem MCP Client --> Filesystem MCP Server (Anthropic, open source) --> additional XML files
```

The reasoning layer (Planner Agent) lives in this repository. The execution layer (`agentsumo-mcp`) is published to PyPI and installed automatically as a dependency.

## Tool Layer

The AgentSUMO MCP Server exposes **26 tools** grouped into five capability categories that follow the simulation workflow. Full reference at [agentsumo.readthedocs.io/.../tools](https://agentsumo.readthedocs.io/en/latest/mcp_servers/agentsumo/index.html).

| Category | Purpose | Representative tools |
|---|---|---|
| **Scenario Generation** | Build a baseline SUMO simulation: OSM → network → trips → routes → run | `osm_extract`, `net_convert`, `trip_generate`, `route_generate`, `sumo_runner` |
| **Policy Experimentation** | Apply infrastructure, demand, and signal-control interventions | `edge_edit_tool`, `reduce_lanes_tool`, `vehicle_generation_tool`, `flow_generation_tool`, `tls_offset_tool`, `tls_adaptation_tool` |
| **Result Analysis** | Convert SUMO XML output to SQLite and render HTML reports | `xml_to_sqlite_tool`, `simulation_report_tool` |
| **Visualization** | Render networks, highlighted edges, and per-edge metric heatmaps | `visualize_net_tool`, `visualize_edge_tool`, `visualize_policy_target_tool`, `visualize_edgedata_tool` |
| **Utility Functions** | Network statistics, routing, road-name ↔ edge-id resolution, OD-coordinate validation, web-search grounding | `network_summary_tool`, `route_analysis_tool`, `validate_od_coordinates_tool`, `web_search_tool` |

## Installation

### Requirements

- Python 3.10 or later
- [SUMO](https://eclipse.dev/sumo/) 1.24 or later (locally installed, with `SUMO_HOME` set)
- Anthropic Claude API key (bring-your-own-key)
- Mapbox access token (used by the web map renderer)

### 1. Install SUMO

**macOS**
```bash
brew install sumo
```
Or download the installer from the [Eclipse SUMO downloads page](https://sumo.dlr.de/docs/Downloads.php).

**Windows** — Download the installer from the [Eclipse SUMO downloads page](https://sumo.dlr.de/docs/Downloads.php).

**Linux (Ubuntu/Debian)**
```bash
sudo add-apt-repository ppa:sumo/stable
sudo apt-get update
sudo apt-get install sumo sumo-tools sumo-doc
```

### 2. Set up the Python environment

Install [`uv`](https://docs.astral.sh/uv/):
```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Clone the repository, create a virtual environment, and install AgentSUMO:
```bash
git clone https://github.com/mw-jeong/AgentSUMO
cd AgentSUMO

# Create a Python 3.12 venv
uv venv --python 3.12

# Activate the venv
source .venv/bin/activate              # macOS / Linux
# .venv\Scripts\activate               # Windows

# Install AgentSUMO and all dependencies
# (this also pulls agentsumo-mcp from PyPI as a dependency)
uv pip install -e .
```

### 3. Configure environment variables

AgentSUMO reads API keys and the SUMO path from environment variables. The easiest way is a `.env` file at the project root:
```bash
cp .env.example .env
```

Open `.env` in your editor and fill in:

**`ANTHROPIC_API_KEY`** (required) — Claude API key that drives the Planner Agent. Get one at the [Anthropic Console](https://console.anthropic.com/settings/keys).
```
ANTHROPIC_API_KEY=sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**`MAPBOX_TOKEN`** (required for the web UI) — used to render the basemap. Get one at the [Mapbox access tokens page](https://account.mapbox.com/access-tokens/).
```
MAPBOX_TOKEN=pk.eyJ1Ijoixxxxxxxxxxxxxxxxxx
```

**`SUMO_HOME`** (required) — absolute path to your local SUMO installation. The directory must contain `bin/sumo` (or `bin/sumo.exe` on Windows).
```
# macOS (Homebrew)
SUMO_HOME=/opt/homebrew/share/sumo

# macOS (Eclipse SUMO installer)
SUMO_HOME=/Library/Frameworks/EclipseSUMO.framework/Versions/<version>/EclipseSUMO  # e.g. 1.24.0; use the directory name installed under Versions/

# Windows
SUMO_HOME=C:\Program Files (x86)\Eclipse\Sumo

# Linux
SUMO_HOME=/usr/share/sumo
```

**`AGENTSUMO_MCP_OUTPUT_BASE`** (optional) — override the base directory where the MCP server writes simulation outputs (networks, trips, results). Defaults to the current working directory.
```
AGENTSUMO_MCP_OUTPUT_BASE=/path/to/your/output/dir
```

### 4. Run

```bash
# Web interface (opens at http://localhost:8000)
python web.py

# CLI mode
python chat.py

# Clean up simulation outputs
python clean.py
```

## Project Structure

```
AgentSUMO/
├── agentsumo/
│   ├── agent/        # Planner Agent (Claude orchestrator + prompts)
│   ├── client/       # MCP clients (AgentSUMO, SQLite, Filesystem)
│   └── core/         # Configuration
├── agentsumo_mcp/    # AgentSUMO MCP Server source (also published to PyPI)
│   └── defaults/     # Packaged fixtures (e.g., vehicle_types.add.xml)
├── packaging/mcp/    # PyPI build configuration for agentsumo-mcp
├── web/              # Web interface (FastAPI + Jinja2 templates)
├── docs/             # Sphinx documentation source
├── tests/            # Unit tests
├── assets/           # README images
├── output/           # Runtime artifacts (auto-populated; 8 categories tracked
│                     #   via .gitkeep — simulations/, networks/, trips/,
│                     #   analysis/, reports/, uploads/, visualizations/, additional/)
├── chat.py           # CLI entry point
├── web.py            # Web server entry point
└── .env.example      # Environment variable template
```

## Use the MCP Server Standalone

The AgentSUMO MCP Server can be used independently from this framework with any MCP-compatible LLM client (Claude Desktop, OpenAI tool clients, Gemini, local LLMs):

```bash
pip install agentsumo-mcp
```

Or via [`uvx`](https://docs.astral.sh/uv/guides/tools/) without installing:

```bash
uvx agentsumo-mcp
```

The server is registered in the official [MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=agentsumo) under `io.github.mw-jeong/agentsumo-mcp`.

## Troubleshooting

**SUMO path error** — Verify `SUMO_HOME` in your `.env`. The directory must contain `bin/sumo` (or `bin/sumo.exe` on Windows).

**API key error** — Verify `ANTHROPIC_API_KEY` in your `.env` is set to a valid Claude API key. The Planner Agent will refuse to start without it.

**Dependency error** — Re-resolve dependencies:
```bash
uv pip install -e . --upgrade
```

**Legacy token files (deprecated, scheduled for removal in 0.2.0)** — AgentSUMO still falls back to `claude_api.txt` and `mapbox_token.txt` at the project root when the corresponding environment variables are missing, but those code paths now emit a `DeprecationWarning` at import time. Use the `.env` workflow for new installations.

## Documentation

Full documentation lives at **[agentsumo.readthedocs.io](https://agentsumo.readthedocs.io)**.

- [Installation](https://agentsumo.readthedocs.io/en/latest/installation.html) — SUMO, Python 3.10+, environment setup
- [Tools](https://agentsumo.readthedocs.io/en/latest/mcp_servers/agentsumo/index.html) — reference for all MCP tools
- [Schema](https://agentsumo.readthedocs.io/en/latest/database/index.html) — `simulations.db` ER diagram and column reference
- [Tutorials](https://agentsumo.readthedocs.io/en/latest/tutorials/index.html) — walkthroughs of the paper case studies

## Citation

If you use AgentSUMO in academic work, please cite:

```bibtex
@article{jeong2025agentsumo,
  title         = {AgentSUMO: An Agentic Framework for Interactive Simulation Scenario Generation in SUMO via Large Language Models},
  author        = {Jeong, Minwoo and Chang, Jeeyun and Yoon, Yoonjin},
  journal       = {arXiv preprint arXiv:2511.06804},
  year          = {2025},
  url           = {https://arxiv.org/abs/2511.06804}
}
```

## License

MIT. See [LICENSE](LICENSE).

---

<div align="center">

<sub>Developed at</sub>

<img src="assets/logo_kaist.png" alt="KAIST" height="55"/> &nbsp;&nbsp;&nbsp;&nbsp;
<img src="assets/logo_caus.png" alt="CAUS" height="55"/> &nbsp;&nbsp;&nbsp;&nbsp;
<img src="assets/logo_stil.png" alt="Spatial Tech Innovation Lab" height="55"/>

</div>
