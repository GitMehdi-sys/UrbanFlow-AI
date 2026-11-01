"""
AgentSUMO Web Application

FastAPI-based web server.
- REST API: network list, status queries, etc.
- WebSocket: real-time chat
"""

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import sumolib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from fastapi import HTTPException
from web.workspace import router as workspace_router, contained, filename, network_geojson, _run_lock
from web.water import router as water_router
from web.workspace import readiness

from agentsumo.core import AgentSUMOConfig

logger = logging.getLogger("agentsumo.web")


# ============================================
# Helper Functions
# ============================================

def get_output_dir() -> Optional[Path]:
    """Get output directory path"""
    output_dir = AgentSUMOConfig.OUTPUT_DIR
    return output_dir if output_dir.exists() else None


def get_networks_dir() -> Optional[Path]:
    """Get networks directory path"""
    networks_dir = AgentSUMOConfig.OUTPUT_DIR / "networks"
    return networks_dir if networks_dir.exists() else None


def get_simulations_dir() -> Optional[Path]:
    """Get simulations directory path"""
    sims_dir = AgentSUMOConfig.OUTPUT_DIR / "simulations"
    return sims_dir if sims_dir.exists() else None


def find_network_file(network_name: str) -> Optional[Path]:
    """Find network file by name"""
    networks_dir = get_networks_dir()
    if networks_dir:
        net_file = contained(networks_dir, filename(network_name))
        if net_file.exists():
            return net_file
    return None


def find_simulation_file(filename: str, pattern: str = None) -> Optional[Path]:
    """Find simulation file by name or pattern"""
    sims_dir = get_simulations_dir()
    if not sims_dir:
        return None

    # Exact match
    exact_match = contained(sims_dir, filename)
    if exact_match.exists():
        return exact_match

    # Pattern match
    if pattern:
        for f in sims_dir.iterdir():
            if f.is_file() and pattern in f.name:
                return f

    return None

# Global agent instance (singleton per server)
_agent = None
_agent_lock = asyncio.Lock()
_chat_lock = asyncio.Lock()


async def get_agent():
    """Get or create AgentSUMO instance"""
    global _agent

    async with _agent_lock:
        if _agent is None:
            from agentsumo import AgentSUMO
            logger.info("Creating new AgentSUMO instance...")
            candidate = AgentSUMO(
                enable_debug=False,
                interface="web",
                enable_filesystem_mcp=True,
                filesystem_directories=[
                    str(AgentSUMOConfig.PROJECT_ROOT / "agentsumo" / "agent" / "additional_files_guide"),
                    str(AgentSUMOConfig.OUTPUT_DIR)
                ]
            )
            try:
                await candidate.start()
            except BaseException:
                try:
                    await candidate.close()
                except Exception:
                    pass
                raise
            _agent = candidate
            logger.info("AgentSUMO ready!")

        return _agent


