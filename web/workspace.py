"""Local workspace readiness, validated imports and nonblocking SUMO jobs."""
import asyncio
import json
import math
import os
import shutil
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import xml.etree.ElementTree as ET

import sumolib
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from agentsumo.core import AgentSUMOConfig as Config

router = APIRouter(prefix="/api")
_run_lock = asyncio.Lock()
_tasks = set()


def contained(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    if target == root.resolve() or not target.is_relative_to(root.resolve()):
        raise HTTPException(400, "Path must stay inside the workspace")
    return target


def filename(name: str) -> str:
    if not name or Path(name).name != name or any(c in name for c in '/\\:') or name.startswith('.'):
        raise HTTPException(400, "Use a plain filename without directories")
    return name


def network_geojson(path: Path):
    try:
        net = sumolib.net.readNet(str(path))
    except Exception as exc:
        raise ValueError(f"Invalid SUMO network: {exc}") from exc
    if not net.hasGeoProj():
        raise ValueError("Network has no geographic projection. Import a georeferenced SUMO network; local XY coordinates cannot be placed on Earth.")
    features, points = [], []
    for edge in net.getEdges():
        coords = [list(net.convertXY2LonLat(x, y)) for x, y in edge.getShape()]
        if len(coords) < 2:
            continue
        if any(not all(math.isfinite(v) for v in p) or not -180 <= p[0] <= 180 or not -90 <= p[1] <= 90 for p in coords):
            raise ValueError("Network contains invalid longitude/latitude coordinates")
        points.extend(coords)
        features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                         "properties": {"id": edge.getID(), "name": edge.getName() or edge.getID(),
                                        "speed": edge.getSpeed(), "lanes": edge.getLaneNumber(),
                                        "length": edge.getLength(), "type": edge.getType()}})
    if not points:
        raise ValueError("Network contains no usable road geometry")
    west, south = min(p[0] for p in points), min(p[1] for p in points)
    east, north = max(p[0] for p in points), max(p[1] for p in points)
    return {"geojson": {"type": "FeatureCollection", "features": features},
            "bounds": [[west, south], [east, north]], "center": {"lon": (west+east)/2, "lat": (south+north)/2},
            "edge_count": len(features), "crs": "EPSG:4326", "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}


def readiness():
    from agentsumo_mcp.settings.settings import sumo_environment
    executable = sumo_environment.get_binary_path("sumo")
    provider = Config.get_llm_provider()
    key_name = "GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY"
    ai = bool(os.environ.get(key_name)) or (provider == "anthropic" and Config.CLAUDE_API_KEY_FILE.exists())
    try:
        has_mapbox = bool(Config.get_mapbox_token())
    except RuntimeError:
        has_mapbox = False
    return {"server": "ready", "simulation": {"ready": Path(executable).is_file(), "busy": _run_lock.locked()},
            "ai": {"configured": ai, "enabled": ai, "mode": "provider" if ai else "unconfigured", "provider": provider, "required_variable": None if ai else key_name,
                   "note": "Credentials configured; provider connectivity is checked when sending a message"},
            "map": {"provider": "mapbox" if has_mapbox else "openstreetmap",
                    "requires_token": False}, "database": {"available": any((Config.OUTPUT_DIR / "analysis").glob("*.db"))}}


@router.get("/health")
def health():
    return readiness()


@router.post("/upload/asset")
async def upload_asset(file: UploadFile = File(...)):
    name = filename(file.filename)
    kind = "networks" if name.endswith('.net.xml') else "trips" if name.endswith(('.rou.xml', '.trips.xml')) else None
    if kind is None:
        raise HTTPException(400, "Supported assets: .net.xml, .rou.xml, .trips.xml")
    root = Config.OUTPUT_DIR / kind
    root.mkdir(parents=True, exist_ok=True)
    dest = contained(root, name)
    if dest.exists():
        raise HTTPException(409, "This filename already exists; rename the file before importing")
    content = await file.read(30 * 1024 * 1024 + 1)
    if len(content) > 30 * 1024 * 1024:
        raise HTTPException(413, "Maximum asset size is 30 MB")
    if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
        raise HTTPException(400, "XML entities and document types are not supported")
    temp = root / f'.import-{uuid4().hex}.xml'
    try:
        document = ET.fromstring(content)
        if document.tag != ('net' if kind == 'networks' else 'routes'):
            raise ValueError("Incorrect SUMO XML root element")
        temp.write_bytes(content)
        if kind == 'networks':
            await asyncio.to_thread(network_geojson, temp)
        temp.rename(dest)
        return {"filename": name, "kind": kind}
    except (ValueError, ET.ParseError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        temp.unlink(missing_ok=True)


@router.get("/scenario/assets")
def scenario_assets():
    return {"networks": sorted(p.name for p in (Config.OUTPUT_DIR / 'networks').glob('*.net.xml')),
            "demand": sorted(p.name for p in (Config.OUTPUT_DIR / 'trips').glob('*.xml') if p.name.endswith(('.rou.xml', '.trips.xml')))}


class RunRequest(BaseModel):
    network: str
    demand: str
    duration: int = Field(default=300, ge=30, le=7200)
    label: str = Field(default='Road baseline', min_length=1, max_length=80)


def jobs_dir():
    root = Config.OUTPUT_DIR / 'jobs'
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_job(job):
    root = jobs_dir()
    temp = root / f'.{job["id"]}.tmp'
    temp.write_text(json.dumps(job, indent=2), encoding='utf-8')
    temp.replace(root / f'{job["id"]}.json')


@router.get("/simulation/jobs")
def list_jobs():
    jobs = []
    for path in jobs_dir().glob('*.json'):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
            if job['status'] in ('running', 'queued') and not _run_lock.locked():
                job.update(status='interrupted', error='Server restarted before this job completed')
            simulations = Config.OUTPUT_DIR / 'simulations'
            job['replay_available'] = bool(job.get('replay')) and (simulations / Path(job.get('replay', '')).name).is_file()
            job['heatmap_available'] = bool(job.get('netstate')) and (simulations / Path(job.get('netstate', '')).name).is_file()
            job['analysis_available'] = False
            for database in (Config.OUTPUT_DIR / 'analysis').glob('*.db'):
                try:
                    with sqlite3.connect(database) as conn:
                        if conn.execute('SELECT 1 FROM simulations WHERE simulation_id=?', (job.get('simulation_id',job['id']),)).fetchone():
                            job['analysis_available'] = True
                            break
                except sqlite3.Error:
                    continue
            jobs.append(job)
        except (ValueError, OSError):
            continue
    return {"jobs": sorted(jobs, key=lambda j: j['created_at'], reverse=True)}


async def execute_job(job, network, demand):
    try:
        async with _run_lock:
            job.update(status='running')
            save_job(job)
            from agentsumo_mcp.baseline.simulation_runner import SimulationRunner
            from agentsumo_mcp.analysis.xml_to_sqlite import XMLToSQLiteConverter
            runner = SimulationRunner()
            kwargs = {"net_file": str(network), "duration": job['duration'], "output_dir": str(Config.OUTPUT_DIR / 'simulations')}
            kwargs['route_file' if demand.name.endswith('.rou.xml') else 'trip_file'] = str(demand)
            result = await asyncio.to_thread(runner.run, **kwargs)
            if result['status'] != 'success':
                raise RuntimeError(result.get('message', 'SUMO failed'))
            files = result['output_files']
            (Config.OUTPUT_DIR / 'analysis').mkdir(exist_ok=True)
            analysis = await asyncio.to_thread(XMLToSQLiteConverter().convert_simulation_results,
                *files, str(Config.OUTPUT_DIR / 'analysis' / 'workspace.db'), simulation_id=job['id'],
                net_file=str(network), route_file=str(demand), description=job['network'], summary_xml=result.get('summary_xml'))
            job.update(status='completed', replay=result['metadata'].get('replay_file'),
                       netstate=Path(files[1]).name, simulation_id=job['id'], analysis_status=analysis.get('status'))
            if analysis.get('status') not in ('success', 'skipped'):
                job['analysis_error'] = analysis.get('message', 'Analysis import failed; raw SUMO outputs remain available')
    except Exception as exc:
        job.update(status='failed', error=str(exc))
    finally:
        job['finished_at'] = datetime.now(timezone.utc).isoformat()
        save_job(job)


@router.post("/simulation/jobs", status_code=202)
async def start_job(request: RunRequest):
    if _run_lock.locked() or any(not t.done() for t in _tasks):
        raise HTTPException(409, 'Another simulation is running; wait for it to finish')
    if not readiness()['simulation']['ready']:
        raise HTTPException(503, 'SUMO executable is unavailable. Configure SUMO_HOME or add sumo to PATH.')
    network = contained(Config.OUTPUT_DIR / 'networks', filename(request.network))
    demand = contained(Config.OUTPUT_DIR / 'trips', filename(request.demand))
    if not network.is_file() or not demand.is_file():
        raise HTTPException(404, 'Select an existing network and demand file')
    if not network.name.endswith('.net.xml') or not demand.name.endswith(('.rou.xml', '.trips.xml')):
        raise HTTPException(400, 'Unsupported scenario file type')
    try:
        await asyncio.to_thread(network_geojson, network)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    job = {"id": f'run_{uuid4().hex[:12]}', **request.model_dump(), "status": "queued",
           "created_at": datetime.now(timezone.utc).isoformat()}
    save_job(job)
    task = asyncio.create_task(execute_job(job, network, demand))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job
