"""
Settings for SUMO MCP Server.
"""

import os
import shutil
from pathlib import Path


def _load_sumo_home() -> str:
    """Load SUMO_HOME from sumo_home.txt or environment variable."""
    # Check sumo_home.txt at the project root
    project_root = Path(__file__).parent.parent.parent.parent
    sumo_home_file = project_root / "sumo_home.txt"

    # 1. Load from file
    if sumo_home_file.exists():
        with open(sumo_home_file, 'r') as f:
            path = f.read().strip()
            if path:
                return path

    # 2. Load from environment variable
    env_path = os.environ.get("SUMO_HOME")
    if env_path:
        return env_path

    # 3. Default (macOS)
    return "/Library/Frameworks/EclipseSUMO.framework/Versions/1.24.0/EclipseSUMO"


class SUMOEnvironment:
    """SUMO environment settings."""

    # SUMO environment (sumo_home.txt > env var > default)
    SUMO_HOME = _load_sumo_home()

    # Base paths
    BASE_DIR = Path(__file__).parent.parent.parent
    DEFAULT_OUTPUT_DIR = BASE_DIR / "output"

    # Output directories
    NETWORKS_DIR = DEFAULT_OUTPUT_DIR / "networks"
    TRIPS_DIR = DEFAULT_OUTPUT_DIR / "trips"
    SIMULATIONS_DIR = DEFAULT_OUTPUT_DIR / "simulations"
    ANALYSIS_DIR = DEFAULT_OUTPUT_DIR / "analysis"
    REPORTS_DIR = DEFAULT_OUTPUT_DIR / "reports"
    VISUALIZATIONS_DIR = DEFAULT_OUTPUT_DIR / "visualizations"

    # Data file paths
    OSM_PBF_PATH = BASE_DIR / "data" / "new-york-latest.osm.pbf"

    # SUMO tool paths
    @classmethod
    def get_tool_path(cls, tool_name: str) -> str:
        """Get path to SUMO tool."""
        for directory in (Path(cls.SUMO_HOME) / 'tools', Path(cls.SUMO_HOME) / 'share' / 'sumo' / 'tools'):
            if (directory / tool_name).is_file():
                return str(directory / tool_name)
        raise FileNotFoundError(f'SUMO tool not found: {tool_name}; check SUMO_HOME')

    @classmethod
    def get_binary_path(cls, binary_name: str) -> str:
        """Get path to SUMO binary."""
        executable = binary_name + (".exe" if os.name == 'nt' and not binary_name.endswith('.exe') else "")
        configured = Path(cls.SUMO_HOME) / 'bin' / executable
        if configured.is_file():
            return str(configured)
        return shutil.which(executable) or str(configured)


class SimulationSettings:
    """Simulation settings."""

    # Simulation defaults
    DEFAULT_DURATION = 3600  # 1 hour
    DEFAULT_SEED = 42

    # Ratios per traffic condition
    TRAFFIC_RATIOS = {
        "light": 0.2,
        "medium": 0.8,
        "heavy": 1.5
    }

    # Ratios per vehicle type
    DEFAULT_ELECTRIC_RATIO = 0.2

    # Simulation time range
    DEFAULT_BEGIN_TIME = 0
    DEFAULT_END_TIME = 3600


class NetworkSettings:
    """Network settings."""

    # Network generation defaults
    DEFAULT_GRID_SIZE = 500  # meters
    DEFAULT_BUFFER = 0.003  # degrees

    # Road-type mapping
    TYPE_FILES = "osmNetconvert.typ.xml"

    # Network conversion options
    NETCONVERT_OPTIONS = [
        "--proj.utm",
        "--keep-edges.by-vclass", "passenger",
        "--output.street-names",
        "--remove-edges.by-type", "highway.motorway,highway.motorway_link"
    ]


class VisualizationSettings:
    """Visualization settings."""

    # Visualization defaults
    DEFAULT_DPI = 300
    DEFAULT_EDGE_WIDTH = 0.5
    DEFAULT_EDGE_COLOR = "#606060"
    DEFAULT_SELECTED_WIDTH = 1.0
    DEFAULT_SELECTED_COLOR = "#800000"

    # Colormap settings
    DEFAULT_COLORMAP = "#0:#606060,0.25:#00c000,0.5:#ffff00,0.75:#c00000,1:#600000"

    # Axis labels
    XLABEL = "[m]"
    YLABEL = "[m]"


class AnalysisSettings:
    """Analysis settings."""

    # Analysis defaults
    DEFAULT_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

    # Statistics calculation patterns
    STATS_PATTERNS = {
        'count': r'count (\d+)',
        'min': r'min ([\d.]+)',
        'max': r'max ([\d.]+)',
        'mean': r'mean ([\d.]+)',
        'Q1': r'Q1 ([\d.]+)',
        'median': r'median ([\d.]+)',
        'Q3': r'Q3 ([\d.]+)',
        'stdDev': r'stdDev ([\d.]+)'
    }

    # Unit conversion settings
    SPEED_CONVERSION = 3.6  # m/s to km/h
    DISTANCE_CONVERSION = 1000  # m to km
    EMISSION_CONVERSION = 1000000  # mg to g


# Global settings instances
sumo_environment = SUMOEnvironment()
simulation_settings = SimulationSettings()
network_settings = NetworkSettings()
visualization_settings = VisualizationSettings()
analysis_settings = AnalysisSettings()
