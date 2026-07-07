"""
cville_permit_navigator.py — Charlottesville Development Code approval navigator.

Focused flow: pick a location on the map, describe the project, and SEE the
approval process that has to happen (rendered from the BPMN, drill into the
5.2.1 spine). Everything else (files, GIS config, authority tables, the shared
intake, and the live SpiffWorkflow stepper) is tucked away, collapsed.

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
    LocalGeoJSONOverlayProvider, OVERLAY_CATEGORIES, OVERLAY_COLORS,
)
from bpmn_build import build_stub_bpmn

try:
    import folium
    from streamlit_folium import st_folium
    HAVE_MAP = True
except Exception:  # noqa: BLE001
    HAVE_MAP = False


# --------------------------------------------------------------------------- #
#  Inline BPMN viewer (bpmn-js from CDN); click the call activity -> spine     #
# --------------------------------------------------------------------------- #
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
  <div id="hint">Click &ldquo;Common Review Procedures (5.2.1)&rdquo; to drill into the shared intake &middot; scroll to zoom, drag to pan</div>
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


def render_bpmn_viewer(main_xml: str, spine_xml: str, height: int = 540):
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

st.set_page_config(page_title="Cville Approval Navigator", page_icon="\U0001F3DB", layout="wide")


# --------------------------------------------------------------------------- #
#  Advanced settings — collapsed, off to the side. Sensible defaults so the    #
#  app works untouched.                                                        #
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("### Charlottesville\nDevelopment Code approval navigator")
    st.caption("Informational only \u2014 not a legal determination.")
    st.caption("\U0001F527 build 4 \u2014 overlay detection fix")
    with st.expander("\u2699\ufe0f Advanced (files & GIS)", expanded=False):
        here = os.path.dirname(os.path.abspath(__file__))
        selector_path = st.text_input("Selector DMN", os.path.join(here, "approval_process_selector.dmn"))
        authority_path = st.text_input("Authority matrix DMN", os.path.join(here, "review_authority_matrix.dmn"))
        spine_path = st.text_input("Spine BPMN", os.path.join(here, "common_review_procedures.bpmn"))
        rezoning_path = st.text_input("Rezoning BPMN (5.2.5)", os.path.join(here, "cville_rezoning_5_2_5.bpmn"))
        st.divider()
        src = st.radio("Overlay source",
                       ["ArcGIS (live, city GIS)", "Local GeoJSON (owned)", "Manual entry"],
                       help="How overlays at the pin are detected. Manual = skip the GIS entirely.")
        arcgis_root = ArcGISOverlayProvider.DEFAULT_ROOT
        arcgis_services = tuple(ArcGISOverlayProvider.DEFAULT_SERVICES)
        local_registry: dict[str, str] = {}
        if src == "ArcGIS (live, city GIS)":
            arcgis_root = st.text_input("REST root", ArcGISOverlayProvider.DEFAULT_ROOT)
            svc_text = st.text_area("Services to scan (one per line)",
                                    "\n".join(ArcGISOverlayProvider.DEFAULT_SERVICES), height=70)
            arcgis_services = tuple(s.strip() for s in svc_text.splitlines() if s.strip())
        elif src == "Local GeoJSON (owned)":
            for sig in OVERLAY_CATEGORIES + ["floodplain", "critical_slopes"]:
                pth = st.text_input(f"{sig} GeoJSON", key=f"geo_{sig}")
                if pth.strip():
                    local_registry[sig] = pth.strip()


@st.cache_resource(show_spinner=False)
def load_navigator(sel, auth, spine, rez):
    models = {}
    if rez and os.path.exists(rez):
        models["5.2.5"] = (rez, "Process_ZoningMapAmendment")
    return PermitNavigator(sel, auth, spine, models=models)


try:
    nav = load_navigator(selector_path, authority_path, spine_path, rezoning_path)
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load artifacts (open **Advanced** in the sidebar to fix paths): {exc}")
    st.stop()


# --------------------------------------------------------------------------- #
#  Overlay districts: load geometry once (visible), detect the pin locally      #
# --------------------------------------------------------------------------- #
def load_footprints(source, root, services, local_items):
    """Fetch overlay-district geometry. Returns (rows, error_msg). NOT cached, so
    a transient failure is never remembered \u2014 the Reload button always retries."""
    try:
        if source == "ArcGIS (live, city GIS)":
            prov = ArcGISOverlayProvider(root=root, services=list(services))
            prov.plan = prov.discover()
            return prov.overlay_geojson(), ""
        if source == "Local GeoJSON (owned)" and local_items:
            return LocalGeoJSONOverlayProvider(dict(local_items)).overlay_geojson(), ""
        return [], ""
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


def _point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_geom(lng, lat, geom):
    t = geom.get("type")
    cs = geom.get("coordinates", [])
    polys = [cs] if t == "Polygon" else (cs if t == "MultiPolygon" else [])
    for poly in polys:
        if not poly:
            continue
        if _point_in_ring(lng, lat, poly[0]) and not any(
                _point_in_ring(lng, lat, hole) for hole in poly[1:]):
            return True
    return False


def detect_from_footprints(lat, lng, footprints):
    """Point-in-polygon against the SAME geometry drawn on the map, so the pin's
    overlays always match what's shown. Returns (overlays:set, flood, slopes)."""
    over, flood, slope = set(), False, False
    for r in footprints:
        gj = r.get("geojson")
        if not gj or not gj.get("features"):
            continue
        hit = any(_point_in_geom(lng, lat, f.get("geometry") or {}) for f in gj["features"])
        if not hit:
            continue
        sig = r["signal"]
        if sig == "floodplain":
            flood = True
        elif sig == "critical_slopes":
            slope = True
        else:
            over.add(sig)
    return over, flood, slope


