"""
cville_permit_navigator.py — Streamlit front end for the Charlottesville
Development Code approval navigator.

Pick a location -> SEE the zoning overlay footprints on the map and verify them ->
resolve overlays at the point -> describe the project -> the selector DMN routes
it to the required approval process(es), the authority matrix DMN says who
recommends/decides/hears appeals, and full process models (e.g. 5.2.5 rezoning,
which calls the 5.2.1 spine) are shown inline.

Run:
    pip install -r requirements.txt
    streamlit run cville_permit_navigator.py
"""

from __future__ import annotations

import os
import base64
import streamlit as st
import streamlit.components.v1 as components

from cville_core import (
    PermitNavigator, ManualOverlayProvider, ArcGISOverlayProvider,
    LocalGeoJSONOverlayProvider, OverlayResult, OVERLAY_CATEGORIES, OVERLAY_COLORS,
)
from bpmn_build import build_stub_bpmn

try:
    import folium
    from streamlit_folium import st_folium
    HAVE_MAP = True
except Exception:  # noqa: BLE001
    HAVE_MAP = False


_BPMN_VIEWER = """
<link rel="stylesheet" href="https://unpkg.com/bpmn-js@17.11.1/dist/assets/diagram-js.css">
<link rel="stylesheet" href="https://unpkg.com/bpmn-js@17.11.1/dist/assets/bpmn-js.css">
<link rel="stylesheet" href="https://unpkg.com/bpmn-js@17.11.1/dist/assets/bpmn-font/css/bpmn-embedded.css">
<style>
 #wrap{position:relative;height:__H__px;border:1px solid #d9d9d9;border-radius:8px;background:#fff;}
 #c{height:100%;} #bar{position:absolute;top:8px;left:8px;z-index:10;}
 #back{display:none;padding:4px 10px;border:1px solid #888;border-radius:6px;background:#fff;cursor:pointer;font:13px sans-serif;}
 #hint{position:absolute;bottom:8px;left:8px;z-index:10;font:12px sans-serif;color:#555;background:rgba(255,255,255,.85);padding:2px 6px;border-radius:4px;}
 #err{color:#b00020;padding:10px;font:13px monospace;}
</style>
<div id="wrap">
  <div id="bar"><button id="back">&#8592; back to process</button></div>
  <div id="c"></div>
  <div id="hint">Click &ldquo;Common Review Procedures (5.2.1)&rdquo; to drill into the spine &middot; scroll to zoom, drag to pan</div>
</div>
<script src="https://unpkg.com/bpmn-js@17.11.1/dist/bpmn-navigated-viewer.production.min.js"></script>
<script>
 var B={main:"__MAIN__",spine:"__SPINE__"};
 function dec(b){return new TextDecoder().decode(Uint8Array.from(atob(b),function(c){return c.charCodeAt(0);}));}
 function boot(){
   if(!window.BpmnJS){document.getElementById('c').innerHTML='<div id=err>Could not load bpmn.io viewer (needs internet access to unpkg.com).</div>';return;}
   var viewer=new BpmnJS({container:'#c'});var cur='main';
   function show(w){viewer.importXML(dec(B[w])).then(function(){viewer.get('canvas').zoom('fit-viewport');cur=w;
     document.getElementById('back').style.display=(w==='spine')?'inline-block':'none';})
     .catch(function(e){document.getElementById('c').innerHTML='<div id=err>'+e+'</div>';});}
   viewer.on('element.click',function(ev){var t=ev.element&&ev.element.type;if(cur==='main'&&t==='bpmn:CallActivity'){show('spine');}});
   document.getElementById('back').onclick=function(){show('main');};
   show('main');
 }
 if(document.readyState!=='loading'){setTimeout(boot,50);}else{window.addEventListener('DOMContentLoaded',boot);}
</script>
"""


def render_bpmn_viewer(main_xml: str, spine_xml: str, height: int = 520):
    mb = base64.b64encode(main_xml.encode("utf-8")).decode("ascii")
    sb = base64.b64encode(spine_xml.encode("utf-8")).decode("ascii")
    html = (_BPMN_VIEWER.replace("__MAIN__", mb).replace("__SPINE__", sb)
            .replace("__H__", str(height)))
    components.html(html, height=height + 24, scrolling=False)

