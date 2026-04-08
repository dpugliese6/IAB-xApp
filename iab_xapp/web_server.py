"""
Flask web server for the IAB xApp topology visualisation.
"""

from flask import Flask, jsonify, render_template_string

import state
import topology as topo
import handover_controller as ho


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Network Topology</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  body { margin: 0; background: #1a1a2e; color: #eee; font-family: sans-serif; }
  h2   { text-align: center; padding: 12px; margin: 0; color: #a0c4ff; }
  #net { width: 100vw; height: calc(100vh - 50px); }
  #legend {
    position: absolute; top: 60px; left: 16px;
    background: rgba(0,0,0,0.6); border-radius: 8px; padding: 10px 16px;
    font-size: 13px;
  }
  .leg-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
  .leg-line  { width: 36px; height: 3px; }
  .leg-shape { width: 18px; height: 18px; display: inline-block; }
  .solid     { background: #74c0fc; }
  .dashdot   { background: repeating-linear-gradient(90deg,#f08c00 0,#f08c00 6px,transparent 6px,transparent 10px); }
  .gnb-shape  { background: #4dabf7; border-radius: 3px; }
  .iab-shape  { background: #e599f7; border-radius: 3px; }
  .ue-shape   { background: #a9e34b; border-radius: 50%; }
  .disc-shape { background: #555; border-radius: 3px; opacity: 0.4; }
  #status {
    position: absolute; top: 60px; right: 16px;
    background: rgba(0,0,0,0.6); border-radius: 8px; padding: 6px 12px;
    font-size: 12px; color: #a9e34b;
  }
  #ho-btn {
    position: absolute; bottom: 24px; right: 24px;
    background: #7048e8; color: #fff; border: none; border-radius: 8px;
    padding: 10px 20px; font-size: 14px; cursor: pointer;
  }
  #ho-btn:hover { background: #5f3dc4; }
  #ho-result {
    position: absolute; bottom: 72px; right: 24px; max-width: 420px;
    background: rgba(0,0,0,0.75); border-radius: 8px; padding: 10px 14px;
    font-size: 12px; color: #ddd; display: none; white-space: pre-wrap;
  }
</style>
</head>
<body>
<h2>Network Topology</h2>
<div id="legend">
  <div class="leg-item"><div class="leg-shape gnb-shape"></div> gNB</div>
  <div class="leg-item"><div class="leg-shape iab-shape"></div> IAB node (+ merged UEs)</div>
  <div class="leg-item"><div class="leg-shape ue-shape"></div> UE</div>
  <div class="leg-item"><div class="leg-shape disc-shape"></div> Disconnected</div>
  <hr style="border-color:#555; margin:6px 0;">
  <div class="leg-item"><div class="leg-line solid"></div> Serving</div>
  <div class="leg-item"><div class="leg-line dashdot"></div> Neighbor</div>
</div>
<div id="status">Waiting for data...</div>
<div id="net"></div>
<div id="ho-result"></div>
<button id="ho-btn" onclick="triggerHandover()">Optimize &amp; Handover</button>
<script>
const IAB_COLOR  = { background: "#e599f7", border: "#9c36b5" };
const GNB_COLOR  = { background: "#4dabf7", border: "#1971c2" };
const UE_COLOR   = { background: "#a9e34b", border: "#5c940d" };
const DISC_COLOR = { background: "#555", border: "#888" };

function toVisNode(n) {
  const isUe = n.type === "ue";
  const disconnected = !isUe && n.connected === false;
  const color = disconnected ? DISC_COLOR : (n.type === "iab" ? IAB_COLOR : (isUe ? UE_COLOR : GNB_COLOR));
  return {
    id: n.id, label: n.label,
    shape: isUe ? "dot" : "box",
    size:  isUe ? 14 : 28,
    color: color,
    opacity: disconnected ? 0.4 : 1.0,
    font:  { color: disconnected ? "#aaa" : "#fff", size: 11, multi: "md" },
    margin: isUe ? undefined : 10,
  };
}

function toVisEdge(e, i) {
  return {
    id: i, from: e.from, to: e.to,
    label:  e.rsrp !== undefined ? e.rsrp.toFixed(1) + " dBm" : "",
    color:  { color: e.style === "solid" ? "#74c0fc" : "#f08c00" },
    dashes: e.style === "dashdot" ? [6, 4, 2, 4] : false,
    width:  e.style === "solid" ? 2.5 : 1.5,
    font:   { size: 9, color: "#ccc", align: "middle" },
    smooth: { type: "curvedCW", roundness: 0.2 },
  };
}

const nodes   = new vis.DataSet([]);
const edges   = new vis.DataSet([]);
const network = new vis.Network(
  document.getElementById("net"),
  { nodes, edges },
  {
    physics: {
      solver: "forceAtlas2Based",
      forceAtlas2Based: { gravitationalConstant: -60, springLength: 160 },
      stabilization: { iterations: 300 },
    },
    interaction: { hover: true, tooltipDelay: 100 },
  }
);

const status = document.getElementById("status");

async function refresh() {
  try {
    const res   = await fetch("/graph?" + Date.now());
    const graph = await res.json();

    const newNodeIds = new Set(graph.nodes.map(n => n.id));
    const newEdgeIds = new Set(graph.edges.map((_, i) => i));

    nodes.remove(nodes.getIds().filter(id => !newNodeIds.has(id)));
    edges.remove(edges.getIds().filter(id => !newEdgeIds.has(id)));
    nodes.update(graph.nodes.map(toVisNode));
    edges.update(graph.edges.map(toVisEdge));

    status.textContent = "Last update: " + new Date().toLocaleTimeString();
    status.style.color = "#a9e34b";
  } catch (err) {
    status.textContent = "Update failed: " + err.message;
    status.style.color = "#ff6b6b";
  }
}

refresh();
setInterval(refresh, 5000);

async function triggerHandover() {
  const btn = document.getElementById("ho-btn");
  const box = document.getElementById("ho-result");
  btn.disabled = true;
  btn.textContent = "Running...";
  box.style.display = "none";
  try {
    const res  = await fetch("/handover", { method: "POST" });
    const data = await res.json();
    if (data.decisions.length === 0) {
      box.textContent = "No handovers needed (no neighbor exceeds serving by 5 dBm).";
    } else {
      box.textContent = data.decisions.map(d =>
        `UE ${d.ue_id}: ${d.current_gnb.slice(-8)} (${d.current_rsrp.toFixed(1)} dBm) → ${d.target_gnb.slice(-8)} (${d.target_rsrp.toFixed(1)} dBm)`
      ).join("\\n") + `\\n\\nSent: ${data.executed}/${data.decisions.length}`;
    }
    box.style.display = "block";
  } catch (err) {
    box.textContent = "Error: " + err.message;
    box.style.display = "block";
  } finally {
    btn.disabled = false;
    btn.textContent = "Optimize & Handover";
  }
}
</script>
</body>
</html>"""


def create_app():
    app = Flask(__name__)

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/")
    def index():
        return render_template_string(HTML)

    @app.route("/graph")
    def graph():
        topology = topo.topology_from_memory()
        return jsonify(topo.build_graph(
            state.gnb_list_global,
            state.gnb_neighbors_global,
            topology,
            state.iab_associations_global,
            state.subscribed_gnbs_global,
        ))

    @app.route("/handover", methods=["POST"])
    def handover():
        topology  = topo.topology_from_memory()
        decisions = ho.compute_ho_decisions(topology)
        executed  = ho.execute_handovers(decisions)
        return jsonify({"decisions": decisions, "executed": executed})

    return app
