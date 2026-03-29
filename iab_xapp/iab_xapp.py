import time
import signal
import argparse
import numpy as np
import re
import csv
import threading
from flask import Flask, jsonify, render_template_string
import json

import setup_imports

from xDevSM.handlers.xDevSM_rmr_xapp import xDevSMRMRXapp

# import xDevSM kpm decorator
from xDevSM.decorators.kpm.kpm_frame import XappKpmFrame


from xDevSM.sm_framework.py_oran.kpm.enums import format_action_def_e
from xDevSM.sm_framework.py_oran.kpm.enums import format_ind_msg_e
from xDevSM.sm_framework.py_oran.kpm.enums import meas_type_enum
from xDevSM.sm_framework.py_oran.kpm.enums import meas_value_e

global logger

STALE_TIMEOUT_MS = 5000

# SST and SD ranges for subscriptions
SST_SET = [1, 2]
SD_RANGE = list(range(0, 11))  # 0 to 10

# topology_data[i] = list of measurement dicts for gnb_list[i]
# each dict: {timestamp, sst, sd, ue_id, value_serv, value_neigh_0, ...}
topology_data        = []
gnb_list_global      = []
gnb_neighbors_global = []
gnb_count_global     = 0
csv_file_global      = None
kpm_xapp_global      = None
iab_associations_global = {}  # gnb_name -> list of (sst, sd)
subscribed_gnbs_global = set()  # gnb_name -> currently subscribed/connected


def _upsert_record(gnb_idx, ue_id, sst, sd, record):
    for i, r in enumerate(topology_data[gnb_idx]):
        if r["ue_id"] == ue_id and r["sst"] == sst and r["sd"] == sd:
            topology_data[gnb_idx][i] = record
            return
    topology_data[gnb_idx].append(record)


def _cleanup_stale(gnb_idx):
    now_ms = int(time.time() * 1000)
    topology_data[gnb_idx] = [
        r for r in topology_data[gnb_idx]
        if now_ms - r["timestamp"] <= STALE_TIMEOUT_MS
    ]


def _cleanup_all_stale():
    """Remove stale records from all gNBs."""
    for gnb_idx in range(len(topology_data)):
        _cleanup_stale(gnb_idx)


def _periodic_cleanup():
    """Background thread that periodically removes stale records."""
    while True:
        time.sleep(STALE_TIMEOUT_MS / 1000)
        _cleanup_all_stale()