CVILLE_CENTER = (38.0293, -78.4767)
ACTION_TYPES = [
    "Rezone", "ZoningTextAmendment", "ComprehensivePlanAmendment",
    "ReviewOfPublicFacilities", "NewDevelopment", "Subdivision",
    "BuildingOrExteriorAlteration", "Sign", "TemporaryUse", "TreeRemoval",
]
USE_PERMISSIONS = ["ByRight", "SpecialUse", "NotPermitted"]
KIND_ICON = {"start": "\u25B6", "end": "\u23F9", "task": "\u25AB",
             "gateway": "\u25C6", "call": "\u2b95", "subprocess": "\u25A4"}

st.set_page_config(page_title="Cville Permit Navigator", page_icon="\U0001F3DB", layout="wide")
st.title("\U0001F3DB Charlottesville Development Code \u2014 Approval Navigator")
st.caption("Overlay footprints from the city GIS + selector/authority DMNs + full BPMN process "
           "models, executed live via SpiffWorkflow. Informational only \u2014 not a legal determination.")


# --------------------------------------------------------------------------- #
#  Sidebar                                                                    #
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Artifacts")
    here = os.path.dirname(os.path.abspath(__file__))
    selector_path = st.text_input("Selector DMN", os.path.join(here, "approval_process_selector.dmn"))
    authority_path = st.text_input("Authority matrix DMN", os.path.join(here, "review_authority_matrix.dmn"))
    spine_path = st.text_input("Spine BPMN", os.path.join(here, "common_review_procedures.bpmn"))
    rezoning_path = st.text_input("Rezoning BPMN (5.2.5)", os.path.join(here, "cville_rezoning_5_2_5.bpmn"))

    st.header("Overlay source")
    src = st.radio("How should overlays be resolved & drawn?",
                   ["ArcGIS (live, city GIS)", "Local GeoJSON (owned)", "Manual entry"])

    arcgis_provider = None
    local_registry: dict[str, str] = {}
    if src == "ArcGIS (live, city GIS)":
        root = st.text_input("REST root", ArcGISOverlayProvider.DEFAULT_ROOT)
        svc_text = st.text_area("Services to scan (one per line)",
                                "\n".join(ArcGISOverlayProvider.DEFAULT_SERVICES), height=70)
        services = [s.strip() for s in svc_text.splitlines() if s.strip()]
        arcgis_provider = ArcGISOverlayProvider(root=root, services=services)
        if st.session_state.get("arcgis_plan"):
            arcgis_provider.plan = st.session_state["arcgis_plan"]
        if st.button("Discover overlay layers"):
            with st.spinner("Scanning services & fetching footprints\u2026"):
                st.session_state["arcgis_plan"] = arcgis_provider.discover()
                try:
                    st.session_state["footprints"] = arcgis_provider.overlay_geojson()
                except Exception as exc:  # noqa: BLE001
                    st.session_state["footprints"] = []
                    st.warning(f"Layers found, but footprint fetch failed: {exc}")
        if st.session_state.get("arcgis_plan"):
            with st.expander("Discovered layers", expanded=True):
                for sig, targets in st.session_state["arcgis_plan"].items():
                    if sig == "_errors":
                        for u, _, msg in targets:
                            st.error(f"{u}: {msg}")
                    else:
                        st.markdown(f"**{sig}** \u2014 " +
                                    "; ".join(f"{n} (id {i})" for _, i, n in targets))
    elif src == "Local GeoJSON (owned)":
        st.caption("Path to a GeoJSON per signal (blank = skip).")
        for sig in OVERLAY_CATEGORIES + ["floodplain", "critical_slopes"]:
            p = st.text_input(f"{sig} GeoJSON", key=f"geo_{sig}")
            if p.strip():
                local_registry[sig] = p.strip()


@st.cache_resource(show_spinner=False)
def load_navigator(sel, auth, spine, rez):
    models = {}
    if rez and os.path.exists(rez):
        models["5.2.5"] = (rez, "Process_ZoningMapAmendment")
    return PermitNavigator(sel, auth, spine, models=models)


try:
    nav = load_navigator(selector_path, authority_path, spine_path, rezoning_path)
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load artifacts: {exc}")
    st.stop()