def chip(label, color="#2b8cbe"):
    return (f"<span style='display:inline-block;padding:2px 9px;margin:2px 4px 2px 0;border-radius:12px;"
            f"background:{color}1a;border:1px solid {color};color:{color};font-size:12px'>{label}</span>")


def legend_item(sig):
    c = OVERLAY_COLORS.get(sig, "#666666")
    return (f"<span style='display:inline-block;width:12px;height:12px;background:{c};"
            f"border:1px solid #333;margin:0 5px 0 12px;vertical-align:middle'></span>{sig}")


# --------------------------------------------------------------------------- #
#  1. LOCATION                                                                 #
# --------------------------------------------------------------------------- #
if "pt" not in st.session_state:
    st.session_state.pt = list(CVILLE_CENTER)
if "mcenter" not in st.session_state:
    st.session_state.mcenter = list(CVILLE_CENTER)
if "mzoom" not in st.session_state:
    st.session_state.mzoom = 16

st.subheader("1 \u00b7 Where is the parcel?")
pt = st.session_state.pt

# --- load overlay districts once (visible + reloadable), keep in session ------
lc1, lc2 = st.columns([1, 4])
need_load = ("footprints" not in st.session_state) or (st.session_state.get("fp_src") != src)
if lc1.button("\u21bb Reload layers") or need_load:
    with st.spinner("Loading overlay districts from the city GIS\u2026"):
        rows, err = load_footprints(src, arcgis_root, arcgis_services,
                                    tuple(sorted(local_registry.items())))
    st.session_state["footprints"] = rows
    st.session_state["fp_error"] = err
    st.session_state["fp_src"] = src

footprints = st.session_state.get("footprints", [])
fp_error = st.session_state.get("fp_error", "")
present_fp = [r for r in footprints if r.get("geojson") and r["geojson"].get("features")]

# status + static color legend, above the map (no folium layer-control menu)
if present_fp:
    seen, items = set(), []
    for r in present_fp:
        if r["signal"] in seen:
            continue
        seen.add(r["signal"])
        items.append(legend_item(r["signal"]))
    total = sum(len(r["geojson"]["features"]) for r in present_fp)
    lc2.caption(f"{len(present_fp)} overlay layers loaded ({total} features).")
    st.markdown("**Overlay districts:** " + " ".join(items), unsafe_allow_html=True)
elif src == "Manual entry":
    lc2.caption("Manual mode \u2014 set overlays by hand below.")
else:
    lc2.warning(f"No overlay geometry loaded. {fp_error or 'The GIS returned nothing.'} "
                "Check the source in **Advanced**, then Reload.")
    if footprints:
        with st.expander("Layer load detail"):
            st.dataframe([{"Signal": r["signal"], "Layer": r["layer"],
                           "Features": r.get("count", 0), "Status": r.get("error", "ok")}
                          for r in footprints], hide_index=True, use_container_width=True)

