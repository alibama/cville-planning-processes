"""
cville_core.py — engine + data layer for the Charlottesville permit navigator.

No Streamlit dependency so it can be unit-tested and reused headless.

Pieces:
  * DmnDecision        — load + evaluate a single-decision DMN via SpiffWorkflow
  * PermitNavigator    — wires the selector DMN + authority-matrix DMN together,
                         composes multi-overlay results, extracts the spine's
                         intake steps from the BPMN.
  * Overlay providers  — Manual / ArcGIS (live, stdlib urllib) / LocalGeoJSON.
                         All return a normalized OverlayResult.

Stdlib only except SpiffWorkflow (required) and shapely (optional, local GeoJSON).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import urlopen

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

# Overlay categories the selector DMN understands.
OVERLAY_CATEGORIES = ["ADC", "HC", "IndividuallyProtected", "EntranceCorridor"]


# --------------------------------------------------------------------------- #
#  DMN evaluation (SpiffWorkflow)                                             #
# --------------------------------------------------------------------------- #
def _make_dmn_task(data: dict, script_engine):
    """Spiff's DMNEngine.result() wants a task-like object exposing
    .data, .task_spec and .workflow.script_engine. Build a minimal stand-in."""
    spec = type("Spec", (), {"file": "dmn"})()
    workflow = type("WF", (), {"script_engine": script_engine, "spec": spec})()
    task_spec = type("TS", (), {"name": "dmn", "bpmn_name": "dmn"})()
    return type("Task", (), {"data": data, "workflow": workflow, "task_spec": task_spec})()


_FLOW_KINDS = {
    "startEvent": "start", "endEvent": "end", "userTask": "task",
    "serviceTask": "task", "manualTask": "task", "callActivity": "call",
    "exclusiveGateway": "gateway", "parallelGateway": "gateway",
    "inclusiveGateway": "gateway", "subProcess": "subprocess",
}

# Colors for overlay categories, shared by any UI that draws footprints.
OVERLAY_COLORS = {
    "ADC": "#8856a7", "HC": "#3182bd", "IndividuallyProtected": "#e34a33",
    "EntranceCorridor": "#31a354", "floodplain": "#2b8cbe", "critical_slopes": "#d95f0e",
}


def process_flow(path: str, process_id: str | None = None) -> list[dict]:
    """Ordered, human-readable flow of a BPMN process, traversed from the start
    event along sequence flows (first-visit order). Nested subprocess internals
    are represented by the subprocess node itself, not expanded."""
    root = ET.parse(path).getroot()
    if process_id:
        proc = root.find(f".//{{{BPMN_NS}}}process[@id='{process_id}']")
    else:
        proc = root.find(f".//{{{BPMN_NS}}}process")
    if proc is None:
        return []

    nodes: dict[str, dict] = {}
    for child in list(proc):
        tag = child.tag.split("}")[-1]
        if tag in _FLOW_KINDS:
            nodes[child.get("id")] = {
                "id": child.get("id"),
                "kind": _FLOW_KINDS[tag],
                "name": child.get("name") or child.get("id"),
                "doc": (child.findtext(f"{{{BPMN_NS}}}documentation") or "").strip(),
                "called": child.get("calledElement"),
            }
    outgoing: dict[str, list[str]] = {}
    start_id = None
    for child in list(proc):
        if child.tag == f"{{{BPMN_NS}}}sequenceFlow":
            outgoing.setdefault(child.get("sourceRef"), []).append(child.get("targetRef"))
        elif child.tag == f"{{{BPMN_NS}}}startEvent" and start_id is None:
            start_id = child.get("id")

    order, seen = [], set()
    stack = [start_id] if start_id else list(nodes)
    while stack:
        nid = stack.pop(0)
        if nid in seen or nid not in nodes:
            continue
        seen.add(nid)
        order.append(nodes[nid])
        # preserve file order of branches
        stack = outgoing.get(nid, []) + stack
    # append any nodes not reached by traversal (defensive)
    for nid, n in nodes.items():
        if nid not in seen:
            order.append(n)
    return order


class DmnDecision:
    """A single decision table loaded from a .dmn file."""

    def __init__(self, path: str, decision_id: str):
        # Imported lazily so the module imports even if Spiff is missing.
        from SpiffWorkflow.dmn.parser.BpmnDmnParser import BpmnDmnParser
        from SpiffWorkflow.dmn.engine.DMNEngine import DMNEngine
        from SpiffWorkflow.bpmn.script_engine import PythonScriptEngine

        parser = BpmnDmnParser()
        parser.add_dmn_file(path)
        if decision_id not in parser.dmn_parsers:
            raise KeyError(
                f"decision '{decision_id}' not in {path}; "
                f"found {list(parser.dmn_parsers)}"
            )
        dp = parser.dmn_parsers[decision_id]
        dp.parse()
        self.decision_id = decision_id
        self.name = dp.decision.name
        self._engine = DMNEngine(dp.decision.decisionTables[0])
        self._script_engine = PythonScriptEngine()

    def evaluate(self, **inputs) -> dict:
        task = _make_dmn_task(dict(inputs), self._script_engine)
        return self._engine.result(task)


# --------------------------------------------------------------------------- #
#  Overlay result + providers                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class OverlayResult:
    overlays: set = field(default_factory=set)   # subset of OVERLAY_CATEGORIES
    floodplain: bool = False
    critical_slopes: bool = False
    details: dict = field(default_factory=dict)  # category -> human note / hit info
    checks: list = field(default_factory=list)   # [{signal, layer, hit, error}] per layer tested
    source: str = "manual"

    def overlay_list(self) -> list:
        """Overlays to feed the selector, or ['None'] if the parcel is clear."""
        return sorted(self.overlays) if self.overlays else ["None"]


class ManualOverlayProvider:
    """Overlays come straight from the UI. Always works, no network."""

    source = "manual"

    def resolve(self, lat=None, lng=None, *, overlays=None,
                floodplain=False, critical_slopes=False) -> OverlayResult:
        return OverlayResult(
            overlays=set(overlays or []),
            floodplain=bool(floodplain),
            critical_slopes=bool(critical_slopes),
            source="manual",
        )


def classify_layer(name: str) -> str | None:
    """Map an ArcGIS layer name to one of our overlay signals.

    Returns a category in OVERLAY_CATEGORIES, or 'floodplain' / 'critical_slopes',
    or None if the layer isn't an overlay we care about.
    """
    n = (name or "").lower()
    if "entrance corridor" in n:          # exact phrase; avoids 'Core Neighborhood Corridor'
        return "EntranceCorridor"
    if "architectural design control" in n or re.search(r"\badc\b", n):
        return "ADC"
    if "individually protected" in n or "protected propert" in n:
        return "IndividuallyProtected"
    if "historic conservation" in n or ("conservation" in n and "district" in n):
        return "HC"
    if "critical slope" in n or ("slope" in n and "regulat" in n):
        return "critical_slopes"
    if "flood" in n:  # floodplain / FEMA / SFHA
        return "floodplain"
    if "historic" in n:           # generic local historic district -> ADC-style
        return "ADC"
    return None


class ArcGISOverlayProvider:
    """Live point-in-polygon against Charlottesville's ArcGIS REST services.

    Uses stdlib urllib only (no requests). Auto-discovers which layer carries
    which overlay by scanning layer names, so it adapts to schema changes
    instead of hardcoding fragile layer IDs.
    """

    source = "arcgis"
    DEFAULT_ROOT = "https://gisweb.charlottesville.org/arcgis/rest/services"
    DEFAULT_SERVICES = ("OpenData_1/MapServer", "OpenData_2/MapServer")

    def __init__(self, root: str | None = None, services: Iterable[str] | None = None,
                 timeout: int = 25):
        self.root = (root or self.DEFAULT_ROOT).rstrip("/")
        self.services = list(services or self.DEFAULT_SERVICES)
        self.timeout = timeout
        # plan: signal -> list of (service_url, layer_id, layer_name)
        self.plan: dict[str, list[tuple[str, int, str]]] = {}

    # --- discovery -------------------------------------------------------- #
    def _service_url(self, svc: str) -> str:
        return svc if svc.startswith("http") else f"{self.root}/{svc}"

    def _get_json(self, url: str) -> dict:
        with urlopen(url, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def list_layers(self, svc: str) -> list[tuple[int, str]]:
        data = self._get_json(f"{self._service_url(svc)}?f=json")
        return [(l["id"], l.get("name", "")) for l in data.get("layers", [])]

    def discover(self) -> dict[str, list[tuple[str, int, str]]]:
        """Scan configured services, classify every layer, build the query plan."""
        plan: dict[str, list[tuple[str, int, str]]] = {}
        for svc in self.services:
            svc_url = self._service_url(svc)
            try:
                layers = self.list_layers(svc)
            except Exception as exc:  # noqa: BLE001 — surfaced to caller via plan
                plan.setdefault("_errors", []).append((svc_url, -1, str(exc)))
                continue
            for lid, lname in layers:
                signal = classify_layer(lname)
                if signal:
                    plan.setdefault(signal, []).append((svc_url, lid, lname))
        self.plan = plan
        return plan

    # --- querying --------------------------------------------------------- #
    def _layer_hit(self, svc_url: str, layer_id: int, lat: float, lng: float) -> bool:
        params = urlencode({
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "where": "1=1",
            "returnGeometry": "false",
            "returnCountOnly": "true",
            "f": "json",
        })
        data = self._get_json(f"{svc_url}/{layer_id}/query?{params}")
        if "count" in data:
            return data["count"] > 0
        return bool(data.get("features"))

    def _query_url(self, svc_url: str, layer_id: int,
                   max_offset: float = 0.00003, max_records: int = 4000) -> str:
        # Request Esri JSON (works on every ArcGIS version); we convert to GeoJSON
        # ourselves rather than relying on the server supporting f=geojson.
        params = urlencode({
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "maxAllowableOffset": str(max_offset),   # simplify vertices for rendering
            "resultRecordCount": str(max_records),
            "f": "json",
        })
        return f"{svc_url}/{layer_id}/query?{params}"

    @staticmethod
    def _esri_to_geojson(data: dict) -> dict:
        """Convert an Esri JSON query response to a GeoJSON FeatureCollection.
        Handles polygons (rings), polylines (paths), and points."""
        feats = []
        for f in data.get("features", []):
            g = f.get("geometry") or {}
            geom = None
            if "rings" in g and g["rings"]:
                # each ring -> its own polygon (fine for footprint display)
                geom = {"type": "MultiPolygon", "coordinates": [[ring] for ring in g["rings"]]}
            elif "paths" in g and g["paths"]:
                geom = {"type": "MultiLineString", "coordinates": g["paths"]}
            elif "x" in g and "y" in g:
                geom = {"type": "Point", "coordinates": [g["x"], g["y"]]}
            if geom:
                feats.append({"type": "Feature",
                              "properties": f.get("attributes", {}),
                              "geometry": geom})
        return {"type": "FeatureCollection", "features": feats}

    def fetch_geojson(self, svc_url: str, layer_id: int, **kw) -> dict:
        data = self._get_json(self._query_url(svc_url, layer_id, **kw))
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"].get("message", str(data["error"])))
        # already GeoJSON? pass through
        if isinstance(data, dict) and data.get("type") == "FeatureCollection":
            return data
        return self._esri_to_geojson(data if isinstance(data, dict) else {})

    def overlay_geojson(self, **kw) -> list[dict]:
        """Fetch footprints for every discovered overlay layer, for map display.

        Returns list of {signal, layer, geojson, count} (or {..., error})."""
        if not self.plan:
            self.discover()
        out = []
        for signal, targets in self.plan.items():
            if signal == "_errors":
                continue
            for svc_url, lid, lname in targets:
                try:
                    gj = self.fetch_geojson(svc_url, lid, **kw)
                    feats = gj.get("features", []) if isinstance(gj, dict) else []
                    out.append({"signal": signal, "layer": lname,
                                "geojson": gj, "count": len(feats)})
                except Exception as exc:  # noqa: BLE001
                    out.append({"signal": signal, "layer": lname,
                                "geojson": None, "count": 0, "error": str(exc)})
        return out

    def resolve(self, lat: float, lng: float) -> OverlayResult:
        if not self.plan:
            self.discover()
        res = OverlayResult(source="arcgis")
        for signal, targets in self.plan.items():
            if signal == "_errors":
                res.details["_errors"] = targets
                continue
            for svc_url, lid, lname in targets:
                hit, err = False, None
                try:
                    hit = self._layer_hit(svc_url, lid, lat, lng)
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                res.checks.append({"signal": signal, "layer": lname, "hit": hit, "error": err})
                if err:
                    res.details.setdefault(signal, []).append(f"error: {err}")
                    continue
                if hit:
                    if signal == "floodplain":
                        res.floodplain = True
                    elif signal == "critical_slopes":
                        res.critical_slopes = True
                    else:
                        res.overlays.add(signal)
                    res.details.setdefault(signal, []).append(f"hit: {lname}")
        return res


class LocalGeoJSONOverlayProvider:
    """Owned/offline overlays: a registry of {signal: geojson_path} plus shapely.

    signal in OVERLAY_CATEGORIES or 'floodplain' / 'critical_slopes'.
    """

    source = "local"

    def __init__(self, registry: dict[str, str]):
        from shapely.geometry import shape, Point  # noqa: F401 (presence check)
        self.registry = registry
        self._cache: dict[str, list] = {}

    def _geoms(self, path: str):
        from shapely.geometry import shape
        if path not in self._cache:
            with open(path, encoding="utf-8") as fh:
                gj = json.load(fh)
            feats = gj.get("features", []) if gj.get("type") == "FeatureCollection" else [gj]
            self._cache[path] = [shape(f["geometry"]) for f in feats if f.get("geometry")]
        return self._cache[path]

    def overlay_geojson(self) -> list[dict]:
        out = []
        for signal, path in self.registry.items():
            try:
                with open(path, encoding="utf-8") as fh:
                    gj = json.load(fh)
                if gj.get("type") != "FeatureCollection":
                    gj = {"type": "FeatureCollection",
                          "features": [gj] if gj.get("type") == "Feature" else []}
                out.append({"signal": signal, "layer": path,
                            "geojson": gj, "count": len(gj.get("features", []))})
            except Exception as exc:  # noqa: BLE001
                out.append({"signal": signal, "layer": path,
                            "geojson": None, "count": 0, "error": str(exc)})
        return out

    def resolve(self, lat: float, lng: float) -> OverlayResult:
        from shapely.geometry import Point
        pt = Point(lng, lat)
        res = OverlayResult(source="local")
        for signal, path in self.registry.items():
            try:
                hit = any(g.contains(pt) or g.intersects(pt) for g in self._geoms(path))
            except Exception as exc:  # noqa: BLE001
                res.details.setdefault(signal, []).append(f"error: {exc}")
                continue
            if hit:
                if signal == "floodplain":
                    res.floodplain = True
                elif signal == "critical_slopes":
                    res.critical_slopes = True
                else:
                    res.overlays.add(signal)
                res.details.setdefault(signal, []).append(f"hit: {path}")
        return res


# --------------------------------------------------------------------------- #
#  Permit navigator — wires the DMNs + spine together                          #
# --------------------------------------------------------------------------- #
_SECTION_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+)?)\b")


@dataclass
class ProcessRouting:
    process: str              # full selector output string
    section: str | None       # e.g. "5.2.14"
    authority: dict           # recommend/decide/appeal/hearing, or {} if no row


class PermitNavigator:
    SELECTOR_DECISION = "Decision_RequiredProcesses"
    AUTHORITY_DECISION = "Decision_ReviewAuthority"

    def __init__(self, selector_path: str, authority_path: str, spine_path: str,
                 models: dict[str, tuple[str, str]] | None = None):
        self.selector = DmnDecision(selector_path, self.SELECTOR_DECISION)
        self.authority = DmnDecision(authority_path, self.AUTHORITY_DECISION)
        self.spine_path = spine_path
        # section prefix -> (bpmn_path, process_id) for full process models
        self.models = models or {}

    def process_flow_for(self, section: str | None) -> list[dict]:
        """Full flow for a routed section if a BPMN model is registered."""
        if not section:
            return []
        entry = self.models.get(section)
        if not entry:
            return []
        path, pid = entry
        try:
            return process_flow(path, pid)
        except Exception:  # noqa: BLE001
            return []

    # --- selector (COLLECT, run once per applicable overlay) -------------- #
    def required_processes(self, *, action_type: str, overlays: OverlayResult,
                           use_permission: str, dimensional_relief: bool) -> list[str]:
        found: set[str] = set()
        for ov in overlays.overlay_list():
            r = self.selector.evaluate(
                actionType=action_type,
                overlay=ov,
                usePermission=use_permission,
                dimensionalReliefSought=bool(dimensional_relief),
                inFloodplain=bool(overlays.floodplain),
                criticalSlopesImpacted=bool(overlays.critical_slopes),
            )
            for v in (r.get("requiredProcess") or []):
                found.add(v)
        return sorted(found)

    # --- authority matrix (UNIQUE) --------------------------------------- #
    def authority_for(self, section: str) -> dict:
        try:
            r = self.authority.evaluate(processRef=section)
        except Exception:  # noqa: BLE001
            return {}
        return {k: r.get(k) for k in ("recommend", "decide", "appeal", "hearing")
                if r.get(k) is not None}

    def route(self, process_string: str) -> list[ProcessRouting]:
        """A selector output may name >1 section (e.g. 'Minor or Major Historic')."""
        routings = []
        for sec in _SECTION_RE.findall(process_string):
            auth = self.authority_for(sec)
            routings.append(ProcessRouting(process_string, sec, auth))
        if not routings:
            routings.append(ProcessRouting(process_string, None, {}))
        return routings

    # --- spine intake steps (read from the BPMN so UI tracks the model) --- #
    def spine_steps(self) -> list[dict]:
        tree = ET.parse(self.spine_path)
        proc = tree.getroot().find(f".//{{{BPMN_NS}}}process")
        steps = []
        for child in list(proc):
            if child.tag == f"{{{BPMN_NS}}}userTask":
                doc = child.findtext(f"{{{BPMN_NS}}}documentation") or ""
                steps.append({"id": child.get("id"),
                              "name": child.get("name"),
                              "doc": doc.strip()})
        return steps

    def spine_clocks(self) -> list[str]:
        """Statutory clocks worth surfacing alongside the intake."""
        return [
            "Completeness determination: 5 days (5.2.1.C.4)",
            "Revised materials due \u2265 30 days before a scheduled meeting/hearing (5.2.1.C.6)",
            "Up to 3 revisions before a new application fee (5.2.1.C.6.c)",
            "Withdrawal permitted at any time, no refund (5.2.1.C.7)",
        ]

    def workflow_runner(self, section: str, seed: dict | None = None):
        """Build a live SpiffWorkflow runner for a registered process model.
        Bundles the process BPMN + the spine so the Call Activity resolves."""
        entry = self.models.get(section)
        if not entry:
            return None
        path, pid = entry
        return WorkflowRunner([path, self.spine_path], pid, seed=seed)


# --------------------------------------------------------------------------- #
#  Live workflow execution (SpiffWorkflow)                                     #
# --------------------------------------------------------------------------- #
# Decision variables the registered process models read at gateways. Seeded up
# front so gateways can evaluate; edit + restart to explore other branches.
DEFAULT_SEED = {
    "preAppWaived": True, "inHistoricOverlay": False, "complete": True,
    "revisionCount": 0, "proffersChangedAtHearing": False, "hasProffers": True,
    "councilModOption": "decline", "councilDecision": "approve",
}


class WorkflowRunner:
    """Thin wrapper over a Spiff BpmnWorkflow for interactive, step-through use.
    Holds the live workflow object (kept in Streamlit session_state across reruns)."""

    def __init__(self, bpmn_paths: list[str], process_id: str, seed: dict | None = None):
        from SpiffWorkflow.bpmn.parser.BpmnParser import BpmnParser
        from SpiffWorkflow.bpmn.workflow import BpmnWorkflow
        from SpiffWorkflow.util.task import TaskState
        self._TaskState = TaskState

        parser = BpmnParser()
        for pth in bpmn_paths:
            parser.add_bpmn_file(pth)
        self.process_id = process_id
        self.wf = BpmnWorkflow(parser.get_spec(process_id),
                               parser.get_subprocess_specs(process_id))
        self.seed = dict(DEFAULT_SEED, **(seed or {}))
        self.history: list[str] = []           # human tasks completed, in order
        for t in self.wf.get_tasks(state=TaskState.READY):
            t.set_data(**self.seed)
        self.wf.do_engine_steps()
        self.wf.refresh_waiting_tasks()

    @staticmethod
    def _label(task) -> str:
        return getattr(task.task_spec, "bpmn_name", None) or task.task_spec.name

    def ready_human_tasks(self) -> list:
        return [t for t in self.wf.get_tasks(state=self._TaskState.READY)
                if getattr(t.task_spec, "manual", False)]

    def complete(self, task_id: str):
        for t in self.ready_human_tasks():
            if str(t.id) == task_id:
                t.set_data(**self.seed)
                self.history.append(self._label(t))
                t.run()
                self.wf.do_engine_steps()
                self.wf.refresh_waiting_tasks()
                return

    def ended(self) -> list[str]:
        return [self._label(t) for t in self.wf.get_tasks()
                if t.task_spec.__class__.__name__ == "EndEvent"
                and t.state == self._TaskState.COMPLETED]

    def state(self) -> dict:
        ready = [{"id": str(t.id), "name": self._label(t),
                  "doc": getattr(t.task_spec, "documentation", "") or ""}
                 for t in self.ready_human_tasks()]
        return {
            "history": list(self.history),
            "ready": ready,
            "ended": self.ended(),
            "complete": self.wf.is_completed(),
        }
