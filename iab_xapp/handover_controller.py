"""
Handover controller: computes optimal topology and sends RC HO commands.

Optimization logic:
  For each UE, find the gNB with the highest RSRP across serving + neighbors.
  Trigger a HO only if the best candidate is at least HO_THRESHOLD_DB better
  than the current serving cell.
"""

import state
from xDevSM.decorators.rc.rc_connected_mode_mobility import ConnectedModeMobilityControl
from xDevSM.sm_framework.py_oran.kpm.enums import ue_id_e2sm_e

HO_THRESHOLD_DB = 5.0
PLMN_IDENTITY   = "00F122"


def gnb_name_to_nr_cell_id(gnb_name):
    """
    Convert a gNB inventory name to a 32-bit binary string for the RC control.
    e.g. 'gnb_001_022_00000e02'  ->  '00000000000000000000111000000010'
    """
    hex_id = gnb_name.split("_")[-1]   # '00000e02'
    return format(int(hex_id, 16), '032b')


def compute_ho_decisions(topology):
    """
    Returns a list of HO decisions, each a dict:
      ue_key, ue_id, current_gnb, target_gnb, current_rsrp, target_rsrp

    For IAB backhaul UEs (whose sst/sd match an IAB association), the IAB
    node's own gNB identity is excluded from HO candidates to prevent
    self-attachment loops.
    """
    # Reverse map: (sst, sd) -> IAB gNB name that owns this backhaul slice
    iab_slice_owner = {
        s: gnb_name
        for gnb_name, slices in state.iab_associations_global.items()
        for s in slices
    }

    decisions = []
    for gnb_id, ues in topology.items():
        for ue_key, data in ues.items():
            own_iab_gnb = iab_slice_owner.get((data["sst"], data["sd"]))

            best_gnb  = gnb_id
            best_rsrp = data["serving_rsrp"]
            for nb_gnb, nb_rsrp in data["neighbor_rsrp"].items():
                if nb_gnb == own_iab_gnb:
                    continue   # never hand IAB UE over to its own gNB side
                if nb_rsrp > best_rsrp:
                    best_gnb  = nb_gnb
                    best_rsrp = nb_rsrp
            if best_gnb != gnb_id and best_rsrp >= data["serving_rsrp"] + HO_THRESHOLD_DB:
                decisions.append({
                    "ue_key":       ue_key,
                    "ue_id":        data["ue_id"],
                    "current_gnb":  gnb_id,
                    "target_gnb":   best_gnb,
                    "current_rsrp": data["serving_rsrp"],
                    "target_rsrp":  best_rsrp,
                })
    return decisions


def execute_handovers(decisions):
    """
    For each decision, fetch the RC function description for the source gNB
    and send an RC control request via ConnectedModeMobilityControl.
    Returns the number of HO commands successfully sent.
    """
    xapp_gen = state.xapp_gen_global
    sent = 0

    if not decisions:
        state.logger.info("[HO] No handovers needed")
        return sent

    rc_func = ConnectedModeMobilityControl(
        xapp_gen,
        logger=xapp_gen.logger,
        server=xapp_gen.server,
        xapp_name=xapp_gen.get_xapp_name(),
        rmr_port=xapp_gen.rmr_port,
        mrc=xapp_gen._mrc,
        http_port=xapp_gen.http_port,
        pltnamespace=xapp_gen.get_pltnamespace(),
        app_namespace=xapp_gen.get_app_namespace(),
        plmn_identity=PLMN_IDENTITY,
        nr_cell_id="0" * 32,   # overridden per decision below
    )

    for d in decisions:
        ue_info = state.ue_ids.get(d["current_gnb"], {}).get(d["ue_key"])
        if ue_info is None:
            state.logger.warning("[HO] UE info not found for ue {} on {}, skipping".format(
                d["ue_id"], d["current_gnb"]))
            continue

        # Recreate a fresh UE ID struct from scalar values (avoids stale C memory)
        ue_id_val  = ue_info["id"]
        ue_id_type = ue_info["type"]
        if ue_id_type == ue_id_e2sm_e.GNB_DU_UE_ID_E2SM:
            ue_struct = rc_func.get_mock_du_ue_id(ran_ue_id=ue_id_val)
        else:
            ue_struct = rc_func.get_mock_ue_id(ran_ue_id=ue_id_val)

        _, gnb_info = xapp_gen.get_selected_e2node_info(d["current_gnb"])
        if gnb_info is None:
            state.logger.warning("[HO] gNB {} not reachable, skipping HO for ue {}".format(
                d["current_gnb"], d["ue_id"]))
            continue

        rc_func_desc = rc_func.get_ran_function_description(json_ran_info=gnb_info)
        nr_cell_id   = gnb_name_to_nr_cell_id(d["target_gnb"])

        state.logger.info("[HO] UE {} | {} ({:.1f} dBm) -> {} ({:.1f} dBm) | nr_cell_id={}".format(
            d["ue_id"], d["current_gnb"], d["current_rsrp"],
            d["target_gnb"],   d["target_rsrp"],  nr_cell_id))

        rc_func.set_nr_cell_id(nr_cell_id)
        rc_func.send(
            e2_node_id=d["current_gnb"],
            ran_func_dsc=rc_func_desc,
            ue_id_struct=ue_struct,
            control_action_id=1,
        )
        sent += 1

    return sent