if HAVE_MAP:
    fmap = folium.Map(location=st.session_state.mcenter, zoom_start=st.session_state.mzoom,
                      control_scale=True)
    for r in present_fp:
        c = OVERLAY_COLORS.get(r["signal"], "#666666")
        folium.GeoJson(
            r["geojson"], name=f'{r["signal"]}: {r["layer"]}',
            style_function=lambda _f, c=c: {"color": c, "weight": 2,
                                            "fillColor": c, "fillOpacity": 0.22},
        ).add_to(fmap)
    folium.Marker(pt, tooltip="Parcel",
                  icon=folium.Icon(color="darkblue", icon="crosshairs", prefix="fa")).add_to(fmap)
    out = st_folium(fmap, key="map", center=st.session_state.mcenter, zoom=st.session_state.mzoom,
                    height=440, use_container_width=True,
                    returned_objects=["last_clicked", "center", "zoom"])
    st.caption("Click the map to set the parcel. The view stays where you leave it.")
    if out:
        if out.get("center"):
            st.session_state.mcenter = [out["center"]["lat"], out["center"]["lng"]]
        if out.get("zoom") is not None:
            st.session_state.mzoom = out["zoom"]
        lc = out.get("last_clicked")
        if lc:
            newpt = [round(lc["lat"], 6), round(lc["lng"], 6)]
            if newpt != st.session_state.pt:
                st.session_state.pt = newpt
                st.rerun()
else:
    st.warning("Install `streamlit-folium` + `folium` for the map; entering coordinates instead.")

with st.expander("Enter exact coordinates"):
    ec = st.columns(2)
    nlat = ec[0].number_input("Latitude", value=float(pt[0]), format="%.6f")
    nlng = ec[1].number_input("Longitude", value=float(pt[1]), format="%.6f")
    if [round(nlat, 6), round(nlng, 6)] != st.session_state.pt:
        st.session_state.pt = [round(nlat, 6), round(nlng, 6)]
        st.rerun()

lat, lng = st.session_state.pt

# Detect overlays at the pin LOCALLY, against the same polygons drawn above.
det_over, det_flood, det_slope = detect_from_footprints(lat, lng, present_fp)

# Effective overlays: follow detection by default; manual override is opt-in
# (kept separate so a stuck checkbox can never shadow detection).
follow = st.session_state.get("follow_det", True)
if follow:
    eff_over, eff_flood, eff_slope = set(det_over), det_flood, det_slope
else:
    eff_over = {c for c in OVERLAY_CATEGORIES if st.session_state.get(f"m_{c}")}
    eff_flood = st.session_state.get("m_flood", det_flood)
    eff_slope = st.session_state.get("m_slope", det_slope)

badge = [chip(o, OVERLAY_COLORS.get(o, "#2b8cbe")) for o in sorted(eff_over)]
if eff_flood:
    badge.append(chip("floodplain", OVERLAY_COLORS.get("floodplain", "#2b8cbe")))
if eff_slope:
    badge.append(chip("critical slopes", "#e6550d"))
st.markdown(("Overlays at this parcel: " + " ".join(badge)) if badge
            else "_No overlays at this parcel._", unsafe_allow_html=True)

with st.expander("Adjust overlays"):
    follow = st.checkbox("Use overlays detected at the pin (recommended)", value=follow,
                         key="follow_det")
    if follow:
        st.caption("Detected from the map: "
                   + (", ".join(sorted(det_over)
                                + (["floodplain"] if det_flood else [])
                                + (["critical slopes"] if det_slope else [])) or "none"))
        eff_over, eff_flood, eff_slope = set(det_over), det_flood, det_slope
    else:
        st.caption("Manual override \u2014 these drive the routing.")
        ac = st.columns(2)
        chosen = set()
        with ac[0]:
            for cat in OVERLAY_CATEGORIES:
                if st.checkbox(cat, value=cat in det_over, key=f"m_{cat}"):
                    chosen.add(cat)
        with ac[1]:
            f = st.checkbox("In floodplain", value=det_flood, key="m_flood")
            s = st.checkbox("Critical slopes impacted", value=det_slope, key="m_slope")
        eff_over, eff_flood, eff_slope = chosen, f, s

overlays = ManualOverlayProvider().resolve(overlays=eff_over, floodplain=eff_flood,
                                           critical_slopes=eff_slope)

st.divider()


# --------------------------------------------------------------------------- #
#  2. PROJECT                                                                  #
# --------------------------------------------------------------------------- #
st.subheader("2 \u00b7 What are you doing?")
pcol = st.columns([2, 2, 3])
with pcol[0]:
    action = st.selectbox("Project action", ACTION_TYPES)
with pcol[1]:
    use_perm = st.selectbox("Use permission in district", USE_PERMISSIONS)
with pcol[2]:
    relief = st.checkbox("Seeking dimensional relief (setback, height, \u2026)")

st.divider()


# --------------------------------------------------------------------------- #
#  3. THE PROCESS  (the main event)                                            #
# --------------------------------------------------------------------------- #
st.subheader("3 \u00b7 The process that has to happen")
procs = nav.required_processes(action_type=action, overlays=overlays,
                               use_permission=use_perm, dimensional_relief=relief)

