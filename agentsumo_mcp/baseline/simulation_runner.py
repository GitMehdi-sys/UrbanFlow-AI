"""
Simulation execution functionality for SUMO MCP Server.

Uses TraCI for simulation execution with replay frame capture.
"""

import os
import json
import time
import datetime
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List
from xml.etree.ElementTree import Element, SubElement, ElementTree, parse

from agentsumo_mcp.settings.settings import sumo_environment, simulation_settings
from agentsumo_mcp.utils.defaults import ensure_vehicle_types_default
from agentsumo_mcp.utils.file_utils import copy_to_workdir, copy_additional_files
from agentsumo_mcp.utils.path_utils import get_output_path, get_working_directory

logger = logging.getLogger(__name__)
_simulation_lock = threading.Lock()


class SimulationRunner:
    """Simulation execution class for SUMO simulations."""

    def __init__(self):
        self.environment = sumo_environment
        self.sim_settings = simulation_settings

    def run(self, *args, **kwargs):
        """Only one TraCI connection may own the default process at a time."""
        if not _simulation_lock.acquire(blocking=False):
            return {"status": "error", "output_files": [], "message": "A simulation is already running in this process", "simulation_time": 0}
        try:
            return self._run_exclusive(*args, **kwargs)
        finally:
            _simulation_lock.release()

    def _run_exclusive(
        self,
        net_file: str,
        trip_file: Optional[str] = None,
        route_file: Optional[str] = None,
        duration: int = 3600,
        output_dir: str = "output/simulations",
        additional_files: Optional[List[str]] = None,
        policy_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Run SUMO simulation using TraCI with replay frame capture.

        Args:
            net_file: Network file path
            trip_file: Trip file path (optional)
            route_file: Route file path (optional)
            duration: Simulation duration in seconds
            output_dir: Output directory for simulation results
            additional_files: List of additional XML files to include
            policy_type: Policy type for batch simulation file naming

        Returns:
            Dict[str, Any]: Simulation result with status and output files
        """
        try:
            # Setup working directory and paths
            base_dir = get_working_directory()
            output_path = get_output_path(output_dir)

            # SUMO loads vehicle_types.add.xml as an additional file (see the
            # additional_files_list assembled in _generate_config_file). Make
            # sure a default copy exists in the simulation directory so a
            # fresh install can run end-to-end without manual setup.
            ensure_vehicle_types_default(output_path)

            if not Path(net_file).is_absolute():
                net_file = str(base_dir / net_file)

            # Handle additional files
            if policy_type == "baseline":
                additional_files = []
            else:
                additional_files = additional_files or []

            # Generate unique output file names
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            base_name = Path(net_file).stem if net_file else "simulation"
            prefix = base_name

            output_files = self._generate_output_filenames(prefix, timestamp)

            # Copy input files to working directory
            file_names = self._copy_input_files(
                net_file, trip_file, route_file, output_path
            )

            # Copy additional files
            additional_file_names = copy_additional_files(
                additional_files, output_path, None
            )

            # Check if any additional file contains a rerouter
            has_rerouter = self._has_rerouter(additional_files)

            # Validate user-provided additional files before simulation
            validation_errors = self._validate_additional_files(additional_files, net_file, duration)
            if validation_errors:
                return {
                    "status": "validation_error",
                    "output_files": [],
                    "message": "Additional file validation failed. Fix the errors and retry.\n\n"
                               + "\n".join(f"- {e}" for e in validation_errors),
                    "errors": validation_errors,
                    "simulation_time": 0.0,
                    "metadata": {"error_type": "AdditionalFileValidationError"}
                }

            # Generate additional.add.xml
            add_file = self._generate_additional_file(output_path, output_files, duration)

            # Generate SUMO configuration
            config_file = self._generate_config_file(
                output_path, file_names, additional_file_names, add_file,
                output_files, duration, timestamp
            )

            # Run simulation via TraCI
            result = self._run_simulation_traci(
                config_file, output_path, output_files, duration, prefix, timestamp,
                enable_rerouting=has_rerouter
            )

            # Prepare result
            return self._prepare_result(
                result, output_path, output_files, base_dir, duration
            )

        except Exception as e:
            return {
                "status": "error",
                "output_files": [],
                "message": f"Simulation failed: {str(e)}",
                "simulation_time": 0.0,
                "metadata": {"error_type": type(e).__name__}
            }

    def _generate_output_filenames(self, prefix: str, timestamp: str) -> Dict[str, str]:
        """Generate output filenames for simulation results."""
        return {
            "tripinfo": f"{prefix}_tripinfo_{timestamp}.xml",
            "summary": f"{prefix}_summary_{timestamp}.xml",
            "emission": f"{prefix}_emission_{timestamp}.xml",
            "netstate_dump": f"{prefix}_netstate_{timestamp}.xml",
            "netstate_dump_emission": f"{prefix}_netstate_emission_{timestamp}.xml",
        }

    def _copy_input_files(
        self, net_file: str, trip_file: Optional[str], route_file: Optional[str],
        output_path: Path
    ) -> Dict[str, str]:
        """Copy input files to working directory."""
        net_file_name = copy_to_workdir(net_file, output_path)
        trip_file_name = copy_to_workdir(trip_file, output_path) if trip_file else None
        route_file_name = copy_to_workdir(route_file, output_path) if route_file else None

        return {
            "net_file": net_file_name,
            "trip_file": trip_file_name,
            "route_file": route_file_name
        }

    @staticmethod
    def _validate_additional_files(
        additional_files: List[str], net_file: str, duration: int
    ) -> List[str]:
        """
        Validate user-provided additional files before simulation.

        Level 1: XML structure (well-formed, correct root, required attributes)
        Level 2: Network consistency (edge/lane IDs exist in the network)

        Returns:
            List of error messages. Empty list means validation passed.
        """
        import xml.etree.ElementTree as _ET

        if not additional_files:
            return []

        errors = []

        # Build edge/lane lookup from network file
        edge_ids = set()
        edge_lane_counts = {}  # edge_id → num_lanes
        if net_file and Path(net_file).exists():
            try:
                import sumolib
                net = sumolib.net.readNet(net_file)
                for edge in net.getEdges():
                    eid = edge.getID()
                    edge_ids.add(eid)
                    edge_lane_counts[eid] = edge.getLaneNumber()
            except Exception as e:
                logger.warning(f"Could not read network for validation: {e}")

        for file_path in additional_files:
            fname = Path(file_path).name

            # --- Level 1: XML structure ---
            try:
                tree = _ET.parse(file_path)
                root = tree.getroot()
            except _ET.ParseError as e:
                errors.append(f"[{fname}] Invalid XML: {e}")
                continue

            if root.tag != "additional":
                errors.append(f"[{fname}] Root element must be <additional>, got <{root.tag}>")
                continue

            # Validate rerouter elements
            for rerouter in root.findall("rerouter"):
                rid = rerouter.get("id")
                if not rid:
                    errors.append(f"[{fname}] <rerouter> missing required 'id' attribute")
                edges_attr = rerouter.get("edges")
                if not edges_attr:
                    errors.append(f"[{fname}] <rerouter id='{rid}'> missing required 'edges' attribute")
                else:
                    # SUMO requires space-separated edge IDs; semicolons cause fatal error
                    if ";" in edges_attr:
                        errors.append(
                            f"[{fname}] <rerouter id='{rid}'> edges attribute uses semicolons — "
                            f"SUMO requires SPACE-separated edge IDs. "
                            f"Replace ';' with ' ' in: edges=\"{edges_attr}\""
                        )
                    # Level 2: Check edge IDs exist
                    if edge_ids:
                        import re
                        for eid in re.split(r'[;\s]+', edges_attr):
                            eid = eid.strip()
                            if eid and eid not in edge_ids:
                                errors.append(
                                    f"[{fname}] <rerouter id='{rid}'> references non-existent edge '{eid}'"
                                )

                for interval in rerouter.findall("interval"):
                    begin = interval.get("begin")
                    end = interval.get("end")
                    if begin is None or end is None:
                        errors.append(f"[{fname}] <interval> in rerouter '{rid}' missing begin/end")
                        continue
                    try:
                        b, e = float(begin), float(end)
                        if b < 0:
                            errors.append(f"[{fname}] interval begin={b} is negative")
                        if e > duration:
                            errors.append(
                                f"[{fname}] interval end={e} exceeds simulation duration ({duration}s)"
                            )
                    except ValueError:
                        errors.append(f"[{fname}] interval begin/end must be numeric")

                    for cr in interval.findall("closingReroute"):
                        cr_id = cr.get("id")
                        if not cr_id:
                            errors.append(f"[{fname}] <closingReroute> missing 'id' attribute")
                        elif edge_ids and cr_id not in edge_ids:
                            errors.append(
                                f"[{fname}] <closingReroute> references non-existent edge '{cr_id}'"
                            )

                    for clr in interval.findall("closingLaneReroute"):
                        lane_id = clr.get("id")
                        if not lane_id:
                            errors.append(f"[{fname}] <closingLaneReroute> missing 'id' attribute")
                        elif edge_ids:
                            # Lane ID format: {edge_id}_{index}
                            parts = lane_id.rsplit("_", 1)
                            if len(parts) != 2:
                                errors.append(
                                    f"[{fname}] Invalid lane ID format '{lane_id}' "
                                    f"(expected '{{edge_id}}_{{index}}')"
                                )
                            else:
                                edge_part, idx_part = parts
                                if edge_part not in edge_ids:
                                    errors.append(
                                        f"[{fname}] <closingLaneReroute> references "
                                        f"non-existent edge '{edge_part}'"
                                    )
                                elif edge_part in edge_lane_counts:
                                    try:
                                        idx = int(idx_part)
                                        if idx >= edge_lane_counts[edge_part]:
                                            errors.append(
                                                f"[{fname}] Lane index {idx} exceeds lane count "
                                                f"({edge_lane_counts[edge_part]}) for edge '{edge_part}'"
                                            )
                                    except ValueError:
                                        errors.append(
                                            f"[{fname}] Invalid lane index '{idx_part}' in '{lane_id}'"
                                        )

            # Validate variableSpeedSign elements
            for vss in root.findall("variableSpeedSign"):
                vss_id = vss.get("id")
                if not vss_id:
                    errors.append(f"[{fname}] <variableSpeedSign> missing required 'id' attribute")
                lanes_attr = vss.get("lanes")
                if not lanes_attr:
                    errors.append(f"[{fname}] <variableSpeedSign id='{vss_id}'> missing required 'lanes' attribute")
                else:
                    # Level 2: Check lane IDs
                    if edge_ids:
                        for lane_id in lanes_attr.split():
                            parts = lane_id.rsplit("_", 1)
                            if len(parts) != 2:
                                errors.append(
                                    f"[{fname}] Invalid lane ID '{lane_id}' in VSS '{vss_id}'"
                                )
                            else:
                                edge_part, idx_part = parts
                                if edge_part not in edge_ids:
                                    errors.append(
                                        f"[{fname}] VSS '{vss_id}' references "
                                        f"non-existent edge '{edge_part}'"
                                    )
                                elif edge_part in edge_lane_counts:
                                    try:
                                        idx = int(idx_part)
                                        if idx >= edge_lane_counts[edge_part]:
                                            errors.append(
                                                f"[{fname}] Lane index {idx} exceeds lane count "
                                                f"({edge_lane_counts[edge_part]}) for edge '{edge_part}'"
                                            )
                                    except ValueError:
                                        errors.append(
                                            f"[{fname}] Invalid lane index '{idx_part}' in '{lane_id}'"
                                        )

                for step in vss.findall("step"):
                    time_val = step.get("time")
                    if time_val is None:
                        errors.append(f"[{fname}] <step> in VSS '{vss_id}' missing 'time' attribute")
                    else:
                        try:
                            t = float(time_val)
                            if t < 0:
                                errors.append(f"[{fname}] step time={t} is negative in VSS '{vss_id}'")
                        except ValueError:
                            errors.append(f"[{fname}] step time must be numeric in VSS '{vss_id}'")

                    speed_val = step.get("speed")
                    if speed_val is not None:
                        try:
                            s = float(speed_val)
                            if s > 100:
                                errors.append(
                                    f"[{fname}] VSS '{vss_id}' speed={s} m/s ({s*3.6:.0f} km/h) "
                                    f"is unusually high — did you use km/h instead of m/s?"
                                )
                        except ValueError:
                            errors.append(f"[{fname}] step speed must be numeric in VSS '{vss_id}'")

        return errors

    @staticmethod
    def _has_rerouter(additional_files: List[str]) -> bool:
        """Check if any additional file contains a rerouter element."""
        import xml.etree.ElementTree as ET
        for f in (additional_files or []):
            try:
                tree = ET.parse(f)
                if tree.find('.//rerouter') is not None:
                    return True
            except Exception:
                continue
        return False

    def _generate_additional_file(self, output_path: Path, output_files: Dict[str, str], duration: int) -> str:
        """Generate additional.add.xml file for simulation."""
        add_file = output_files['netstate_dump'].replace('.xml', '.add.xml')
        add_path = output_path / add_file

        add_root = Element("additional")
        SubElement(add_root, "edgeData", {
            "id": "dump_output",
            "freq": str(duration),
            "file": output_files["netstate_dump"],
        })
        SubElement(add_root, "edgeData", {
            "id": "dump_output_emission",
            "freq": str(duration),
            "file": output_files["netstate_dump_emission"],
            "type": "emissions",
            "excludeEmpty": "true",
        })

        ElementTree(add_root).write(add_path, encoding="utf-8", xml_declaration=True)
        return add_file

    def _generate_config_file(
        self, output_path: Path, file_names: Dict[str, str],
        additional_file_names: List[str], add_file: str,
        output_files: Dict[str, str], duration: int, timestamp: str
    ) -> Path:
        """Generate SUMO configuration file."""
        config_file = output_path / f"simulation_{timestamp}.sumocfg"
        cfg_root = Element("configuration")

        # Input section
        input_tag = SubElement(cfg_root, "input")
        SubElement(input_tag, "net-file", {"value": file_names["net_file"]})

        if file_names["route_file"]:
            SubElement(input_tag, "route-files", {"value": file_names["route_file"]})
        elif file_names["trip_file"]:
            SubElement(input_tag, "trip-files", {"value": file_names["trip_file"]})

        # Additional files
        # Scenario-defined vehicle types take precedence over bundled defaults.
        defined = set()
        for name in [file_names.get('route_file'), file_names.get('trip_file'), *additional_file_names]:
            if name:
                for element in parse(output_path / name).getroot().iter():
                    if element.tag in ('vType', 'vTypeDistribution') and element.get('id'):
                        defined.add(element.get('id'))
        defaults = parse(output_path / 'vehicle_types.add.xml')
        for element in list(defaults.getroot()):
            if element.get('id') in defined:
                defaults.getroot().remove(element)
        default_name = f'vehicle_types_{timestamp}.add.xml'
        defaults.write(output_path / default_name, encoding='utf-8', xml_declaration=True)
        additional_files_list = [add_file, default_name]
        additional_files_list.extend(additional_file_names)
        SubElement(input_tag, "additional-files", {
            "value": ",".join(additional_files_list)
        })

        # Time section
        time_tag = SubElement(cfg_root, "time")
        SubElement(time_tag, "begin", {"value": "0"})
        SubElement(time_tag, "end", {"value": str(duration)})

        # Output section
        output_tag = SubElement(cfg_root, "output")
        SubElement(output_tag, "tripinfo-output", {"value": output_files["tripinfo"]})
        SubElement(output_tag, "summary-output", {"value": output_files["summary"]})

        # Report section
        report_tag = SubElement(cfg_root, "report")
        SubElement(report_tag, "verbose", {"value": "true"})
        SubElement(report_tag, "no-step-log", {"value": "true"})

        ElementTree(cfg_root).write(config_file, encoding="utf-8", xml_declaration=True)
        return config_file

    def _run_simulation_traci(
        self,
        config_file: Path,
        output_path: Path,
        output_files: Dict[str, str],
        duration: int = 3600,
        prefix: str = "simulation",
        timestamp: str = "",
        enable_rerouting: bool = False
    ) -> Any:
        """
        Run SUMO simulation using TraCI.
        Captures vehicle position frames for post-simulation replay.
        """
        import traci

        start_time = time.time()
        sumo_binary = self.environment.get_binary_path("sumo")

        sumo_cmd = [
            sumo_binary,
            "-c", str(config_file),
            "--device.emissions.probability", "1.0",
            "--device.rerouting.probability", "1.0",
            "--no-warnings"
        ]

        if enable_rerouting:
            sumo_cmd.append("--ignore-route-errors")
            logger.info("Rerouter detected — ignore-route-errors enabled")

        logger.info(f"Running SUMO simulation (TraCI): {config_file.name}")

        try:
            # Set SUMO_HOME for TraCI
            sumo_home = self.environment.SUMO_HOME
            if sumo_home:
                os.environ["SUMO_HOME"] = sumo_home

            traci.start(sumo_cmd, traceFile=None, label="agentsumo")

            step_count = 0
            replay_interval = 5  # Capture frame every N steps
            wall_timeout = max(duration * 2, 600)
            replay_frames = []  # Accumulate frames in memory
            progress_file = output_path / ".sim_progress.json"
            progress_interval = 10  # Write progress every N steps
            logger.info(f"Progress file path: {progress_file}, output_path exists: {output_path.exists()}")

            while traci.simulation.getMinExpectedNumber() > 0:
                # Enforce simulation duration limit
                sim_time = traci.simulation.getTime()
                if sim_time >= duration:
                    logger.info(f"Duration limit reached ({duration}s at sim_time={sim_time:.0f}s), stopping simulation")
                    break

                # Wall-clock safety net
                elapsed_wall = time.time() - start_time
                if elapsed_wall >= wall_timeout:
                    logger.warning(f"Wall-clock timeout ({wall_timeout}s) reached, forcing stop")
                    raise TimeoutError(f"SUMO exceeded wall-clock timeout ({wall_timeout}s)")

                traci.simulationStep()
                step_count += 1

                # Write progress file periodically
                if step_count % progress_interval == 0:
                    try:
                        pct = min(round(sim_time / duration * 100), 99)
                        progress_file.write_text(json.dumps({"percent": pct}))
                        if step_count == progress_interval:
                            logger.info(f"Progress file created: {progress_file} ({pct}%)")
                    except Exception as e:
                        logger.warning(f"Failed to write progress file: {e}")

                # Capture frame for replay
                if step_count % replay_interval == 0:
                    try:
                        vehicles = []
                        for vid in traci.vehicle.getIDList():
                            pos = traci.vehicle.getPosition(vid)
                            geo = traci.simulation.convertGeo(*pos)
                            vehicles.append({
                                'id': vid,
                                'lon': round(geo[0], 6),
                                'lat': round(geo[1], 6),
                                'speed': round(traci.vehicle.getSpeed(vid), 1),
                                'angle': round(traci.vehicle.getAngle(vid), 1)
                            })

                        replay_frames.append({
                            'time': traci.simulation.getTime(),
                            'vehicles': vehicles,
                            'vehicle_count': len(vehicles)
                        })

                    except Exception as e:
                        logger.debug(f"Frame capture error (ignored): {e}")

            elapsed = time.time() - start_time
            logger.info(f"Simulation completed in {elapsed:.2f} seconds ({step_count} steps, {len(replay_frames)} frames captured)")

            # Clean up progress file
            try:
                progress_file.unlink(missing_ok=True)
            except Exception:
                pass

            traci.close()

            # Save replay file
            replay_filename = f"{prefix}_replay_{timestamp}.json"
            replay_path = output_path / replay_filename
            with open(replay_path, 'w') as f:
                json.dump({
                    'duration': duration,
                    'frame_interval': replay_interval,
                    'total_frames': len(replay_frames),
                    'frames': replay_frames
                }, f)
            logger.info(f"Replay saved: {replay_filename} ({replay_path.stat().st_size / 1024 / 1024:.1f} MB)")

            # Create result-like object
            class TraCIResult:
                returncode = 0
                stdout = ""
                stderr = ""

            result = TraCIResult()
            result.elapsed = elapsed
            result.replay_file = replay_filename
            return result

        except Exception as e:
            try:
                traci.close()
            except Exception:
                pass

            elapsed = time.time() - start_time
            logger.error(f"TraCI simulation failed: {e}")

            class ErrorResult:
                returncode = 1
                stdout = ""
                stderr = str(e)

            result = ErrorResult()
            result.elapsed = elapsed
            result.replay_file = None
            return result

    def _prepare_result(
        self, result: Any, output_path: Path,
        output_files: Dict[str, str], base_dir: Path, duration: int
    ) -> Dict[str, Any]:
        """Prepare simulation result."""
        tripinfo_abs = str(output_path / output_files["tripinfo"])
        edgedata_abs = str(output_path / output_files["netstate_dump"])
        edgedata_emission_abs = str(output_path / output_files["netstate_dump_emission"])
        summary_abs = str(output_path / output_files["summary"])

        replay_file = getattr(result, 'replay_file', None)

        if result.returncode != 0:
            return {
                "status": "error",
                "output_files": [],
                "summary_xml": summary_abs,
                "message": f"Simulation failed after {result.elapsed:.2f} seconds\n{result.stderr}",
                "simulation_time": result.elapsed,
                "metadata": {
                    "working_directory": str(base_dir),
                    "absolute_tripinfo": tripinfo_abs,
                    "absolute_edgedata": edgedata_abs,
                    "absolute_edgedata_emission": edgedata_emission_abs,
                    "absolute_summary": summary_abs,
                    "return_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "replay_file": replay_file
                }
            }

        if not all(Path(p).is_file() and Path(p).stat().st_size > 0 for p in (tripinfo_abs, edgedata_abs, edgedata_emission_abs)):
            return {"status": "error", "output_files": [], "message": "SUMO produced no usable output files", "simulation_time": result.elapsed}
        return {
            "status": "success",
            "output_files": [tripinfo_abs, edgedata_abs, edgedata_emission_abs],
            "summary_xml": summary_abs,
            "message": f"Simulation executed successfully in {result.elapsed:.2f} seconds.",
            "simulation_time": result.elapsed,
            "metadata": {
                "working_directory": str(base_dir),
                "absolute_tripinfo": tripinfo_abs,
                "absolute_edgedata": edgedata_abs,
                "absolute_edgedata_emission": edgedata_emission_abs,
                "absolute_summary": summary_abs,
                "return_code": result.returncode,
                "replay_file": replay_file
            }
        }
