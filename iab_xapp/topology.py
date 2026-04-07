"""
Topology data management and graph building.
Reads shared state via `import state` (never `from state import X`).
"""

import time
import csv

import numpy as np

import state


# ── In-memory store helpers ───────────────────────────────────────────────────

def get_record(gnb_idx, ue_id, sst, sd):
    for r in state.topology_data[gnb_idx]:
        if r["ue_id"] == ue_id and r["sst"] == sst and r["sd"] == sd:
            return r
    return None


def upsert_record(gnb_idx, ue_id, sst, sd, record):
    for i, r in enumerate(state.topology_data[gnb_idx]):
        if r["ue_id"] == ue_id and r["sst"] == sst and r["sd"] == sd:
            state.topology_data[gnb_idx][i] = record
            return
    state.topology_data[gnb_idx].append(record)


def cleanup_stale(gnb_idx):
    now_ms = int(time.time() * 1000)
    state.topology_data[gnb_idx] = [
        r for r in state.topology_data[gnb_idx]
        if now_ms - r["timestamp"] <= state.STALE_TIMEOUT_MS
    ]


def cleanup_all_stale():
    for gnb_idx in range(len(state.topology_data)):
        cleanup_stale(gnb_idx)


def periodic_cleanup():
    """Background thread — periodically removes stale records."""
    while True:
        time.sleep(state.STALE_TIMEOUT_MS / 1000)
        cleanup_all_stale()


# ── CSV export ────────────────────────────────────────────────────────────────

def write_csv():
    if state.csv_file_global is None:
        return
    fieldnames = ["timestamp", "gnb_id", "sst", "sd", "ue_id", "value_serv"] + \
                 [f"value_neigh_{i}" for i in range(state.gnb_count_global)]
    with open(state.csv_file_global, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for gnb_idx, records in enumerate(state.topology_data):
            for r in records:
                writer.writerow({"gnb_id": state.gnb_list_global[gnb_idx], **r})


# ── KPM slice extraction ──────────────────────────────────────────────────────

def extract_slice_from_labels(ind_msg_format_1):
    """Extract (sst, sd) from the first measurement label that has a sliceID."""
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
                    sd  = label.sliceID.contents.sD.contents.value if label.sliceID.contents.sD else 0
                    return int(sst), int(sd)
        except Exception:
            continue
    return None, None


# ── Topology view ─────────────────────────────────────────────────────────────

def topology_from_memory():
    topology = {gnb: {} for gnb in state.gnb_list_global}
    for gnb_idx, records in enumerate(state.topology_data):
        gnb_id  = state.gnb_list_global[gnb_idx]
        nb_list = state.gnb_neighbors_global[gnb_idx]
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


# ── Graph building ────────────────────────────────────────────────────────────

def build_graph(gnb_list, gnb_neighbors, topology, iab_associations, connected_gnbs=None):
    nodes, edges = [], []
    seen_edges = set()
    if connected_gnbs is None:
        connected_gnbs = set(gnb_list)

    # Collect UEs absorbed into IAB nodes
    iab_ue_keys = set()
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
        is_iab    = gnb in iab_associations
        connected = gnb in connected_gnbs
        if is_iab:
            absorbed = iab_ues.get(gnb, [])
            label    = gnb + "\n" + "\n".join(absorbed) if absorbed else gnb
            nodes.append({"id": gnb, "type": "iab", "label": label,
                          "ue_count": len(absorbed), "connected": connected})
        else:
            nodes.append({"id": gnb, "type": "gnb", "label": gnb, "connected": connected})

    # Build UE nodes and edges
    for gnb_id, ues in topology.items():
        for ue_key, data in ues.items():
            if ue_key in iab_ue_keys:
                # UE merged into IAB node — redirect edges
                merged_into = None
                for iab_gnb, slices in iab_associations.items():
                    if (data["sst"], data["sd"]) in slices:
                        merged_into = iab_gnb
                        break
                if merged_into and merged_into != gnb_id:
                    edge_key = (merged_into, gnb_id, "serving")
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append({"from": merged_into, "to": gnb_id,
                                      "style": "solid", "rsrp": data["serving_rsrp"]})
                for nb_gnb, nb_rsrp in data["neighbor_rsrp"].items():
                    if nb_gnb == merged_into:
                        continue
                    edge_key = (merged_into, nb_gnb, "neigh")
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append({"from": merged_into, "to": nb_gnb,
                                      "style": "dashdot", "rsrp": nb_rsrp})
            else:
                ue_label = "{} (s{}/d{})".format(data["ue_id"], data["sst"], data["sd"])
                nodes.append({"id": ue_key, "type": "ue", "label": ue_label})
                edges.append({"from": ue_key, "to": gnb_id,
                              "style": "solid", "rsrp": data["serving_rsrp"]})
                for nb_gnb, nb_rsrp in data["neighbor_rsrp"].items():
                    key = (ue_key, nb_gnb)
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append({"from": ue_key, "to": nb_gnb,
                                      "style": "dashdot", "rsrp": nb_rsrp})

    return {"nodes": nodes, "edges": edges}