if not procs:
    st.info("No approval process matched. Adjust the action or overlays above.")
    st.stop()

# routed sections (dedup, keep label)
sections = {}
for p in procs:
    for r in nav.route(p):
        if r.section:
            sections.setdefault(r.section, p)


def authority_sentence(a: dict) -> str:
    if not a:
        return ""
    parts = []
    if a.get("recommend") and a["recommend"] not in ("\u2014", "-"):
        parts.append(f"{a['recommend']} reviews & recommends")
    if a.get("decide") and a["decide"] not in ("\u2014", "-"):
        parts.append(f"**{a['decide']} decides**")
    if a.get("appeal") and a["appeal"] not in ("\u2014", "-"):
        parts.append(f"appeal to {a['appeal']}")
    s = "; ".join(parts)
    h = a.get("hearing")
    if h and h not in ("None", "\u2014", "-"):
        s += f" \u2014 {h}"
    return s


if len(sections) > 1:
    choice = st.radio("This project triggers more than one process:", list(sections),
                      format_func=lambda s: sections[s], horizontal=True)
else:
    choice = next(iter(sections))
    st.markdown(f"Your project requires: **{sections[choice]}**")

auth = nav.authority_for(choice)
sentence = authority_sentence(auth)
if sentence:
    st.markdown(f"\u2192 {sentence}")

# The diagram.
try:
    if choice == "5.2.5" and os.path.exists(rezoning_path):
        main_xml = open(rezoning_path, encoding="utf-8").read()
    else:
        main_xml = build_stub_bpmn(choice, auth)
    spine_xml = open(spine_path, encoding="utf-8").read()
    render_bpmn_viewer(main_xml, spine_xml, height=540)
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not render the process model: {exc}")


# --------------------------------------------------------------------------- #
#  Everything else — collapsed, out of the way                                 #
# --------------------------------------------------------------------------- #
with st.expander("Who decides \u2014 authority detail"):
    for p in procs:
        rows = []
        for r in nav.route(p):
            if r.section and r.authority:
                a = r.authority
                rows.append({"Sec.": r.section, "Review / Recommend": a.get("recommend", "\u2014"),
                             "Final Decision": a.get("decide", "\u2014"),
                             "Appeal": a.get("appeal", "\u2014"), "Hearing": a.get("hearing", "\u2014")})
        if rows:
            st.markdown(f"**{p}**")
            st.dataframe(rows, hide_index=True, use_container_width=True)

with st.expander("Shared intake \u2014 Common Review Procedures (5.2.1)"):
    st.caption("Every routed process runs this intake first.")
    for s in nav.spine_steps():
        st.markdown(f"\u25AB {s['name']}", help=s["doc"] or None)
    for c in nav.spine_clocks():
        st.caption(f"\u2022 {c}")

runnable = [s for s in sections if s in nav.models]
if runnable:
    with st.expander("Step through it live (SpiffWorkflow)"):
        pick = runnable[0] if len(runnable) == 1 else st.selectbox("Process", runnable)
        with st.popover("Decision variables"):
            pre_waived = st.checkbox("Pre-application conference waived", value=True)
            complete_ok = st.checkbox("Application complete on first submittal", value=True)
            in_hist = st.checkbox("Property in ADC/HC/IPP overlay",
                                  value=bool(overlays.overlays & {"ADC", "HC", "IndividuallyProtected"}))
            has_proffers = st.checkbox("Includes proffered conditions", value=True)
            proffers_changed = st.checkbox("Proffers changed at PC hearing", value=False)
            council_decision = st.selectbox("Council decision", ["approve", "deny"])
            council_mod = st.selectbox("Council proffer-modification handling",
                                       ["decline", "continue", "refer"])
        seed = {"preAppWaived": pre_waived, "complete": complete_ok, "inHistoricOverlay": in_hist,
                "hasProffers": has_proffers, "proffersChangedAtHearing": proffers_changed,
                "councilDecision": council_decision, "councilModOption": council_mod,
                "revisionCount": 0}
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
                for h in state["history"]:
                    st.markdown(f"\u2705 {h}")
                if not state["history"]:
                    st.caption("nothing yet")
            with colB:
                st.markdown("**Ready now**")
                for t in state["ready"]:
                    if st.button(f"\u25B8 {t['name']}", key=f"run_{t['id']}", help=t["doc"] or None):
                        runner.complete(t["id"])
                        st.rerun()
                if not state["ready"] and not state["complete"]:
                    st.caption("(engine step / waiting)")
            if state["complete"]:
                st.success(f"Complete \u2014 reached: {', '.join(state['ended']) or 'end'}")