def write_csv():
    if csv_file_global is None:
        return
    fieldnames = ["timestamp", "gnb_id", "sst", "sd", "ue_id", "value_serv"] + \
                 [f"value_neigh_{i}" for i in range(gnb_count_global)]
    with open(csv_file_global, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for gnb_idx, records in enumerate(topology_data):
            for r in records:
                writer.writerow({"gnb_id": gnb_list_global[gnb_idx], **r})


def _extract_slice_from_labels(ind_msg_format_1):
    """Extract (sst, sd) from the first measurement label that has a sliceID.
    Tries meas_info_lst_len first, falls back to meas_record_len of the last data entry."""
    num_info = ind_msg_format_1.meas_info_lst_len
    if num_info == 0 and ind_msg_format_1.meas_data_lst_len > 0:
        j = ind_msg_format_1.meas_data_lst_len - 1
        num_info = ind_msg_format_1.meas_data_lst[j].meas_record_len

    for k in range(num_info):
        try:
            meas_info = ind_msg_format_1.meas_info_lst[k]
            if meas_info.label_info_lst_len > 0 and meas_info.label_info_lst:
                label = meas_info.label_info_lst[0]
                if label.sliceID:
                    sst = label.sliceID.contents.sST
                    sd = label.sliceID.contents.sD.contents.value if label.sliceID.contents.sD else 0
                    return int(sst), int(sd)
        except Exception:
            continue
    return None, None


def indication_callback(ind_hdr, ind_msg, meid, sub_id=None):
    gnbid = meid.decode('utf-8')
    logger.info("[Main] Received indication message from {} (sub_id={})".format(gnbid, sub_id))
    # Decoding sender_name
    sender_name = None
    if ind_hdr.data.kpm_ric_ind_hdr_format_1.sender_name:
        my_string = bytes(np.ctypeslib.as_array(ind_hdr.data.kpm_ric_ind_hdr_format_1.sender_name.contents.buf, shape = (ind_hdr.data.kpm_ric_ind_hdr_format_1.sender_name.contents.len,)))
        sender_name = my_string.decode('utf-8')

    if sender_name is None:
        logger.info("[Main] Sender name not specified in the indication message")

    try:
        gnb_idx = gnb_list_global.index(gnbid)
    except ValueError:
        logger.warning("[Main] gNB {} not in gnb_list, skipping".format(gnbid))
        return

    if ind_msg.type.value == format_ind_msg_e.FORMAT_3_INDICATION_MESSAGE:
        for i in range(ind_msg.data.frm_3.ue_meas_report_lst_len):
            # for each ue
            meas_report_ue = ind_msg.data.frm_3.meas_report_per_ue[i]

            ue_id = kpm_xapp_global.get_ue_id(meas_report_ue.ue_meas_report_lst)

            logger.info("[Main] gnb: {}, sender_name: {}, ue: {}".format(gnbid, sender_name, ue_id))
            ind_msg_format_1 = meas_report_ue.ind_msg_format_1

            # Extract sst/sd from measurement labels first, then subscription context
            sst, sd = _extract_slice_from_labels(ind_msg_format_1)
            if sst is not None:
                logger.info("[Main] Extracted slice from labels: sst={} sd={}".format(sst, sd))
            else:
                # Try subscription context lookup — sub_id from RMR may be int, keys in context may be str
                ctx = None
                if sub_id is not None:
                    ctx = kpm_xapp_global.subscription_context.get(sub_id) or \
                          kpm_xapp_global.subscription_context.get(str(sub_id))
                if ctx:
                    sst, sd = ctx["sst"], ctx["sd"]
                    logger.info("[Main] Extracted slice from subscription context (sub_id={}): sst={} sd={}".format(sub_id, sst, sd))
                else:
                    sst, sd = 1, 1  # fallback
                    logger.warning("[Main] Could not extract slice info (sub_id={} type={}, context_keys={}), using default sst={} sd={}".format(
                        sub_id, type(sub_id).__name__, list(kpm_xapp_global.subscription_context.keys()), sst, sd))

            meas_data_lst = ind_msg_format_1.meas_data_lst
            neigh_pattern = re.compile(r'L1M\.SS-RSRPNrNbr\.(\d+)\.\d+')
            serv_pattern  = re.compile(r'L1M\.SS-RSRP\.\d+')

            for j in range(ind_msg_format_1.meas_data_lst_len):
                serv_vals = []
                neigh_vals = {}  # { n: [values...] }

                for k in range(meas_data_lst[j].meas_record_len):
                    meas_record_lst_el = meas_data_lst[j].meas_record_lst[k]
                    if ind_msg_format_1.meas_info_lst[k].meas_type.type.value == meas_type_enum.NAME_MEAS_TYPE:
                        meas_type     = ind_msg_format_1.meas_info_lst[k].meas_type.value.name
                        meas_record   = meas_record_lst_el

                        meas_type_bs  = bytes(np.ctypeslib.as_array(meas_type.buf, shape=(meas_type.len,)))
                        meas_type_str = meas_type_bs.decode('utf-8')

                        if meas_record.value.value == meas_value_e.INTEGER_MEAS_VALUE:
                            val = meas_record.union.int_val
                        elif meas_record.value.value == meas_value_e.REAL_MEAS_VALUE:
                            val = meas_record.union.real_val
                        else:
                            continue

                        logger.info("{}:{}".format(meas_type_str, val))

                        if serv_pattern.fullmatch(meas_type_str):
                            serv_vals.append(val)

                        elif m := neigh_pattern.fullmatch(meas_type_str):
                            n = int(m.group(1))
                            neigh_vals.setdefault(n, []).append(val)
                    else:
                        logger.info("[Main] Not supported meas type {}".format(ind_msg_format_1.meas_info_lst[k].meas_type.type.value))

                # After inner loop — compute and discard if serv_RSRP is 0
                serv_RSRP = np.mean([v for v in serv_vals if v != 0]) if any(v != 0 for v in serv_vals) else 0
                if serv_RSRP == 0:
                    continue

                neigh_RSRP = np.full(gnb_count_global, 0)
                for n, vals in neigh_vals.items():
                    non_zero = [v for v in vals if v != 0]
                    if non_zero:
                        neigh_RSRP[n] = np.mean(non_zero)

                record = {
                    "timestamp":  int(time.time() * 1000),
                    "sst":        sst,
                    "sd":         sd,
                    "ue_id":      str(ue_id),
                    "value_serv": float(serv_RSRP),
                    **{f"value_neigh_{i}": float(neigh_RSRP[i]) for i in range(gnb_count_global)},
                }
                _upsert_record(gnb_idx, str(ue_id), sst, sd, record)

            _cleanup_stale(gnb_idx)

        write_csv()

    else:
        logger.info("[Main] format not supported for storing")


def shutdown():
    logger.info("[Main] Shutting down")
    write_csv()


def sub_failed_callback(json_data):
    logger.info("[Main]subscription failed: {}".format(json_data))



def parse_plmn(plmn_str):
    return {
        "mcc":        int(re.search(r'mcc\s*=\s*(\d+)', plmn_str).group(1)),
        "mnc":        int(re.search(r'mnc\s*=\s*(\d+)', plmn_str).group(1)),
        "mnc_length": int(re.search(r'mnc_length\s*=\s*(\d+)', plmn_str).group(1)),
    }


def parse_value(raw):
    """Return hex string (e.g. 'e01') or int depending on the value format."""
    raw = raw.strip()
    if raw.lower().startswith("0x"):
        return raw[2:].lower()   # strip '0x', keep as hex string e.g. 'e01'
    return int(raw)


def get_field(key, block):
    m = re.search(rf'{key}\s*=\s*(0x[0-9a-fA-F]+|\d+)', block)
    return parse_value(m.group(1)) if m else None


def parse_neighbor_cell(block):
    plmn_m = re.search(r'plmn\s*=\s*\{([^}]+)\}', block)
    return {
        "gNB_ID":               get_field("gNB_ID", block),
        "nr_cellid":            get_field("nr_cellid", block),
        "physical_cellId":      get_field("physical_cellId", block),
        "absoluteFrequencySSB": get_field("absoluteFrequencySSB", block),
        "subcarrierSpacing":    get_field("subcarrierSpacing", block),
        "band":                 get_field("band", block),
        "plmn":                 parse_plmn(plmn_m.group(1)) if plmn_m else None,
        "tracking_area_code":   get_field("tracking_area_code", block),
    }


def split_blocks(text, open_ch="{", close_ch="}"):
    """Split text into top-level brace-delimited blocks."""
    blocks, depth, start = [], 0, None
    for i, ch in enumerate(text):
        if ch == open_ch:
            if depth == 0:
                start = i
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(text[start + 1:i])
                start = None
    return blocks


def strip_comments(content):
    """Remove # comments but preserve hex literals like 0xe00."""
    return re.sub(r'(?<![0-9a-fA-F])#[^\n]*', '', content)


def parse_entry(entry_block):
    ncfg_m = re.search(r'neighbour_cell_configuration\s*=\s*\((.+?)\)\s*;', entry_block, re.DOTALL)
    neighbors = []
    if ncfg_m:
        for nb_block in split_blocks(ncfg_m.group(1)):
            neighbors.append(parse_neighbor_cell(nb_block))

    # Only parse plmn from the entry block, excluding neighbour_cell_configuration
    entry_header = entry_block[:entry_block.find("neighbour_cell_configuration")] if "neighbour_cell_configuration" in entry_block else entry_block
    plmn_m = re.search(r'plmn\s*=\s*\{([^}]+)\}', entry_header)

    return {
        "gNB_ID":          get_field("gNB_ID", entry_header),
        "nr_cellid":       get_field("nr_cellid", entry_header),
        "physical_cellId": get_field("physical_cellId", entry_header),
        "plmn":            parse_plmn(plmn_m.group(1)) if plmn_m else None,
        "neighbours":      neighbors,
    }


def parse_neighbor_list(filepath):
    with open(filepath, "r") as f:
        content = f.read()

    content = strip_comments(content)

    nl_m = re.search(r'neighbour_list\s*=\s*\((.+)\)\s*;', content, re.DOTALL)
    if not nl_m:
        print("DEBUG: Could not find neighbour_list block. Stripped content preview:")
        print(repr(content[:300]))
        raise ValueError("neighbour_list block not found in file")

    entries = []
    for entry_block in split_blocks(nl_m.group(1)):
        entries.append(parse_entry(entry_block))

    return entries


def parse_iab_associations(filepath):
    """Parse iab_associations.conf and return a dict mapping gnb_name -> list of (sst, sd)."""
    with open(filepath, "r") as f:
        content = f.read()

    content = strip_comments(content)

    nl_m = re.search(r'iab_nodes\s*=\s*\((.+)\)\s*;', content, re.DOTALL)
    if not nl_m:
        raise ValueError("iab_nodes block not found in file")

    associations = {}
    for block in split_blocks(nl_m.group(1)):
        plmn_m = re.search(r'plmn\s*=\s*\{([^}]+)\}', block)
        if not plmn_m:
            continue
        plmn = parse_plmn(plmn_m.group(1))
        gnb_id = get_field("gNB_ID", block)
        sst = get_field("sst", block)
        sd = get_field("sd", block)
        if gnb_id is None or sst is None or sd is None:
            continue

        mcc_str = str(plmn["mcc"]).zfill(3)
        mnc_str = f"0{plmn['mnc']}" if plmn["mnc_length"] == 2 else str(plmn["mnc"])
        gnb_id_str = gnb_id if isinstance(gnb_id, str) else str(gnb_id)
        gnb_name = f"gnb_{mcc_str}_{mnc_str}_{gnb_id_str.zfill(8)}"

        associations.setdefault(gnb_name, []).append((sst, sd))

    return associations


def get_gnb_list(neighbor_list):
    result = []
    for entry in neighbor_list:
        plmn = entry["plmn"]
        mcc_str = str(plmn["mcc"]).zfill(3)
        mnc_str = f"0{plmn['mnc']}" if plmn["mnc_length"] == 2 else str(plmn["mnc"])
        gnb_id_str = entry["gNB_ID"].zfill(8)
        result.append(f"gnb_{mcc_str}_{mnc_str}_{gnb_id_str}")
    return result


def get_gnb_neighbors(neighbor_list, gnb_list):
    result = []
    for entry in neighbor_list:
        neighbors = []
        for nb in entry["neighbours"]:
            plmn = nb["plmn"]
            mcc_str = str(plmn["mcc"]).zfill(3)
            mnc_str = f"0{plmn['mnc']}" if plmn["mnc_length"] == 2 else str(plmn["mnc"])
            gnb_id_str = nb["gNB_ID"].zfill(8)
            neighbors.append(f"gnb_{mcc_str}_{mnc_str}_{gnb_id_str}")
        result.append(neighbors)
    return result


def topology_from_memory():
    topology = {gnb: {} for gnb in gnb_list_global}
    for gnb_idx, records in enumerate(topology_data):
        gnb_id  = gnb_list_global[gnb_idx]
        nb_list = gnb_neighbors_global[gnb_idx]
        for r in records:
            neighbor_rsrp = {
                nb_gnb: r[f"value_neigh_{i}"]
                for i, nb_gnb in enumerate(nb_list)
                if r.get(f"value_neigh_{i}", 0) != 0
            }
            ue_key = "{}:sst{}sd{}".format(r["ue_id"], r["sst"], r["sd"])
            topology[gnb_id][ue_key] = {
                "ue_id":         r["ue_id"],
                "sst":           r["sst"],
                "sd":            r["sd"],
                "serving_rsrp":  r["value_serv"],
                "neighbor_rsrp": neighbor_rsrp,
            }
    return topology


def build_graph(gnb_list, gnb_neighbors, topology, iab_associations, connected_gnbs=None):
    nodes, edges = [], []
    seen_edges = set()
    if connected_gnbs is None:
        connected_gnbs = set(gnb_list)

    # Collect UEs that belong to an IAB node (they will be merged into the IAB node)
    # iab_ue_keys: set of ue_keys absorbed into an IAB node
    iab_ue_keys = set()
    # iab_ues: iab_gnb -> list of UE labels absorbed
    iab_ues = {gnb: [] for gnb in iab_associations}

    for gnb_id, ues in topology.items():
        for ue_key, data in ues.items():
            for iab_gnb, slices in iab_associations.items():
                if (data["sst"], data["sd"]) in slices:
                    iab_ue_keys.add(ue_key)
                    iab_ues[iab_gnb].append("UE {} (s{}/d{})".format(data["ue_id"], data["sst"], data["sd"]))
                    break

    # Build gNB / IAB nodes
    for gnb in gnb_list:
        is_iab = gnb in iab_associations
        connected = gnb in connected_gnbs
        if is_iab:
            absorbed = iab_ues.get(gnb, [])
            label = gnb + "\n" + "\n".join(absorbed) if absorbed else gnb
            nodes.append({
                "id": gnb,
                "type": "iab",
                "label": label,
                "ue_count": len(absorbed),
                "connected": connected,
            })
        else:
            nodes.append({
                "id": gnb,
                "type": "gnb",
                "label": gnb,
                "connected": connected,
            })

    # Build UE nodes and edges (skip UEs absorbed into IAB nodes)
    for gnb_id, ues in topology.items():
        for ue_key, data in ues.items():
            if ue_key in iab_ue_keys:
                # This UE is merged into an IAB node — redirect its edges
                # Find which IAB node absorbed it
                merged_into = None
                for iab_gnb, slices in iab_associations.items():
                    if (data["sst"], data["sd"]) in slices:
                        merged_into = iab_gnb
                        break
                # Add serving edge from IAB node to serving gNB (if different)
                if merged_into and merged_into != gnb_id:
                    edge_key = (merged_into, gnb_id, "serving")
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append({
                            "from":  merged_into,
                            "to":    gnb_id,
                            "style": "solid",
                            "rsrp":  data["serving_rsrp"],
                        })
                # Add neighbor edges from IAB node
                for nb_gnb, nb_rsrp in data["neighbor_rsrp"].items():
                    if nb_gnb == merged_into:
                        continue
                    edge_key = (merged_into, nb_gnb, "neigh")
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append({
                            "from":  merged_into,
                            "to":    nb_gnb,
                            "style": "dashdot",
                            "rsrp":  nb_rsrp,
                        })
            else:
                # Regular UE — show as separate node
                ue_label = "{} (s{}/d{})".format(data["ue_id"], data["sst"], data["sd"])
                nodes.append({
                    "id": ue_key,
                    "type": "ue",
                    "label": ue_label,
                })
                edges.append({
                    "from":  ue_key,
                    "to":    gnb_id,
                    "style": "solid",
                    "rsrp":  data["serving_rsrp"],
                })
                for nb_gnb, nb_rsrp in data["neighbor_rsrp"].items():
                    key = (ue_key, nb_gnb)
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append({
                            "from":  ue_key,
                            "to":    nb_gnb,
                            "style": "dashdot",
                            "rsrp":  nb_rsrp,
                        })

    return {"nodes": nodes, "edges": edges}


# ── HTML template ─────────────────────────────────────────────────────────────

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
        topology = topology_from_memory()
        return jsonify(build_graph(gnb_list_global, gnb_neighbors_global, topology, iab_associations_global, subscribed_gnbs_global))

    return app

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="kpm xApp v3")

    parser.add_argument("-r", "--route_file", metavar="<route_file>",
                        help="path of xApp route file",
                        type=str, default="./config/uta_rtg.rt")

    args = parser.parse_args()

    neighbor_list = parse_neighbor_list("neighborhood.conf")
    gnb_list = get_gnb_list(neighbor_list)
    print(json.dumps(gnb_list, indent=2))
    gnb_neighbors = get_gnb_neighbors(neighbor_list, gnb_list)
    print(json.dumps(gnb_neighbors, indent=2))

    # Parse IAB associations
    iab_associations = parse_iab_associations("iab_associations.conf")
    print("IAB associations:", json.dumps({k: v for k, v in iab_associations.items()}, indent=2))

    # Creating a generic xDevSM RMR xApp
    xapp_gen = xDevSMRMRXapp("0.0.0.0", route_file=args.route_file)
    logger = xapp_gen.logger


    # Adding kpm functionalities to the xapp
    kpm_xapp = XappKpmFrame(xapp_gen,
                            logger,
                            xapp_gen.server,
                            xapp_gen.get_xapp_name(),
                            xapp_gen.rmr_port,
                            xapp_gen.http_port,xapp_gen.get_pltnamespace(),
                            xapp_gen.get_app_namespace())

    # Initialise global topology state
    gnb_list_global      = gnb_list
    gnb_neighbors_global = gnb_neighbors
    gnb_count_global     = len(gnb_list) - 1
    csv_file_global      = "network_topology.csv"
    kpm_xapp_global      = kpm_xapp
    iab_associations_global = iab_associations
    topology_data        = [[] for _ in gnb_list]

    # Start Flask web server in background thread
    flask_app = create_app()

    def run_flask():
        try:
            flask_app.run(host="0.0.0.0", port=5555, debug=False, threaded=True)
        except Exception as e:
            logger.error("[Flask] Web server crashed: {}".format(e))

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("[Main] Flask web server started on port 5555")

    # Start periodic stale-data cleanup thread
    cleanup_thread = threading.Thread(target=_periodic_cleanup, daemon=True)
    cleanup_thread.start()

    # Registering the shutdown function
    xapp_gen.register_shutdown(shutdown)

    # Registering the outermost rmr handler
    xapp_gen.register_handler(kpm_xapp.handle)

    # Registering indication message callback
    kpm_xapp.register_ind_msg_callback(handler=indication_callback)
    # Registering subscription failed callback
    kpm_xapp.register_sub_fail_callback(handler=sub_failed_callback)

    # Registering termination signal handlers
    signal.signal(signal.SIGINT, kpm_xapp.terminate)
    signal.signal(signal.SIGTERM, kpm_xapp.terminate)

    time.sleep(10)

    # Track which gNBs have been successfully subscribed (also exposed to Flask via subscribed_gnbs_global)
    subscribed_gnbs = subscribed_gnbs_global
    ev_trigger_tuple = (0, 1000)

    def subscribe_to_gnb(gnb_name):
        """Try to subscribe to a single gNB for all (sst, sd) combinations."""
        gnb, gnb_info = xapp_gen.get_selected_e2node_info(gnb_name)
        if not gnb:
            return False

        ran_function_description = kpm_xapp.get_ran_function_description(json_ran_info=gnb_info)
        func_def_dict = ran_function_description.get_dict_of_values()
        logger.debug("[Main] gNB {} - Available functions: {}".format(gnb_name, func_def_dict))

        # Select format 4 or 1
        func_def_sub_dict = {}
        selected_format = format_action_def_e.END_ACTION_DEFINITION
        if len(func_def_dict[format_action_def_e.FORMAT_4_ACTION_DEFINITION]) == 0:
            selected_format = format_action_def_e.FORMAT_1_ACTION_DEFINITION
        else:
            selected_format = format_action_def_e.FORMAT_4_ACTION_DEFINITION

        if selected_format == format_action_def_e.END_ACTION_DEFINITION:
            logger.error("[Main] gNB {} - No supported action definition format".format(gnb_name))
            return False

        func_def_sub_dict[selected_format] = func_def_dict[selected_format]
        logger.debug("[Main] gNB {} - Selected functions: {}".format(gnb_name, func_def_dict[selected_format]))

        all_ok = True
        for sst in SST_SET:
            for sd in SD_RANGE:
                status = kpm_xapp.subscribe(
                    gnb=gnb,
                    ev_trigger=ev_trigger_tuple,
                    func_def=func_def_sub_dict,
                    ran_period_ms=1000,
                    sst=sst,
                    sd=sd,
                )
                if status != 201:
                    logger.error("[Main] gNB {} sst={} sd={} - subscription failed (status: {})".format(
                        gnb_name, sst, sd, status))
                    all_ok = False
                else:
                    logger.info("[Main] gNB {} sst={} sd={} - subscription OK".format(gnb_name, sst, sd))
        return all_ok

    def subscription_loop():
        """Periodically attempt to subscribe to gNBs that are not yet subscribed,
        and detect disconnected gNBs to re-subscribe when they come back."""
        while True:
            # Check already-subscribed gNBs for disconnection
            for gnb_name in list(subscribed_gnbs):
                gnb, _ = xapp_gen.get_selected_e2node_info(gnb_name)
                if gnb is None:
                    logger.warning("[Sub] gNB {} appears disconnected, cleaning up subscriptions".format(gnb_name))
                    for sub_id in list(kpm_xapp.subscription_id.get(gnb_name, [])):
                        try:
                            kpm_xapp.subscriber.Unsubscribe(sub_id)
                        except Exception:
                            pass
                    kpm_xapp.subscription_id.pop(gnb_name, None)
                    subscribed_gnbs.discard(gnb_name)

            # Try to subscribe to missing/reconnected gNBs
            missing = [g for g in gnb_list if g not in subscribed_gnbs]
            for gnb_name in missing:
                try:
                    logger.info("[Sub] Trying to subscribe to {}...".format(gnb_name))
                    if subscribe_to_gnb(gnb_name):
                        subscribed_gnbs.add(gnb_name)
                        logger.info("[Sub] Successfully subscribed to {}".format(gnb_name))
                    else:
                        logger.warning("[Sub] gNB {} not available yet, will retry".format(gnb_name))
                except Exception as e:
                    logger.error("[Sub] gNB {} - exception during subscribe: {}, will retry".format(gnb_name, e))
            time.sleep(10)

    sub_thread = threading.Thread(target=subscription_loop, daemon=True)
    sub_thread.start()

    # Start running
    logger.info("[Main] Starting xapp")
    xapp_gen.run()