# --------------------------------------------------------------------------- #
#  Overlay footprints (fetch + cache)                                          #
# --------------------------------------------------------------------------- #
def fetch_footprints():
    if src == "ArcGIS (live, city GIS)" and arcgis_provider is not None:
        return arcgis_provider.overlay_geojson()
    if src == "Local GeoJSON (owned)" and local_registry:
        return LocalGeoJSONOverlayProvider(local_registry).overlay_geojson()
    return []


def color_chip(sig):
    c = OVERLAY_COLORS.get(sig, "#666")
    return f"<span style='display:inline-block;width:12px;height:12px;background:{c};" \
           f"border:1px solid #333;margin-right:6px;vertical-align:middle'></span>"


st.subheader("1. Location & overlay footprints")
lat = float(st.session_state.get("lat", CVILLE_CENTER[0]))
lng = float(st.session_state.get("lng", CVILLE_CENTER[1]))

top = st.columns([1, 1, 1, 2])
with top[0]:
    if st.button("Load / refresh footprints", type="secondary",
                 disabled=(src == "Manual entry")):
        with st.spinner("Fetching overlay geometry\u2026"):
            try:
                st.session_state["footprints"] = fetch_footprints()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Footprint fetch failed: {exc}")
footprints = st.session_state.get("footprints", [])

# Verification table — the "am I seeing the right stuff" check.
if footprints:
    rows = [{"": "", "Signal": r["signal"], "Layer": r["layer"],
             "Features": r.get("count", 0),
             "Status": r.get("error", "ok")} for r in footprints]
    st.markdown("**Overlay layers pulled from the API** (verify these are the right ones):")
    st.dataframe(
        [{"Signal": r["signal"], "Layer": r["layer"],
          "Features": r.get("count", 0), "Status": r.get("error", "ok")} for r in footprints],
        hide_index=True, use_container_width=True)
    legend = " &nbsp; ".join(color_chip(r["signal"]) + r["signal"]
                             for r in footprints if r.get("geojson"))
    st.markdown("Legend: " + legend, unsafe_allow_html=True)
elif src != "Manual entry":
    st.info("Click **Load / refresh footprints** to draw the overlay layers on the map "
            "and confirm they're the right ones. (ArcGIS: run **Discover overlay layers** first.)")

mcol, ccol = st.columns([3, 2])
with ccol:
    lat = st.number_input("Latitude", value=lat, format="%.6f")
    lng = st.number_input("Longitude", value=lng, format="%.6f")
    st.session_state["lat"], st.session_state["lng"] = lat, lng
    resolve_clicked = st.button("Resolve overlays at this point", type="primary")

with mcol:
    if HAVE_MAP:
        fmap = folium.Map(location=[lat, lng], zoom_start=13, control_scale=True)
        drawn = 0
        for r in footprints:
            gj = r.get("geojson")
            if not gj or not gj.get("features"):
                continue
            c = OVERLAY_COLORS.get(r["signal"], "#666666")
            folium.GeoJson(
                gj, name=f'{r["signal"]}: {r["layer"]} ({r["count"]})',
                style_function=lambda _f, c=c: {
                    "color": c, "weight": 2, "fillColor": c, "fillOpacity": 0.28},
            ).add_to(fmap)
            drawn += 1
        folium.Marker([lat, lng], tooltip="Selected point",
                      icon=folium.Icon(color="black", icon="crosshairs", prefix="fa")).add_to(fmap)
        if drawn:
            folium.LayerControl(collapsed=False).add_to(fmap)
        out = st_folium(fmap, height=460, width=None, key="map")
        if out and out.get("last_clicked"):
            st.session_state["lat"] = out["last_clicked"]["lat"]
            st.session_state["lng"] = out["last_clicked"]["lng"]
            st.caption("Map click captured \u2014 update the fields or re-run resolve to use it.")
    else:
        st.warning("Install `streamlit-folium` + `folium` to see the map and overlay footprints.")


# --------------------------------------------------------------------------- #
#  2. Overlays at the point (auto-detected, editable)                          #
# --------------------------------------------------------------------------- #
st.subheader("2. Overlays at this point")
detected: OverlayResult | None = st.session_state.get("detected")
if resolve_clicked:
    try:
        if src == "ArcGIS (live, city GIS)":
            detected = arcgis_provider.resolve(lat, lng)
        elif src == "Local GeoJSON (owned)":
            detected = LocalGeoJSONOverlayProvider(local_registry).resolve(lat, lng)
        else:
            detected = OverlayResult(source="manual")
        st.session_state["detected"] = detected
    except Exception as exc:  # noqa: BLE001
        st.error(f"Overlay lookup failed: {exc}")
        detected = OverlayResult(source="error")

