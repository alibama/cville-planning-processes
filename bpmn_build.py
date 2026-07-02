"""
bpmn_build.py — emit valid BPMN 2.0 (+ layered DI) from a node/edge spec, and
generate a per-process "overview" model on the fly from the authority matrix so
every routed process renders a distinct diagram (each invoking the 5.2.1 spine).

Pure/stringly — no Streamlit or Spiff dependency; testable headless.
"""
from __future__ import annotations
from xml.sax.saxutils import escape

NS = "https://lexipedia.xyz/cville/dev-code"
SPINE_PROCESS_ID = "Process_CommonReviewProcedures"

SIZE = {
    "startEvent": (36, 36), "endEvent": (36, 36), "exclusiveGateway": (50, 50),
    "userTask": (150, 80), "callActivity": (170, 90), "subProcess": (160, 90),
}
COL_W, ROW_H, X0, Y0 = 220, 150, 60, 340


def _bounds(node):
    _id, kind, _name, rank, row = node[0], node[1], node[2], node[3], node[4]
    w, h = SIZE.get(kind, (150, 80))
    cx, cy = X0 + rank * COL_W, Y0 + row * ROW_H
    return cx - w / 2, cy - h / 2, w, h


def emit_bpmn(process_id: str, name: str, nodes: list, flows: list,
              targetns: str = NS) -> str:
    """nodes: (id, kind, name, rank, row, doc, called_element_or_None)
       flows: (id, src, tgt, label, condition_or_None, is_default_bool)"""
    bounds = {n[0]: _bounds(n) for n in nodes}
    rank = {n[0]: n[3] for n in nodes}

    def incoming(nid): return [f[0] for f in flows if f[2] == nid]
    def outgoing(nid): return [f[0] for f in flows if f[1] == nid]

    def default_of(nid):
        for f in flows:
            if f[1] == nid and f[5]:
                return f[0]
        return None

    def anchors(src, tgt):
        sx, sy, sw, sh = bounds[src]
        tx, ty, tw, th = bounds[tgt]
        if rank[tgt] > rank[src]:
            return [(sx + sw, sy + sh / 2), (tx, ty + th / 2)]
        depth = max(sy + sh, ty + th) + 70
        return [(sx + sw / 2, sy + sh), (sx + sw / 2, depth),
                (tx + tw / 2, depth), (tx + tw / 2, ty + th)]

    o = []
    a = o.append
    a('<?xml version="1.0" encoding="UTF-8"?>')
    a('<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
      'xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" '
      'xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" '
      'xmlns:di="http://www.omg.org/spec/DD/20100524/DI" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
      f'id="Definitions_{process_id}" targetNamespace="{targetns}">')
    a(f'  <bpmn:process id="{process_id}" name="{escape(name)}" isExecutable="false">')
    for nid, kind, nm, _r, _row, doc, called in nodes:
        io = "".join(f'\n      <bpmn:incoming>{f}</bpmn:incoming>' for f in incoming(nid))
        io += "".join(f'\n      <bpmn:outgoing>{f}</bpmn:outgoing>' for f in outgoing(nid))
        docx = f'\n      <bpmn:documentation>{escape(doc)}</bpmn:documentation>' if doc else ""
        if kind == "callActivity":
            a(f'    <bpmn:callActivity id="{nid}" name="{escape(nm)}" calledElement="{called}">{docx}{io}\n    </bpmn:callActivity>')
        elif kind == "exclusiveGateway":
            d = default_of(nid)
            da = f' default="{d}"' if d else ""
            a(f'    <bpmn:exclusiveGateway id="{nid}" name="{escape(nm)}"{da}>{docx}{io}\n    </bpmn:exclusiveGateway>')
        elif kind == "startEvent":
            a(f'    <bpmn:startEvent id="{nid}" name="{escape(nm)}">{docx}{io}\n    </bpmn:startEvent>')
        elif kind == "endEvent":
            a(f'    <bpmn:endEvent id="{nid}" name="{escape(nm)}">{docx}{io}\n    </bpmn:endEvent>')
        else:
            a(f'    <bpmn:userTask id="{nid}" name="{escape(nm)}">{docx}{io}\n    </bpmn:userTask>')
    for fid, src, tgt, label, cond, _d in flows:
        na = f' name="{escape(label)}"' if label else ""
        if cond:
            a(f'    <bpmn:sequenceFlow id="{fid}"{na} sourceRef="{src}" targetRef="{tgt}">')
            a(f'      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">{escape(cond)}</bpmn:conditionExpression>')
            a('    </bpmn:sequenceFlow>')
        else:
            a(f'    <bpmn:sequenceFlow id="{fid}"{na} sourceRef="{src}" targetRef="{tgt}" />')
    a('  </bpmn:process>')
    a(f'  <bpmndi:BPMNDiagram id="Diagram_{process_id}">')
    a(f'    <bpmndi:BPMNPlane id="Plane_{process_id}" bpmnElement="{process_id}">')
    for nid, kind, _nm, _r, _row, _doc, _c in nodes:
        x, y, w, h = bounds[nid]
        mk = ' isMarkerVisible="true"' if kind == "exclusiveGateway" else ""
        a(f'      <bpmndi:BPMNShape id="di_{nid}" bpmnElement="{nid}"{mk}>')
        a(f'        <dc:Bounds x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" />')
        a('      </bpmndi:BPMNShape>')
    for fid, src, tgt, _l, _c, _d in flows:
        a(f'      <bpmndi:BPMNEdge id="di_{fid}" bpmnElement="{fid}">')
        for wx, wy in anchors(src, tgt):
            a(f'        <di:waypoint x="{wx:.0f}" y="{wy:.0f}" />')
        a('      </bpmndi:BPMNEdge>')
    a('    </bpmndi:BPMNPlane>')
    a('  </bpmndi:BPMNDiagram>')
    a('</bpmn:definitions>')
    return "\n".join(o)