def create_app() -> FastAPI:
    """Create FastAPI application"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Application lifespan manager"""
        # Startup
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
        )
        logger.info("AgentSUMO Web starting...")

        # Fail-fast environment check. Surface missing/broken configuration
        # at startup rather than letting the first chat message hit a
        # cryptic "MCP connection failed" further down the stack.
        llm_provider = os.environ.get("LLM_PROVIDER", "anthropic").lower()
        llm_key = "GROQ_API_KEY" if llm_provider == "groq" else "ANTHROPIC_API_KEY"
        required = [llm_key]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            logger.info("AI chat is unavailable until a provider credential is configured.")

        sumo_home = os.environ.get("SUMO_HOME")
        if sumo_home:
            sumo_bin = Path(sumo_home) / "bin" / ("sumo.exe" if os.name == "nt" else "sumo")
            if not sumo_bin.exists():
                logger.error(
                    "SUMO_HOME=%s does not contain bin/sumo. "
                    "Check the value against your local SUMO installation.",
                    sumo_home,
                )

        yield
        # Shutdown
        global _agent
        if _agent:
            await _agent.close()
            _agent = None
        logger.info("AgentSUMO Web stopped")

    app = FastAPI(
        title="Ditwin",
        description="Road simulation and water planning workspace",
        version="0.1.0",
        lifespan=lifespan
    )

    # Static files & templates
    web_dir = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=web_dir / "static"), name="static")
    templates = Jinja2Templates(directory=web_dir / "templates")
    app.include_router(workspace_router)
    app.include_router(water_router)

    @app.middleware("http")
    async def protect_local_workspace(request: Request, call_next):
        origin = request.headers.get('origin')
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and origin and origin != f'{request.url.scheme}://{request.url.netloc}':
            return JSONResponse(content={'error': 'Cross-origin workspace changes are not allowed'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        return response

    # ============================================
    # Routes
    # ============================================

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        from fastapi.responses import FileResponse
        return FileResponse(web_dir / "static" / "favicon.ico")

    @app.get("/")
    async def index(request: Request):
        """Main page"""
        try:
            mapbox_token = AgentSUMOConfig.get_mapbox_token()
        except RuntimeError:
            mapbox_token = ""
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "mapbox_token": mapbox_token}
        )

    @app.get("/landing")
    async def landing_page(request: Request):
        """Landing page (design preview)"""
        return templates.TemplateResponse(
            "landing.html",
            {"request": request}
        )

    @app.get("/preview")
    async def preview_page(request: Request):
        """Main interface dark theme preview"""
        try:
            mapbox_token = AgentSUMOConfig.get_mapbox_token()
        except RuntimeError:
            mapbox_token = ""
        return templates.TemplateResponse(
            "main_dark.html",
            {"request": request, "mapbox_token": mapbox_token}
        )

    @app.get("/api/state")
    async def get_state():
        """Get current agent state"""
        if not readiness()['ai']['configured']:
            return JSONResponse(content={'assistant': 'AI provider not configured', 'mode': 'unconfigured'})
        try:
            agent = await get_agent()
            state = agent.get_state()
            return JSONResponse(content=state)
        except Exception as e:
            return JSONResponse(
                content={"error": str(e)},
                status_code=500
            )

    @app.get("/api/operations")
    async def list_operations(limit: int = 20):
        """Return recorded agent runs and their real tool-execution timeline."""
        try:
            path = AgentSUMOConfig.OUTPUT_DIR / ".agent_executions.json"
            if not path.exists():
                return JSONResponse(content={"operations": [], "count": 0})
            operations = json.loads(path.read_text())
            limit = max(1, min(limit, 50))
            return JSONResponse(content={
                "operations": operations[:limit],
                "count": len(operations),
            })
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Could not read operation history: %s", exc)
            return JSONResponse(content={"error": "Operation history is unavailable", "operations": []}, status_code=500)

    @app.get("/api/networks")
    async def list_networks():
        """List available networks"""
        try:
            networks_dir = get_networks_dir()
            if networks_dir is None:
                return JSONResponse(content={"networks": [], "message": "No networks directory"})

            networks = [
                f.name for f in networks_dir.iterdir()
                if f.is_file() and f.name.endswith('.net.xml')
            ]

            return JSONResponse(content={
                "networks": sorted(networks),
                "count": len(networks),
                "directory": str(networks_dir)
            })
        except Exception as e:
            logger.error(f"Error listing networks: {e}")
            return JSONResponse(
                content={"error": str(e), "networks": []},
                status_code=500
            )

    @app.get("/api/files")
    async def list_output_files():
        """List files in output directory as tree structure"""
        try:
            output_dir = get_output_dir()
            if output_dir is None:
                return JSONResponse(content={"tree": [], "message": "No output directory"})

            def build_tree(path, base_path):
                """Recursively build file tree"""
                items = []
                try:
                    for entry in sorted(path.iterdir()):
                        # Skip hidden/internal files
                        if entry.name.startswith('.'):
                            continue
                        if entry.is_symlink():
                            continue
                        rel_path = entry.relative_to(base_path).as_posix()
                        if entry.is_dir():
                            children = build_tree(entry, base_path)
                            items.append({
                                "name": entry.name,
                                "path": rel_path,
                                "type": "folder",
                                "children": children,
                                "expanded": False  # Start collapsed
                            })
                        else:
                            # Get file info
                            stat = entry.stat()
                            size_kb = stat.st_size / 1024
                            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")

                            # Determine file type icon
                            file_type = "file"
                            if entry.suffix == ".xml":
                                if "net" in entry.name:
                                    file_type = "network"
                                elif "route" in entry.name or "trip" in entry.name:
                                    file_type = "route"
                                elif "netstate" in entry.name:
                                    file_type = "simulation"
                                else:
                                    file_type = "xml"
                            elif entry.suffix == ".osm":
                                file_type = "osm"
                            elif entry.suffix == ".html":
                                file_type = "report"

                            items.append({
                                "name": entry.name,
                                "path": rel_path,
                                "type": file_type,
                                "size": f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB",
                                "modified": mtime
                            })
                except PermissionError:
                    pass
                return items

            tree = build_tree(output_dir, output_dir)

            return JSONResponse(content={
                "tree": tree,
                "root": str(output_dir)
            })
        except Exception as e:
            logger.error(f"Error listing files: {e}")
            return JSONResponse(
                content={"error": str(e), "tree": []},
                status_code=500
            )

    @app.get("/api/report/{report_path:path}")
    async def get_report(report_path: str):
        """Serve HTML report file"""
        from fastapi.responses import FileResponse
        try:
            output_dir = get_output_dir()
            if output_dir is None:
                return JSONResponse(
                    content={"error": "No output directory"},
                    status_code=404
                )

            report_file = contained(output_dir, report_path)
            if not report_file.exists() or not report_file.suffix == ".html":
                return JSONResponse(
                    content={"error": f"Report not found: {report_path}"},
                    status_code=404
                )

            return FileResponse(
                path=str(report_file),
                media_type="text/html"
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error serving report: {e}")
            return JSONResponse(
                content={"error": str(e)},
                status_code=500
            )

    @app.get("/api/network/{network_name}/geojson")
    async def get_network_geojson(network_name: str):
        """Get network as GeoJSON for map display"""
        try:
            net_file = find_network_file(network_name)
            if net_file is None:
                return JSONResponse(
                    content={"error": f"Network not found: {network_name}"},
                    status_code=404
                )

            return JSONResponse(content=await asyncio.to_thread(network_geojson, net_file))

        except HTTPException:
            raise
        except ValueError as e:
            return JSONResponse(content={"error": str(e)}, status_code=422)
        except Exception as e:
            logger.error(f"Error loading network: {e}")
            return JSONResponse(
                content={"error": str(e)},
                status_code=500
            )

    @app.get("/api/simulations")
    async def list_simulations():
        """List available simulation results"""
        try:
            sims_dir = get_simulations_dir()
            if sims_dir is None:
                return JSONResponse(content={"simulations": [], "message": "No simulations directory"})

            # Find netstate files (actual SUMO output format: {name}_netstate_{timestamp}.xml)
            simulations = []
            for f in sims_dir.iterdir():
                if f.is_file() and '_netstate_' in f.name and f.name.endswith('.xml') and '_emission' not in f.name:
                    # Extract base name (e.g., "gangnam_station.net" from "gangnam_station.net_netstate_20251208_005012.xml")
                    parts = f.name.split('_netstate_')
                    if len(parts) >= 2:
                        base_name = parts[0]
                        timestamp = parts[1].replace('.xml', '')
                        simulations.append({
                            "name": f"{base_name}_{timestamp}",
                            "base_name": base_name,
                            "netstate_file": f.name,
                            "path": str(f),
                            "timestamp": timestamp
                        })

            return JSONResponse(content={
                "simulations": sorted(simulations, key=lambda x: x["timestamp"], reverse=True),
                "count": len(simulations),
                "directory": str(sims_dir)
            })
        except Exception as e:
            logger.error(f"Error listing simulations: {e}")
            return JSONResponse(
                content={"error": str(e), "simulations": []},
                status_code=500
            )

    @app.get("/api/heatmap/{network_name}/{simulation_name}")
    def get_heatmap_geojson(network_name: str, simulation_name: str, attribute: str = "density"):
        """Get simulation results as GeoJSON heatmap for map display"""
        try:
            net_file = find_network_file(network_name)
            if net_file is None:
                return JSONResponse(
                    content={"error": f"Network not found: {network_name}"},
                    status_code=404
                )

            # Find netstate file
            netstate_file = find_simulation_file(simulation_name, pattern="_netstate_")
            if netstate_file is None:
                return JSONResponse(
                    content={"error": f"Simulation netstate not found: {simulation_name}"},
                    status_code=404
                )

            logger.info(f"Loading heatmap from: {netstate_file}")

            # Parse network
            net = sumolib.net.readNet(str(net_file))

            # Parse netstate XML and extract attribute values
            edge_values = {}
            tree = ET.parse(str(netstate_file))
            root = tree.getroot()

            for interval in root.findall('.//interval'):
                for edge in interval.findall('edge'):
                    edge_id = edge.get('id')
                    value = edge.get(attribute)
                    if value is not None:
                        try:
                            edge_values[edge_id] = float(value)
                        except ValueError:
                            pass

            if not edge_values:
                return JSONResponse(
                    content={"error": f"No data found for attribute: {attribute}"},
                    status_code=404
                )

            # Build edge length lookup for filtering
            edge_lengths = {}
            for edge in net.getEdges():
                edge_lengths[edge.getID()] = edge.getLength()

            # Filter out very short edges (< 5m) — intersection connectors
            # produce extreme outlier values for all metrics
            filtered_values = {
                eid: val for eid, val in edge_values.items()
                if edge_lengths.get(eid, 0) >= 5.0
            }
            if not filtered_values:
                filtered_values = edge_values

            # Use percentile-based bounds to handle outliers (all attributes)
            sorted_vals = sorted(filtered_values.values())
            n = len(sorted_vals)
            p5_val = sorted_vals[int(n * 0.05)]
            p95_val = sorted_vals[min(int(n * 0.95), n - 1)]
            min_val = p5_val
            max_val = p95_val
            val_range = max_val - min_val if max_val != min_val else 1

            # Build GeoJSON with heatmap values (no invert — frontend handles colormap direction)
            features = []
            for edge in net.getEdges():
                edge_id = edge.getID()
                if edge_id not in edge_values:
                    continue

                if edge.getLength() < 5.0:
                    continue

                shape = edge.getShape()
                if len(shape) < 2:
                    continue

                coords = [net.convertXY2LonLat(x, y) for x, y in shape]
                value = edge_values[edge_id]
                normalized = max(0.0, min((value - min_val) / val_range, 1.0))

                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords
                    },
                    "properties": {
                        "id": edge_id,
                        "name": edge.getName() or edge_id,
                        "value": value,
                        "normalized": normalized,
                        "attribute": attribute
                    }
                })

            geojson = {
                "type": "FeatureCollection",
                "features": features
            }

            # Calculate center
            if features:
                all_coords = []
                for f in features:
                    all_coords.extend(f["geometry"]["coordinates"])
                lons = [c[0] for c in all_coords]
                lats = [c[1] for c in all_coords]
                center = {
                    "lon": sum(lons) / len(lons),
                    "lat": sum(lats) / len(lats)
                }
            else:
                center = {"lon": 127.028, "lat": 37.497}

            return JSONResponse(content={
                "geojson": geojson,
                "center": center,
                "edge_count": len(features),
                "attribute": attribute,
                "min_value": min_val,
                "max_value": max_val
            })

        except Exception as e:
            logger.error(f"Error loading heatmap: {e}")
            return JSONResponse(
                content={"error": str(e)},
                status_code=500
            )

    # ============================================
    # Dashboard API
    # ============================================

    @app.get("/api/dashboard")
    def get_dashboard_data(sim_a: str, sim_b: str = None):
        """Get dashboard data for one or two simulations"""
        try:
            output_dir = get_output_dir()
            if not output_dir:
                return JSONResponse(content={"error": "No output directory"}, status_code=404)

            analysis_dir = output_dir / "analysis"
            db_files = list(analysis_dir.glob("*.db")) if analysis_dir.exists() else []
            if not db_files:
                return JSONResponse(content={"error": "No analysis DB found"}, status_code=404)

            db_path = str(db_files[0])
            if Path(db_path).stat().st_size == 0:
                return JSONResponse(content={"error": "DB is empty"}, status_code=404)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            if sim_b:
                networks = conn.execute('SELECT simulation_id, net_file FROM simulations WHERE simulation_id IN (?, ?)', (sim_a, sim_b)).fetchall()
                if len({Path(row['net_file']).name for row in networks if row['net_file']}) > 1:
                    conn.close()
                    return JSONResponse(content={"error": "Compare runs on the same road network"}, status_code=409)

            def get_kpi(sim_id):
                r = conn.execute("""
                    SELECT COUNT(*) as trip_count,
                           AVG(duration) as avg_duration,
                           AVG(waitingTime) as avg_waiting,
                           AVG(timeLoss) as avg_timeloss,
                           AVG(routeLength) as avg_route_length,
                           SUM(CO2_abs) as total_co2,
                           SUM(fuel_abs) as total_fuel,
                           SUM(NOx_abs) as total_nox
                    FROM trips WHERE simulation_id=?
                """, (sim_id,)).fetchone()
                if not r or r['trip_count'] == 0:
                    return None

                # Average speed from edge_metrics
                spd = conn.execute("""
                    SELECT AVG(speed) as avg_speed FROM edge_metrics
                    WHERE simulation_id=? AND speed > 0
                """, (sim_id,)).fetchone()

                return {
                    "trip_count": r['trip_count'],
                    "avg_duration": round(r['avg_duration'], 1),
                    "avg_waiting": round(r['avg_waiting'], 1),
                    "avg_timeloss": round(r['avg_timeloss'], 1),
                    "avg_route_length": round(r['avg_route_length'], 1),
                    "total_co2_kg": round((r['total_co2'] or 0) / 1e6, 1),
                    "total_fuel_L": round((r['total_fuel'] or 0) / 1e6, 1),
                    "total_nox_g": round((r['total_nox'] or 0) / 1000, 1),
                    "avg_speed_kmh": round((spd['avg_speed'] or 0) * 3.6, 1)
                }

            def get_duration_distribution(sim_id):
                buckets = [
                    ("< 1 min", 0, 60),
                    ("1-3 min", 60, 180),
                    ("3-5 min", 180, 300),
                    ("5-10 min", 300, 600),
                    ("10+ min", 600, 999999)
                ]
                result = []
                for label, lo, hi in buckets:
                    r = conn.execute(
                        "SELECT COUNT(*) as cnt FROM trips WHERE simulation_id=? AND duration>=? AND duration<?",
                        (sim_id, lo, hi)
                    ).fetchone()
                    result.append({"bucket": label, "count": r['cnt']})
                return result

            def get_top_congested(sim_id, limit=5):
                """Get top congested roads aggregated by road name, length-weighted"""
                rows = conn.execute("""
                    SELECT edge_id, AVG(density) as density, AVG(speed) as speed,
                           AVG(waitingTime) as waiting_time
                    FROM edge_metrics
                    WHERE simulation_id=? AND density > 0
                    GROUP BY edge_id
                """, (sim_id,)).fetchall()

                # Load network for road names and lengths
                net_file_row = conn.execute(
                    "SELECT net_file FROM simulations WHERE simulation_id=?", (sim_id,)
                ).fetchone()

                net = None
                if net_file_row and net_file_row['net_file']:
                    try:
                        net = sumolib.net.readNet(net_file_row['net_file'])
                    except Exception:
                        pass

                if not net:
                    return []

                # Aggregate by road name (length-weighted, skip < 5m edges)
                road_data = {}  # name -> {sum_density_x_len, sum_speed_x_len, sum_wait_x_len, total_len, edge_ids}
                for r in rows:
                    try:
                        edge = net.getEdge(r['edge_id'])
                        length = edge.getLength()
                        if length < 5.0:
                            continue
                        name = edge.getName()
                        if not name:
                            continue
                    except Exception:
                        continue

                    if name not in road_data:
                        road_data[name] = {
                            "sum_d": 0, "sum_s": 0, "sum_w": 0,
                            "total_len": 0, "edge_ids": []
                        }
                    rd = road_data[name]
                    rd["sum_d"] += r['density'] * length
                    rd["sum_s"] += r['speed'] * length
                    rd["sum_w"] += r['waiting_time'] * length
                    rd["total_len"] += length
                    rd["edge_ids"].append(r['edge_id'])

                # Compute weighted averages and sort
                result = []
                for name, rd in road_data.items():
                    if rd["total_len"] == 0:
                        continue
                    result.append({
                        "name": name,
                        "density": round(rd["sum_d"] / rd["total_len"], 1),
                        "speed_kmh": round(rd["sum_s"] / rd["total_len"] * 3.6, 1),
                        "waiting_time": round(rd["sum_w"] / rd["total_len"], 1),
                        "total_length_m": round(rd["total_len"], 0),
                        "segment_count": len(rd["edge_ids"]),
                        "edge_ids": rd["edge_ids"]
                    })

                result.sort(key=lambda x: x["density"], reverse=True)
                return result[:limit]

            def get_time_series(sim_id, sample_interval=10):
                """Get time-series data from network_state, sampled every N seconds."""
                try:
                    # Check if table exists
                    tables = [r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='network_state'"
                    ).fetchall()]
                    if not tables:
                        return None

                    rows = conn.execute("""
                        SELECT time, running, halting, waiting, meanSpeed, meanSpeedRelative
                        FROM network_state
                        WHERE simulation_id=? AND CAST(time AS INTEGER) % ? = 0
                        ORDER BY time
                    """, (sim_id, sample_interval)).fetchall()

                    if not rows:
                        return None

                    return {
                        "time": [r['time'] for r in rows],
                        "running": [r['running'] for r in rows],
                        "halting": [r['halting'] for r in rows],
                        "waiting": [r['waiting'] for r in rows],
                        "meanSpeed": [round(r['meanSpeed'] * 3.6, 1) if r['meanSpeed'] else 0 for r in rows],
                        "meanSpeedRelative": [round(r['meanSpeedRelative'], 3) if r['meanSpeedRelative'] else 0 for r in rows]
                    }
                except Exception as e:
                    logger.warning(f"Failed to get time series for {sim_id}: {e}")
                    return None

            # Build response
            kpi_a = get_kpi(sim_a)
            if not kpi_a:
                conn.close()
                return JSONResponse(content={"error": f"No data for {sim_a}"}, status_code=404)

            data = {
                "sim_a": sim_a,
                "kpi_a": kpi_a,
                "duration_dist_a": get_duration_distribution(sim_a),
                "top_congested_a": get_top_congested(sim_a),
                "has_compare": sim_b is not None
            }

            ts_a = get_time_series(sim_a)
            if ts_a:
                data["time_series_a"] = ts_a

            if sim_b:
                kpi_b = get_kpi(sim_b)
                if kpi_b:
                    data["sim_b"] = sim_b
                    data["kpi_b"] = kpi_b
                    data["duration_dist_b"] = get_duration_distribution(sim_b)
                    data["top_congested_b"] = get_top_congested(sim_b)
                    ts_b = get_time_series(sim_b)
                    if ts_b:
                        data["time_series_b"] = ts_b

            conn.close()
            return JSONResponse(content=data)

        except Exception as e:
            logger.error(f"Error getting dashboard data: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return JSONResponse(content={"error": str(e)}, status_code=500)

    # ============================================
    # Compare Heatmap API (DB-based)
    # ============================================

    COMPARE_ATTRIBUTES = [
        'density', 'speed', 'waitingTime', 'timeLoss',
        'occupancy', 'laneDensity', 'traveltime', 'sampledSeconds',
        'CO2_abs', 'NOx_abs', 'fuel_abs', 'CO_abs', 'PMx_abs'
    ]

    # Attributes where higher = better (need inverted colormap)
    INVERT_ATTRIBUTES = {'speed', 'speedRelative'}

    @app.get("/api/db/simulations")
    async def list_db_simulations():
        """List simulation IDs from the analysis DB"""
        try:
            output_dir = get_output_dir()
            if not output_dir:
                return JSONResponse(content={"simulations": [], "db_path": None})

            analysis_dir = output_dir / "analysis"
            if not analysis_dir.exists():
                return JSONResponse(content={"simulations": [], "db_path": None})

            # Find first .db file
            db_files = list(analysis_dir.glob("*.db"))
            if not db_files:
                return JSONResponse(content={"simulations": [], "db_path": None})

            db_path = str(db_files[0])

            # Skip empty DB files
            if Path(db_path).stat().st_size == 0:
                return JSONResponse(content={"simulations": [], "db_path": None})

            conn = sqlite3.connect(db_path)

            # Check if simulations table exists
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='simulations'"
            ).fetchone()
            if not table_check:
                conn.close()
                return JSONResponse(content={"simulations": [], "db_path": db_path})

            rows = conn.execute(
                "SELECT simulation_id, description, vehicle_count, net_file FROM simulations ORDER BY created_at"
            ).fetchall()
            conn.close()

            sims = []
            for r in rows:
                # Skip entries with no description (auto-generated duplicates)
                if r[1] is None:
                    continue
                sims.append({
                    "id": r[0],
                    "label": r[1],
                    "description": r[1],
                    "vehicle_count": r[2],
                    "net_file": r[3]
                })

            return JSONResponse(content={
                "simulations": sims,
                "db_path": db_path,
                "count": len(sims)
            })
        except Exception as e:
            logger.error(f"Error listing DB simulations: {e}")
            return JSONResponse(content={"error": str(e), "simulations": []}, status_code=500)

    @app.get("/api/compare")
    async def compare_simulations(
        network_name: str,
        sim_a: str,
        sim_b: str = None,
        db_path: str = None
    ):
        """
        Compare one or two simulations. Returns GeoJSON with all attributes.
        All attribute values + normalized values are included per edge.
        """
        try:
            import sqlite3 as sqlite3_mod

            net_file = find_network_file(network_name)
            if not net_file:
                return JSONResponse(content={"error": f"Network not found: {network_name}"}, status_code=404)

            # Find DB
            if not db_path:
                analysis_dir = get_output_dir() / "analysis"
                db_files = list(analysis_dir.glob("*.db")) if analysis_dir.exists() else []
                if not db_files:
                    return JSONResponse(content={"error": "No analysis DB found"}, status_code=404)
                db_path = str(db_files[0])

            conn = sqlite3_mod.connect(db_path)
            conn.row_factory = sqlite3_mod.Row

            attrs = COMPARE_ATTRIBUTES

            # Build AVG query for all attributes
            avg_cols = ", ".join(f"AVG({a}) as {a}" for a in attrs)

            # Query sim_a
            rows_a = conn.execute(
                f"SELECT edge_id, {avg_cols} FROM edge_metrics WHERE simulation_id=? GROUP BY edge_id",
                (sim_a,)
            ).fetchall()

            data_a = {r['edge_id']: dict(r) for r in rows_a}

            # Query sim_b if provided
            data_b = {}
            if sim_b:
                rows_b = conn.execute(
                    f"SELECT edge_id, {avg_cols} FROM edge_metrics WHERE simulation_id=? GROUP BY edge_id",
                    (sim_b,)
                ).fetchall()
                data_b = {r['edge_id']: dict(r) for r in rows_b}

            conn.close()

            if not data_a:
                return JSONResponse(content={"error": f"No data for simulation: {sim_a}"}, status_code=404)

            # Parse network for geometry
            net = sumolib.net.readNet(str(net_file))

            # Build edge length filter
            edge_objs = {e.getID(): e for e in net.getEdges()}

            # Collect values for percentile normalization (per attribute)
            attr_values_a = {a: [] for a in attrs}
            attr_values_b = {a: [] for a in attrs}
            attr_values_diff = {a: [] for a in attrs}

            # First pass: collect values (filtered by length >= 5m)
            valid_edges = set()
            for edge_id in data_a:
                edge_obj = edge_objs.get(edge_id)
                if not edge_obj or edge_obj.getLength() < 5.0:
                    continue
                valid_edges.add(edge_id)
                for a in attrs:
                    val = data_a[edge_id].get(a)
                    if val is not None:
                        attr_values_a[a].append(val)
                    if sim_b and edge_id in data_b:
                        val_b = data_b[edge_id].get(a)
                        if val_b is not None:
                            attr_values_b[a].append(val_b)
                        if val is not None and val_b is not None:
                            attr_values_diff[a].append(val_b - val)

            # Compute percentile bounds per attribute
            def percentile_bounds(values):
                if not values:
                    return 0, 1
                s = sorted(values)
                n = len(s)
                lo = s[int(n * 0.05)]
                hi = s[min(int(n * 0.95), n - 1)]
                if hi == lo:
                    hi = lo + 1
                return lo, hi

            bounds_a = {a: percentile_bounds(attr_values_a[a]) for a in attrs}
            bounds_b = {a: percentile_bounds(attr_values_b[a]) for a in attrs}

            # Diff bounds: symmetric around 0 so that 0 = white center
            def symmetric_bounds(values):
                if not values:
                    return -1, 1
                s = sorted(values)
                n = len(s)
                lo = s[int(n * 0.05)]
                hi = s[min(int(n * 0.95), n - 1)]
                extent = max(abs(lo), abs(hi))
                if extent == 0:
                    extent = 1
                return -extent, extent

            bounds_diff = {a: symmetric_bounds(attr_values_diff[a]) for a in attrs}

            # Build GeoJSON
            features = []
            for edge_id in valid_edges:
                edge_obj = edge_objs.get(edge_id)
                if not edge_obj:
                    continue

                shape = edge_obj.getShape()
                if len(shape) < 2:
                    continue

                coords = [net.convertXY2LonLat(x, y) for x, y in shape]

                props = {
                    "id": edge_id,
                    "name": edge_obj.getName() or edge_id,
                    "length": edge_obj.getLength(),
                    "lanes": edge_obj.getLaneNumber(),
                }

                for a in attrs:
                    val_a = data_a[edge_id].get(a)

                    if val_a is not None:
                        props[f"{a}_a"] = round(val_a, 4)
                        lo, hi = bounds_a[a]
                        norm = max(0.0, min((val_a - lo) / (hi - lo), 1.0))
                        props[f"n_{a}_a"] = round(norm, 4)
                    else:
                        props[f"{a}_a"] = None
                        props[f"n_{a}_a"] = 0

                    if sim_b and edge_id in data_b:
                        val_b = data_b[edge_id].get(a)
                        if val_b is not None:
                            props[f"{a}_b"] = round(val_b, 4)
                            lo, hi = bounds_b[a]
                            norm = max(0.0, min((val_b - lo) / (hi - lo), 1.0))
                            props[f"n_{a}_b"] = round(norm, 4)

                            # Diff — symmetric around 0: normalized 0.5 = no change
                            diff = val_b - val_a if val_a is not None else 0
                            props[f"{a}_diff"] = round(diff, 4)
                            lo_d, hi_d = bounds_diff[a]
                            norm_d = max(0.0, min((diff - lo_d) / (hi_d - lo_d), 1.0))
                            props[f"n_{a}_diff"] = round(norm_d, 4)
                        else:
                            props[f"{a}_b"] = None
                            props[f"n_{a}_b"] = 0
                            props[f"{a}_diff"] = None
                            props[f"n_{a}_diff"] = 0.5
                    else:
                        props[f"{a}_b"] = None
                        props[f"n_{a}_b"] = 0
                        props[f"{a}_diff"] = None
                        props[f"n_{a}_diff"] = 0.5

                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": props
                })

            geojson = {"type": "FeatureCollection", "features": features}

            # Center
            if features:
                all_coords = []
                for f in features:
                    all_coords.extend(f["geometry"]["coordinates"])
                lons = [c[0] for c in all_coords]
                lats = [c[1] for c in all_coords]
                center = {"lon": sum(lons)/len(lons), "lat": sum(lats)/len(lats)}
            else:
                center = {"lon": 127.028, "lat": 37.497}

            # Return bounds info for legend
            bounds_info = {}
            for a in attrs:
                lo_a, hi_a = bounds_a[a]
                bounds_info[a] = {"min_a": round(lo_a, 2), "max_a": round(hi_a, 2)}
                if sim_b:
                    lo_b, hi_b = bounds_b[a]
                    lo_d, hi_d = bounds_diff[a]
                    bounds_info[a].update({
                        "min_b": round(lo_b, 2), "max_b": round(hi_b, 2),
                        "min_diff": round(lo_d, 2), "max_diff": round(hi_d, 2)
                    })

            return JSONResponse(content={
                "geojson": geojson,
                "center": center,
                "edge_count": len(features),
                "attributes": attrs,
                "bounds": bounds_info,
                "sim_a": sim_a,
                "sim_b": sim_b,
                "has_compare": sim_b is not None
            })

        except Exception as e:
            logger.error(f"Error comparing simulations: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return JSONResponse(content={"error": str(e)}, status_code=500)

    # ============================================
    # Replay API
    # ============================================

    @app.get("/api/simulation/progress")
    async def get_simulation_progress():
        """Get current simulation progress"""
        sims_dir = get_simulations_dir()
        if sims_dir:
            progress_file = sims_dir / ".sim_progress.json"
            if progress_file.exists():
                try:
                    data = json.loads(progress_file.read_text())
                    return JSONResponse(content=data)
                except Exception:
                    pass
        return JSONResponse(content={"percent": -1})

    @app.get("/api/tool/progress")
    async def get_tool_progress():
        """Get current tool execution progress"""
        output_dir = get_output_dir()
        if output_dir:
            progress_file = output_dir / ".tool_progress.json"
            if progress_file.exists():
                try:
                    return JSONResponse(content=json.loads(progress_file.read_text()))
                except Exception:
                    pass
        return JSONResponse(content={"tools": []})

    @app.get("/api/replay/list")
    async def list_replay_files():
        """List available replay files with DB description matching"""
        try:
            sims_dir = get_simulations_dir()
            if not sims_dir:
                return JSONResponse(content={"replays": [], "count": 0})

            # Load DB: map timestamp to description via tripinfo filenames
            timestamp_to_desc = {}
            try:
                output_dir = get_output_dir()
                if output_dir:
                    analysis_dir = output_dir / "analysis"
                    if analysis_dir.exists():
                        db_files = list(analysis_dir.glob("*.db"))
                        if db_files and db_files[0].stat().st_size > 0:
                            conn = sqlite3.connect(str(db_files[0]))
                            try:
                                rows = conn.execute(
                                    "SELECT simulation_id, description FROM simulations WHERE description IS NOT NULL"
                                ).fetchall()
                                sim_id_to_desc = {sid: desc for sid, desc in rows}
                            except Exception:
                                sim_id_to_desc = {}
                            conn.close()

                            # Match tripinfo files to simulation_ids via created_at order
                            # tripinfo files are named: {prefix}_tripinfo_{timestamp}.xml
                            # Replay files share the same timestamp
                            tripinfo_files = sorted(
                                sims_dir.glob("*_tripinfo_*.xml"),
                                key=lambda p: p.stat().st_mtime
                            )
                            sim_ids_ordered = sorted(
                                sim_id_to_desc.keys(),
                                key=lambda sid: sim_id_to_desc.get(sid, sid)
                            )
                            # Match by chronological order: DB created_at vs file mtime
                            try:
                                conn2 = sqlite3.connect(str(db_files[0]))
                                ordered_sims = conn2.execute(
                                    "SELECT simulation_id, description FROM simulations ORDER BY created_at"
                                ).fetchall()
                                conn2.close()
                                for i, tf in enumerate(tripinfo_files):
                                    # Extract timestamp from tripinfo filename
                                    tp = tf.name.split('_tripinfo_')
                                    if len(tp) >= 2:
                                        ts = tp[1].replace('.xml', '')
                                        if i < len(ordered_sims):
                                            timestamp_to_desc[ts] = ordered_sims[i][1] or ordered_sims[i][0]
                            except Exception:
                                pass
            except Exception:
                pass

            replays = []
            for f in sims_dir.iterdir():
                if f.is_file() and '_replay_' in f.name and f.name.endswith('.json'):
                    parts = f.name.split('_replay_')
                    if len(parts) >= 2:
                        base_name = parts[0]
                        timestamp = parts[1].replace('.json', '')
                        size_mb = f.stat().st_size / 1024 / 1024

                        # Match by shared timestamp between replay and tripinfo
                        display_name = timestamp_to_desc.get(timestamp, f"{base_name} ({timestamp})")

                        replays.append({
                            "name": display_name,
                            "base_name": base_name,
                            "filename": f.name,
                            "timestamp": timestamp,
                            "size_mb": round(size_mb, 1)
                        })

            return JSONResponse(content={
                "replays": sorted(replays, key=lambda x: x["timestamp"], reverse=True),
                "count": len(replays)
            })
        except Exception as e:
            logger.error(f"Error listing replay files: {e}")
            return JSONResponse(content={"error": str(e), "replays": []}, status_code=500)

    @app.get("/api/replay/{replay_filename}")
    async def get_replay_data(replay_filename: str):
        """Load replay file data for playback"""
        import json as _json
        try:
            replay_file = find_simulation_file(replay_filename)
            if replay_file is None:
                return JSONResponse(
                    content={"error": f"Replay file not found: {replay_filename}"},
                    status_code=404
                )

            logger.info(f"Loading replay: {replay_file.name} ({replay_file.stat().st_size / 1024 / 1024:.1f} MB)")

            with open(replay_file, 'r') as f:
                data = _json.load(f)

            return JSONResponse(content=data)

        except Exception as e:
            logger.error(f"Error loading replay file: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @app.get("/api/tls/{tls_filename}/parse")
    async def parse_tls_file(tls_filename: str):
        """
        Parse TLS (Traffic Light State) file and return state changes.

        Args:
            tls_filename: TLS switch output file name

        Returns:
            JSON with traffic light state changes indexed by time
        """
        try:
            tls_file = find_simulation_file(tls_filename)
            if tls_file is None:
                return JSONResponse(
                    content={"error": f"TLS file not found: {tls_filename}"},
                    status_code=404
                )

            logger.info(f"Parsing TLS file: {tls_file}")

            # Parse TLS switch output XML
            # Format: <tlsSwitch id="..." fromLane="..." toLane="..." begin="..." end="..." duration="..."/>
            # Or: <tlsState time="..." id="..." state="..."/>
            tls_states = {}  # time -> {tls_id: state}

            tree = ET.parse(str(tls_file))
            root = tree.getroot()

            # Try parsing as tlsStates (state at each timestep)
            for state_elem in root.findall('.//tlsState'):
                time_val = float(state_elem.get('time', 0))
                tls_id = state_elem.get('id', '')
                state = state_elem.get('state', '')

                time_key = round(time_val, 1)
                if time_key not in tls_states:
                    tls_states[time_key] = {}
                tls_states[time_key][tls_id] = state

            # Also try parsing as tlsSwitch (switch events)
            for switch_elem in root.findall('.//tlsSwitch'):
                time_val = float(switch_elem.get('begin', 0))
                tls_id = switch_elem.get('id', '')
                # For switches, we just record the event
                time_key = round(time_val, 1)
                if time_key not in tls_states:
                    tls_states[time_key] = {}
                # Mark as switch event
                tls_states[time_key][tls_id] = 'switch'

            logger.info(f"Parsed {len(tls_states)} timesteps with TLS data")

            return JSONResponse(content={
                "filename": tls_filename,
                "tls_states": tls_states,
                "total_timesteps": len(tls_states)
            })

        except Exception as e:
            logger.error(f"Error parsing TLS file: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @app.get("/api/network/{network_name}/traffic-lights")
    async def get_traffic_light_positions(network_name: str):
        """
        Get traffic light positions from network file.

        Returns junction coordinates for all traffic lights.
        """
        try:
            net_file = find_network_file(network_name)
            if net_file is None:
                return JSONResponse(
                    content={"error": f"Network not found: {network_name}"},
                    status_code=404
                )

            # Parse network
            net = sumolib.net.readNet(str(net_file))

            traffic_lights = []
            for tls in net.getTrafficLights():
                tls_id = tls.getID()
                # Get junction for this TLS
                # TLS ID often matches junction ID, or we find connected junctions
                try:
                    # Try to get the junction directly
                    junction = net.getNode(tls_id)
                    if junction:
                        x, y = junction.getCoord()
                        lon, lat = net.convertXY2LonLat(x, y)
                        traffic_lights.append({
                            "id": tls_id,
                            "lon": lon,
                            "lat": lat,
                            "type": junction.getType()
                        })
                except:
                    # If junction not found by TLS ID, find connected edges
                    connections = tls.getConnections()
                    if connections:
                        # Get position from first connection's incoming lane
                        conn = connections[0]
                        incoming_lane = conn[0]
                        edge = incoming_lane.getEdge()
                        to_node = edge.getToNode()
                        x, y = to_node.getCoord()
                        lon, lat = net.convertXY2LonLat(x, y)
                        traffic_lights.append({
                            "id": tls_id,
                            "lon": lon,
                            "lat": lat,
                            "type": "traffic_light"
                        })

            logger.info(f"Found {len(traffic_lights)} traffic lights in network")

            return JSONResponse(content={
                "network": network_name,
                "traffic_lights": traffic_lights,
                "count": len(traffic_lights)
            })

        except Exception as e:
            logger.error(f"Error getting traffic lights: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    # ============================================
    # SUMO GUI API
    # ============================================

    @app.get("/api/sumocfg/list")
    async def list_sumocfg_files():
        """List available SUMO configuration files"""
        try:
            sims_dir = get_simulations_dir()
            if not sims_dir:
                return JSONResponse(content={"configs": [], "count": 0})

            cfg_files = []
            for f in sims_dir.iterdir():
                if f.is_file() and f.name.endswith('.sumocfg'):
                    parts = f.name.replace('simulation_', '').replace('.sumocfg', '')
                    cfg_files.append({
                        "name": f.name,
                        "path": str(f),
                        "timestamp": parts,
                        "directory": str(sims_dir)
                    })

            return JSONResponse(content={
                "configs": sorted(cfg_files, key=lambda x: x["timestamp"], reverse=True),
                "count": len(cfg_files)
            })
        except Exception as e:
            logger.error(f"Error listing SUMO config files: {e}")
            return JSONResponse(content={"error": str(e), "configs": []}, status_code=500)

    @app.post("/api/upload/od")
    async def upload_od_file(file: UploadFile = File(...)):
        """Upload OD data CSV file"""
        try:
            upload_dir = AgentSUMOConfig.OUTPUT_DIR / "uploads" / "od"
            upload_dir.mkdir(parents=True, exist_ok=True)

            safe_name = filename(file.filename)
            if not safe_name.lower().endswith('.csv'):
                raise HTTPException(400, 'OD demand must be a CSV file')
            dest = contained(upload_dir, safe_name)
            if dest.exists():
                raise HTTPException(409, 'File already exists; rename before uploading')
            content = await file.read(30 * 1024 * 1024 + 1)
            if len(content) > 30 * 1024 * 1024:
                raise HTTPException(413, 'Maximum upload size is 30 MB')
            dest.write_bytes(content)

            logger.info(f"OD file uploaded: {file.filename} ({len(content)/1024:.1f} KB)")
            return JSONResponse(content={
                "status": "success",
                "filename": file.filename,
                "path": str(dest),
                "size": len(content)
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"OD upload failed: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @app.post("/api/upload/shapefile")
    async def upload_shapefile(files: list[UploadFile] = File(...)):
        """Upload shapefile (multiple files: .shp, .shx, .dbf, .prj, etc.)"""
        try:
            upload_dir = AgentSUMOConfig.OUTPUT_DIR / "uploads" / "shp"
            upload_dir.mkdir(parents=True, exist_ok=True)

            uploaded = []
            shp_path = None
            for f in files:
                safe_name = filename(f.filename)
                if Path(safe_name).suffix.lower() not in {'.shp', '.shx', '.dbf', '.prj', '.cpg'}:
                    raise HTTPException(400, 'Unsupported shapefile component')
                dest = contained(upload_dir, safe_name)
                if dest.exists():
                    raise HTTPException(409, 'File already exists; rename before uploading')
                content = await f.read(30 * 1024 * 1024 + 1)
                if len(content) > 30 * 1024 * 1024:
                    raise HTTPException(413, 'Maximum component size is 30 MB')
                dest.write_bytes(content)
                uploaded.append(f.filename)
                if f.filename.endswith(".shp"):
                    shp_path = str(dest)

            logger.info(f"Shapefile uploaded: {uploaded}")
            return JSONResponse(content={
                "status": "success",
                "files": uploaded,
                "shp_path": shp_path
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Shapefile upload failed: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @app.post("/api/clean")
    async def clean_output():
        """Clean output directory (same logic as clean.py)"""
        if _run_lock.locked() or _chat_lock.locked():
            raise HTTPException(409, "Wait for active operations to finish before cleaning outputs")
        try:
            output_dir = get_output_dir()
            if output_dir is None:
                return JSONResponse(content={"message": "No output directory", "deleted": 0})

            preserve_files = {"vehicle_types.add.xml"}
            clean_folders = ["networks", "trips", "simulations", "analysis", "reports", "visualizations", "additional"]

            deleted_count = 0
            preserved_count = 0

            for folder_name in clean_folders:
                folder = output_dir / folder_name
                if not folder.exists():
                    continue

                for entry in folder.iterdir():
                    if entry.name in preserve_files:
                        preserved_count += 1
                    elif entry.is_file():
                        entry.unlink()
                        deleted_count += 1
                    elif entry.is_dir():
                        import shutil
                        shutil.rmtree(entry)
                        deleted_count += 1

            # Also delete state file
            if output_dir:
                state_file = output_dir / ".agentsumo_state.json"
                if state_file.exists():
                    state_file.unlink()
                    deleted_count += 1

            logger.info(f"Clean output: {deleted_count} deleted, {preserved_count} preserved")

            return JSONResponse(content={
                "status": "success",
                "deleted": deleted_count,
                "preserved": preserved_count,
                "message": f"{deleted_count} files deleted, {preserved_count} preserved"
            })
        except Exception as e:
            logger.error(f"Error cleaning output: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @app.post("/api/reset-session")
    async def reset_session():
        """Reset entire session: clean output files + recreate AgentSUMO instance"""
        global _agent
        if _run_lock.locked() or _chat_lock.locked():
            raise HTTPException(409, "Wait for active operations to finish before resetting")
        try:
            # 1. Close existing agent (MCP connections)
            async with _agent_lock:
                if _agent:
                    await _agent.close()
                    _agent = None

            # 2. Clean output files
            output_dir = get_output_dir()
            deleted_count = 0
            if output_dir:
                preserve_files = {"vehicle_types.add.xml"}
                clean_folders = ["networks", "trips", "simulations", "analysis", "reports", "visualizations", "additional"]
                for folder_name in clean_folders:
                    folder = output_dir / folder_name
                    if not folder.exists():
                        continue
                    for entry in folder.iterdir():
                        if entry.name in preserve_files:
                            continue
                        elif entry.is_file():
                            entry.unlink()
                            deleted_count += 1
                        elif entry.is_dir():
                            import shutil
                            shutil.rmtree(entry)
                            deleted_count += 1

            # 3. Delete state file
            if output_dir:
                state_file = output_dir / ".agentsumo_state.json"
                if state_file.exists():
                    state_file.unlink()
                    logger.info("State file deleted")

            # 4. Recreate agent (will be lazy-created on next get_agent() call)
            logger.info(f"Session reset: {deleted_count} files deleted, agent will be recreated on next request")

            return JSONResponse(content={
                "status": "success",
                "deleted": deleted_count,
                "message": f"Session reset. {deleted_count} files deleted."
            })
        except Exception as e:
            logger.error(f"Error resetting session: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @app.post("/api/sumo-gui/open")
    async def open_sumo_gui(config_path: str = None, config_name: str = None):
        """
        Launch SUMO GUI with specified configuration file.

        Args:
            config_path: Full path to .sumocfg file (priority)
            config_name: Config filename to find in simulations directory

        Returns:
            JSON with status of the launch
        """
        try:
            from agentsumo_mcp.settings.settings import sumo_environment
            import platform

            # Find the config file
            cfg_file = None

            if config_path and Path(config_path).exists():
                cfg_file = Path(config_path)
            elif config_name:
                cfg_file = find_simulation_file(config_name)

            if cfg_file is None:
                return JSONResponse(
                    content={"error": f"Config file not found: {config_path or config_name}"},
                    status_code=404
                )

            # Launch sumo-gui

            if platform.system() == "Darwin":
                # macOS: SUMO GUI requires X11/XQuartz
                xquartz_app = "/Applications/Utilities/XQuartz.app"
                if not Path(xquartz_app).exists():
                    return JSONResponse(
                        content={
                            "error": "XQuartz is required to run SUMO GUI on macOS",
                            "solution": "Install XQuartz from https://www.xquartz.org/ and restart your Mac"
                        },
                        status_code=400
                    )

                # Start XQuartz if not running
                subprocess.Popen(
                    ["open", "-a", "XQuartz"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                # Give XQuartz time to start
                import time
                time.sleep(1.5)

                # Set DISPLAY for X11
                env = os.environ.copy()
                env["DISPLAY"] = ":0"

                # Get sumo-gui binary path
                sumo_gui_binary = sumo_environment.get_binary_path("sumo-gui")
                cmd = [sumo_gui_binary, "-c", str(cfg_file)]
                logger.info(f"Launching SUMO GUI with X11: {' '.join(cmd)}")
                process = subprocess.Popen(
                    cmd,
                    cwd=str(cfg_file.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    start_new_session=True
                )
            else:
                # Linux/Windows: direct execution
                sumo_gui_binary = sumo_environment.get_binary_path("sumo-gui")
                cmd = [sumo_gui_binary, "-c", str(cfg_file)]
                logger.info(f"Launching SUMO GUI: {' '.join(cmd)}")
                process = subprocess.Popen(
                    cmd,
                    cwd=str(cfg_file.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True
                )

            return JSONResponse(content={
                "status": "success",
                "message": f"SUMO GUI launched with {cfg_file.name}",
                "config_file": str(cfg_file),
                "pid": process.pid
            })

        except Exception as e:
            logger.error(f"Error launching SUMO GUI: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    # ============================================
    # WebSocket for Chat
    # ============================================

    @app.websocket("/ws/chat")
    async def websocket_chat(websocket: WebSocket):
        """WebSocket endpoint for chat"""
        origin = websocket.headers.get('origin')
        expected_origin = f'{"https" if websocket.url.scheme == "wss" else "http"}://{websocket.url.netloc}'
        if origin and origin != expected_origin:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        logger.info("WebSocket connection established")

        try:
            agent = None

            # Set up progress callback — writes to file for polling
            _progress_file = Path(get_output_dir() or "/tmp") / ".tool_progress.json"

            async def progress_callback(tool_name: str, status: str, message: str = None):
                """Write progress to file for frontend polling"""
                logger.info(f"📡 Progress: {tool_name} - {status}")
                try:
                    if tool_name == "__intermediate_text__":
                        # Send intermediate text directly via WS (best-effort)
                        try:
                            await websocket.send_json({"type": "intermediate", "data": message})
                        except Exception:
                            pass
                        return

                    # Read current progress state
                    tools = []
                    if _progress_file.exists():
                        try:
                            tools = json.loads(_progress_file.read_text()).get("tools", [])
                        except Exception:
                            tools = []

                    if status == "started":
                        tools.append({"tool": tool_name, "status": "started"})
                    elif status in ("completed", "error"):
                        # Update latest matching tool
                        for t in reversed(tools):
                            if t["tool"] == tool_name and t["status"] == "started":
                                t["status"] = status
                                break

                    _progress_file.write_text(json.dumps({"tools": tools}))
                except Exception as e:
                    logger.warning(f"Failed to write progress: {e}")

            # Initialize the provider only when a chat message is actually sent.

            # Send initial state
            await websocket.send_json({
                "type": "state",
                "data": {"assistant": "Awaiting message"}
            })

            while True:
                # Receive message
                data = await websocket.receive_json()
                msg_type = data.get("type", "chat")

                if msg_type == "chat":
                    user_message = data.get("message", "")

                    if not user_message:
                        continue

                    if not readiness()['ai']['configured']:
                        key = readiness()['ai']['required_variable']
                        await websocket.send_json({'type': 'error', 'data': f'Configure {key} in .env and restart the server to enable AI chat.'})
                        await websocket.send_json({'type': 'thinking', 'data': False})
                        continue

                    # Send "thinking" indicator
                    await websocket.send_json({
                        "type": "thinking",
                        "data": True
                    })

                    try:
                        # Get response from agent
                        async with _chat_lock:
                            agent = await asyncio.wait_for(get_agent(), timeout=45)
                            agent.set_progress_callback(progress_callback)
                            response = await asyncio.wait_for(agent.chat(user_message), timeout=600)

                        # Send response
                        await websocket.send_json({
                            "type": "response",
                            "data": response
                        })

                        # Send updated state
                        await websocket.send_json({
                            "type": "state",
                            "data": agent.get_state()
                        })

                    except Exception as e:
                        await websocket.send_json({
                            "type": "error",
                            "data": str(e)
                        })

                    finally:
                        # Clean up tool progress file
                        try:
                            _progress_file.unlink(missing_ok=True)
                        except Exception:
                            pass
                        await websocket.send_json({
                            "type": "thinking",
                            "data": False
                        })

                elif msg_type == "reset":
                    if agent:
                        agent.reset_conversation()
                    await websocket.send_json({
                        "type": "state",
                        "data": agent.get_state() if agent else {}
                    })
                    await websocket.send_json({
                        "type": "info",
                        "data": "Conversation reset"
                    })

        except WebSocketDisconnect:
            logger.info("WebSocket disconnected")

        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            try:
                await websocket.send_json({"type": "error", "data": "Assistant connection failed. Check provider configuration and server logs."})
            except Exception:
                pass

    return app


# For running with uvicorn
app = create_app()