det = detected or OverlayResult()
if det.source not in ("manual", "error", ""):
    hits = sorted(det.overlays) + (["floodplain"] if det.floodplain else []) \
        + (["critical slopes"] if det.critical_slopes else [])
    st.success(f"Detected via {det.source}: {', '.join(hits)}" if hits
               else f"No overlays detected at this point via {det.source}.")
    if det.checks:
        st.markdown("**Every layer tested at this point** (so the overrides below make sense):")
        st.dataframe(
            [{"Result": "\u2705 in" if c["hit"] else ("\u26a0\ufe0f error" if c["error"] else "\u2014 not in"),
              "Signal": c["signal"], "Layer": c["layer"], "Note": c["error"] or ""}
             for c in det.checks],
            hide_index=True, use_container_width=True)
    if det.details:
        with st.expander("Raw lookup detail"):
            st.json(det.details)

st.caption("Detection pre-fills these; override as needed. These are what the model uses.")
oc1, oc2 = st.columns(2)
chosen = set()
with oc1:
    for cat in OVERLAY_CATEGORIES:
        if st.checkbox(cat, value=cat in det.overlays, key=f"ov_{cat}"):
            chosen.add(cat)
with oc2:
    floodplain = st.checkbox("In floodplain", value=det.floodplain, key="ov_flood")
    slopes = st.checkbox("Critical slopes impacted", value=det.critical_slopes, key="ov_slope")

overlays = ManualOverlayProvider().resolve(overlays=chosen, floodplain=floodplain, critical_slopes=slopes)


# --------------------------------------------------------------------------- #
#  3. Project                                                                  #
# --------------------------------------------------------------------------- #
st.subheader("3. Project")
pc1, pc2, pc3 = st.columns(3)
with pc1:
    action = st.selectbox("Project action", ACTION_TYPES)
with pc2:
    use_perm = st.selectbox("Use permission in district", USE_PERMISSIONS)
with pc3:
    relief = st.checkbox("Seeking dimensional relief (setback, height, etc.)")


# --------------------------------------------------------------------------- #
#  4. Required processes + authority + full model                              #
# --------------------------------------------------------------------------- #
st.subheader("4. Required approval process(es)")
procs = nav.required_processes(action_type=action, overlays=overlays,
                               use_permission=use_perm, dimensional_relief=relief)
if not procs:
    st.info("No approval process matched. Adjust the project action or overlays above.")
else:
    if len(overlays.overlay_list()) > 1:
        st.caption("Parcel sits in multiple overlays \u2014 selector run per overlay, results unioned.")
    for p in procs:
        with st.container(border=True):
            st.markdown(f"**{p}**")
            rows = []
            for r in nav.route(p):
                if r.section and r.authority:
                    a = r.authority
                    rows.append({"Sec.": r.section, "Review / Recommend": a.get("recommend", "\u2014"),
                                 "Final Decision": a.get("decide", "\u2014"),
                                 "Appeal": a.get("appeal", "\u2014"), "Hearing": a.get("hearing", "\u2014")})
                elif r.section:
                    rows.append({"Sec.": r.section, "Review / Recommend": "(no matrix row)",
                                 "Final Decision": "", "Appeal": "", "Hearing": ""})
            if rows:
                st.dataframe(rows, hide_index=True, use_container_width=True)


# --------------------------------------------------------------------------- #
#  Process model diagram — redraws on selection, drill into the spine          #
# --------------------------------------------------------------------------- #
sections = {}
for p in procs:
    for r in nav.route(p):
        if r.section:
            sections.setdefault(r.section, p)

if sections:
    st.subheader("Process model \u2014 pick a process to render it")
    st.caption("Each choice redraws the actual BPMN. Processes without a hand-built model are "
               "generated on the fly from the authority matrix. Click the spine activity to drill in.")
    opts = list(sections)
    choice = st.radio("Process", opts, format_func=lambda s: sections[s], horizontal=True,
                      label_visibility="collapsed")
    try:
        if choice == "5.2.5" and os.path.exists(rezoning_path):
            main_xml = open(rezoning_path, encoding="utf-8").read()
            st.caption("Hand-built **5.2.5** model \u2014 the call activity invokes the 5.2.1 spine.")
        else:
            main_xml = build_stub_bpmn(choice, nav.authority_for(choice))
            st.caption(f"Overview of **{choice}** generated from the authority matrix.")
        spine_xml = open(spine_path, encoding="utf-8").read()
        render_bpmn_viewer(main_xml, spine_xml, height=520)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not render model: {exc}")