def build_stub_bpmn(section: str, authority: dict | None) -> str:
    """Generate an overview model for a process from its authority-matrix row:
    Start -> Common Review Procedures (5.2.1) call -> [recommend] -> decide(hearing)
    -> outcome gateway -> Approved / (Appeal to body | Denied)."""
    au = authority or {}
    rec = au.get("recommend") or "-"
    dec = au.get("decide") or "Administrator"
    app = au.get("appeal") or "-"
    hear = au.get("hearing") or "None"

    nodes, flows, r = [], [], 0
    nodes.append(("Start", "startEvent", f"{section} initiated", r, 0, "", None)); r += 1
    nodes.append(("Call_Spine", "callActivity", "Common Review Procedures (5.2.1)", r, 0,
                  "Shared intake spine (5.2.1). Click to drill into it.", SPINE_PROCESS_ID)); r += 1
    flows.append(("f_start", "Start", "Call_Spine", "", None, False))
    prev = "Call_Spine"
    if rec != "-":
        nodes.append(("Recommend", "userTask", f"{rec}: review & recommend", r, 0, "", None))
        flows.append(("f_rec", prev, "Recommend", "", None, False)); prev = "Recommend"; r += 1
    dname = f"{dec} decision" + (f" ({hear})" if hear not in ("None", "-") else "")
    nodes.append(("Decision", "userTask", dname, r, 0, "", None))
    flows.append(("f_dec", prev, "Decision", "", None, False)); r += 1
    nodes.append(("Gw", "exclusiveGateway", "Decision outcome", r, 0, "", None))
    flows.append(("f_gw", "Decision", "Gw", "", None, False)); r += 1
    nodes.append(("End_Approved", "endEvent", "Approved", r, 0, "", None))
    flows.append(("f_ok", "Gw", "End_Approved", "approved", None, True))
    if app != "-":
        nodes.append(("Appeal", "userTask", f"Appeal to {app}", r, 1, "", None))
        nodes.append(("End_Appeal", "endEvent", "After appeal", r + 1, 1, "", None))
        flows.append(("f_app", "Gw", "Appeal", "appealed", "outcome == 'appeal'", False))
        flows.append(("f_ae", "Appeal", "End_Appeal", "", None, False))
    else:
        nodes.append(("End_Denied", "endEvent", "Denied", r, 1, "", None))
        flows.append(("f_deny", "Gw", "End_Denied", "denied", "outcome == 'deny'", False))

    pid = "Process_" + section.replace(".", "_")
    return emit_bpmn(pid, f"{section} (overview generated from the authority matrix)", nodes, flows)
