<div align="center">

<img src="web/static/ditwin-logo.svg" alt="Ditwin logo" width="240" />

# Ditwin

### Explore the city. Simulate scenarios. Support informed decisions.

A digital twin workspace for road simulation and water planning, focused on Laâyoune.

[![Tests](https://github.com/its-Medd/UrbanFlow-AI/actions/workflows/tests.yml/badge.svg)](https://github.com/its-Medd/UrbanFlow-AI/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/Python-3.12%20%7C%203.13-3776AB?logo=python&logoColor=white)
![Backend](https://img.shields.io/badge/Backend-FastAPI-009688?logo=fastapi&logoColor=white)
![Simulation](https://img.shields.io/badge/Traffic-Eclipse%20SUMO-00549F)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[Overview](#overview) · [Installation](#installation) · [Configuration](#configuration) · [Architecture](#architecture) · [Team](#team)

</div>

---

## Overview

Ditwin brings geographic visualization, road simulation, and water balance forecasting into one workspace.

Explore mapped buildings in 3D, run SUMO simulations using imported road networks and traffic demand, and calculate water storage forecasts from supplied measurements. An optional AI assistant supports interaction with the simulation tools through the Model Context Protocol (MCP).

The application starts with an empty workspace. Operational data must be imported by the user.

## Features

| Capability | Description |
|---|---|
| **3D geographic workspace** | Explore Laâyoune using MapLibre, OpenStreetMap, and OpenFreeMap building geometry. |
| **Road simulation** | Import georeferenced SUMO networks and matching demand, configure a run, and execute SUMO. |
| **Recorded replay** | Review available simulation output through playback and geographic overlays. |
| **Traffic analysis** | Inspect heatmaps and SQLite analysis generated from completed runs. |
| **Water planning** | Import production, demand, storage, and capacity measurements to calculate storage forecasts. |
| **Optional AI assistant** | Use a configured Anthropic or Groq provider with the existing MCP tool integration. |
| **Explicit readiness states** | See whether simulation tools and AI credentials are configured before using them. |

## Installation

### Requirements

- Python **3.12 or 3.13**
- Eclipse SUMO **1.24 or later** for traffic simulation
- A browser with WebGL support
- Internet access for map tiles and external browser resources
- Node.js and npm for the optional filesystem MCP integration
- An Anthropic or Groq API key to enable AI chat

### Clone the repository

```bash
git clone https://github.com/its-Medd/UrbanFlow-AI.git
cd UrbanFlow-AI
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

Copy-Item .env.example .env
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

cp .env.example .env
```

### Start the application

Edit `.env` as needed, then run:

```bash
python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

Open:

- **Application:** http://127.0.0.1:8000
- **API documentation:** http://127.0.0.1:8000/docs
- **Readiness endpoint:** http://127.0.0.1:8000/api/health

Alternatively, with uv:

```bash
uv sync --extra dev
uv run ditwin
```

## Configuration

Copy `.env.example` to `.env` and configure the capabilities you need.

| Variable | Purpose |
|---|---|
| `SUMO_HOME` | SUMO installation root containing `bin/sumo` or `bin/sumo.exe`. Executable discovery through PATH is also supported. |
| `LLM_PROVIDER` | AI provider: `anthropic` or `groq`. |
| `ANTHROPIC_API_KEY` | Server credential when using Anthropic. |
| `GROQ_API_KEY` | Server credential when using Groq. |
| `LLM_MODEL` | Optional tool-capable model identifier. |
| `MULTI_AGENT_MODE` | Enables specialist advisory calls through the configured AI provider. |
| `MAPBOX_TOKEN` | Optional public `pk.` token. Leave blank to use MapLibre and OpenStreetMap. |
| `AGENTSUMO_MCP_OUTPUT_BASE` | Optional workspace root. Runtime data is stored under `<root>/output`. |

Keep credentials in `.env`, which is excluded from Git.

Without an AI credential, chat remains unavailable. Direct road and water workflows can still be used when their required tools and input data are available.

## Road Simulation

1. Import a georeferenced SUMO `.net.xml` network.
2. Import matching traffic demand in `.rou.xml` or `.trips.xml` format.
3. Open **Road simulation**.
4. Select the network and demand files.
5. Name the run and choose a duration from **30 to 7200 seconds**.
6. Start the simulation and follow its status in **Runs**.
7. Open replay, heatmap, or analysis when the corresponding outputs are available.

Networks without a geographic projection are rejected because they cannot be placed accurately on the map.

Simulation results depend on the supplied network and demand. They represent modeled conditions rather than live traffic observations.

## Water Planning

Open **Water simulation**, select a measurements JSON file, and click **Import measurements**.

All fields below are required:

| Field | Description |
|---|---|
| `source` | Source name or dataset reference, up to 200 characters |
| `observed_at` | ISO 8601 observation timestamp including a timezone |
| `production_m3_day` | Nonnegative production rate in cubic metres per day |
| `demand_m3_day` | Nonnegative demand rate in cubic metres per day |
| `storage_m3` | Nonnegative usable storage in cubic metres |
| `storage_capacity_m3` | Positive storage capacity, at least equal to current storage |

Unknown fields, nonfinite numbers, and invalid storage ranges are rejected.

After importing measurements:

1. Choose a forecast horizon from **1 to 168 hours**.
2. Set production loss and demand reduction percentages.
3. Select **Calculate forecast**.
4. Review storage over time, cumulative unmet demand, and overflow.

The model applies an hourly storage balance using constant adjusted production and demand rates:

```text
Hourly balance = Previous storage + (Daily production − Daily demand) / 24
```

Storage is bounded between zero and the supplied capacity. Shortfalls accumulate as unmet demand, and excess volume accumulates as overflow.

Measurements persist in:

```text
output/water/measurements.json
```

Importing a new measurements file replaces the current dataset.

### Water API

| Method | Endpoint | Purpose |
|---|---|---|
| `PUT` | `/api/water/measurements` | Validate and store measurements |
| `GET` | `/api/water/measurements` | Retrieve the current measurements |
| `POST` | `/api/water/forecast` | Calculate a forecast from stored measurements |

Forecast requests return **HTTP 409** when measurements have not been imported.

## Architecture

```text
Browser
  |
  +-- 3D map and workspace interface
  +-- Road simulation controls
  +-- Water import and forecast charts
  +-- Optional AI chat
  |
  v
FastAPI application
  |
  +-- Workspace API --> SUMO jobs --> Replay and simulation outputs
  |
  +-- Water API --> Validated measurements --> Storage balance forecast
  |
  +-- AI WebSocket --> Provider --> MCP clients and simulation tools
  |
  +-- Local persistence --> Files, job records, and SQLite analysis
```

### Project Structure

```text
UrbanFlow-AI/
├── agentsumo/                 # AI coordination, prompts, and MCP clients
├── agentsumo_mcp/             # Bundled SUMO tool server and execution code
├── web/
│   ├── app.py                 # FastAPI application and AI WebSocket
│   ├── workspace.py           # Asset imports, readiness, and SUMO jobs
│   ├── water.py               # Water validation and forecast API
│   ├── cli.py                 # Application command
│   ├── templates/
│   │   └── index.html         # Main interface
│   └── static/
│       ├── workspace.js       # Workspace interactions
│       ├── workspace.css      # Interface styles
│       ├── water.js           # Water workflow and chart
│       ├── ditwin-city.js     # Geographic building layer
│       ├── ditwin-logo.svg    # Project logo
│       └── vendor/            # Bundled browser libraries and licenses
├── tests/                     # Automated checks
├── docs/                      # Documentation and upstream references
├── output/                    # Local runtime data, excluded from Git
├── .env.example               # Configuration template
├── .gitignore
├── pyproject.toml
├── uv.lock
├── README.md
└── LICENSE
```

The application uses the Ditwin brand. Python package names remain `agentsumo` and `agentsumo_mcp` for compatibility with the upstream implementation.

## Team

| Member | Role | Areas of contribution |
|---|---|---|
| **Moussa Elaadmi** | Team leader and code integration | Project coordination, code assembly, module integration |
| **Mohamed Belmaizi** | Frontend, LLM integration, and IoT | Web interfaces, language model integration, connected devices |
| **Mohamed Jbiri** | Civil engineering | Urban infrastructure, network analysis, terrain constraints |
| **Mehdi Elmouate** | AI, simulation, and backend | AI models, scenario simulation, backend services |
| **Walid Charoune** | IT and security manager | IT systems, access management, data security |

## Development

Install development dependencies and run the checks:

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/ -q
git diff --check
```

Test fixtures remain isolated from application data and do not populate the workspace.

GitHub Actions runs automated tests on pushes and pull requests to `main`.

## Temporary HTTPS Sharing

With the application running locally:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Cloudflare prints a temporary HTTPS address.

Keep the application and tunnel processes running while sharing the link. Restarting a quick tunnel generates a new address.

## Current Limitations

- The application uses a single shared workspace and does not implement user authentication or tenant isolation.
- Water forecasts use a storage mass balance rather than a hydraulic pressure or pipe-flow model.
- Imported measurements require source verification by the user.
- No live municipal sensor connection or automated infrastructure control is configured.
- Building geometry and heights depend on map-provider data and may include inferred values.
- AI capabilities require a valid provider credential and can incur provider charges.
- Public deployment requires appropriate access control and operational configuration.

## Acknowledgments

Ditwin builds on the AgentSUMO framework and retains its original licensing and attribution.

The project adapts and extends the upstream implementation with Ditwin branding, a revised interface, imported-data water planning, and improvements to simulation workflows.

See [the preserved upstream README](docs/UPSTREAM_README.md) for the original project documentation and academic citation.

The project also uses Eclipse SUMO, FastAPI, MapLibre GL JS, OpenStreetMap, OpenFreeMap, Chart.js, Marked, and DOMPurify. Relevant license notices remain with the distributed components.

## License

Distributed under the **MIT License**. See [LICENSE](LICENSE).

---

<div align="center">

**Ditwin**  
Road simulation and water planning for Laâyoune

</div>