# --------------------------------------------------------------------------- #
#  5. Common Review Procedures spine (shared intake)                           #
# --------------------------------------------------------------------------- #
st.subheader("5. Common Review Procedures intake (5.2.1)")
st.caption("Every routed process runs this shared intake spine first. Steps read from the BPMN.")
steps = nav.spine_steps()
sc1, sc2 = st.columns([3, 2])
with sc1:
    for s in steps:
        st.checkbox(s["name"], key=f"spine_{s['id']}", help=s["doc"] or None)
with sc2:
    revisions = st.number_input("Revisions used", min_value=0, max_value=10, value=0)
    if revisions > 3:
        st.warning("New application fee required for further review (5.2.1.C.6.c).")
    with st.expander("Statutory clocks & withdrawal", expanded=True):
        for c in nav.spine_clocks():
            st.markdown(f"- {c}")


# --------------------------------------------------------------------------- #
#  6. Run the process live (SpiffWorkflow)                                     #
# --------------------------------------------------------------------------- #
st.subheader("6. Run the process live (SpiffWorkflow)")
runnable = []
for p in procs:
    for r in nav.route(p):
        if r.section in nav.models and r.section not in runnable:
            runnable.append(r.section)

if not runnable:
    st.caption("No routed process has an executable model yet. Rezoning (5.2.5) is the "
               "first one built \u2014 pick a rezoning above to run it live.")
else:
    pick = st.selectbox("Process to execute", runnable)
    with st.expander("Decision variables (seeded up front; change and restart to explore branches)"):
        dc1, dc2, dc3 = st.columns(3)
        with dc1:
            pre_waived = st.checkbox("Pre-application conference waived", value=True)
            complete_ok = st.checkbox("Application complete on first submittal", value=True)
        with dc2:
            in_hist = st.checkbox("Property in ADC/HC/IPP overlay",
                                  value=bool(overlays.overlays & {"ADC", "HC", "IndividuallyProtected"}))
            has_proffers = st.checkbox("Includes proffered conditions", value=True)
        with dc3:
            proffers_changed = st.checkbox("Proffers changed at PC hearing", value=False)
            council_decision = st.selectbox("Council decision", ["approve", "deny"])
        council_mod = st.selectbox("Council proffer-modification handling",
                                   ["decline", "continue", "refer"])
    seed = {"preAppWaived": pre_waived, "complete": complete_ok, "inHistoricOverlay": in_hist,
            "hasProffers": has_proffers, "proffersChangedAtHearing": proffers_changed,
            "councilDecision": council_decision, "councilModOption": council_mod,
            "revisionCount": 0}

    bcol1, bcol2 = st.columns([1, 3])
    with bcol1:
        if st.button("\u25B6 Start / restart run", type="primary"):
            try:
                st.session_state["runner"] = nav.workflow_runner(pick, seed=seed)
                st.session_state["runner_section"] = pick
            except Exception as exc:  # noqa: BLE001
                st.session_state["runner"] = None
                st.error(f"Could not start workflow: {exc}")

    runner = st.session_state.get("runner")
    if runner is not None and st.session_state.get("runner_section") == pick:
        state = runner.state()
        colA, colB = st.columns(2)
        with colA:
            st.markdown("**Completed**")
            if state["history"]:
                for h in state["history"]:
                    st.markdown(f"\u2705 {h}")
            else:
                st.caption("nothing yet")
        with colB:
            st.markdown("**Ready now** \u2014 click to advance")
            if state["ready"]:
                for t in state["ready"]:
                    if st.button(f"\u25B8 {t['name']}", key=f"run_{t['id']}", help=t["doc"] or None):
                        runner.complete(t["id"])
                        st.rerun()
            elif not state["complete"]:
                st.caption("(engine step / waiting)")
        if state["complete"]:
            end = ", ".join(state["ended"]) or "end"
            st.success(f"Process complete \u2014 reached: {end}")
