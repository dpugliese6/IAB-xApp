import time
import signal
import argparse
import threading
import json

import numpy as np
import re

import setup_imports

import state
import conf_parser
import topology as topo
import web_server

from xDevSM.handlers.xDevSM_rmr_xapp import xDevSMRMRXapp
from xDevSM.decorators.kpm.kpm_frame import XappKpmFrame
from xDevSM.sm_framework.py_oran.kpm.enums import format_action_def_e
from xDevSM.sm_framework.py_oran.kpm.enums import format_ind_msg_e
from xDevSM.sm_framework.py_oran.kpm.enums import meas_type_enum
from xDevSM.sm_framework.py_oran.kpm.enums import meas_value_e


# ── Indication callback ───────────────────────────────────────────────────────

def indication_callback(ind_hdr, ind_msg, meid, sub_id=None):
    gnbid = meid.decode('utf-8')
    state.logger.info("[Main] Received indication message from {} (sub_id={})".format(gnbid, sub_id))

    sender_name = None
    if ind_hdr.data.kpm_ric_ind_hdr_format_1.sender_name:
        my_string = bytes(np.ctypeslib.as_array(
            ind_hdr.data.kpm_ric_ind_hdr_format_1.sender_name.contents.buf,
            shape=(ind_hdr.data.kpm_ric_ind_hdr_format_1.sender_name.contents.len,)))
        sender_name = my_string.decode('utf-8')

    if sender_name is None:
        state.logger.info("[Main] Sender name not specified in the indication message")

    try:
        gnb_idx = state.gnb_list_global.index(gnbid)
    except ValueError:
        state.logger.warning("[Main] gNB {} not in gnb_list, skipping".format(gnbid))
        return

    if ind_msg.type.value != format_ind_msg_e.FORMAT_3_INDICATION_MESSAGE:
        state.logger.info("[Main] format not supported for storing")
        return

    neigh_pattern = re.compile(r'L1M\.SS-RSRPNrNbr\.(\d+)\.\d+')
    serv_pattern  = re.compile(r'L1M\.SS-RSRP\.\d+')

    for i in range(ind_msg.data.frm_3.ue_meas_report_lst_len):
        meas_report_ue = ind_msg.data.frm_3.meas_report_per_ue[i]
        ue_id          = state.kpm_xapp_global.get_ue_id(meas_report_ue.ue_meas_report_lst)

        state.logger.info("[Main] gnb: {}, sender_name: {}, ue: {}".format(gnbid, sender_name, ue_id))
        ind_msg_format_1 = meas_report_ue.ind_msg_format_1

        # Extract sst/sd: labels first, then subscription context, then fallback
        sst, sd = topo.extract_slice_from_labels(ind_msg_format_1)
        if sst is not None:
            state.logger.info("[Main] Extracted slice from labels: sst={} sd={}".format(sst, sd))
        else:
            ctx = None
            if sub_id is not None:
                ctx = state.kpm_xapp_global.subscription_context.get(sub_id) or \
                      state.kpm_xapp_global.subscription_context.get(str(sub_id))
            if ctx:
                sst, sd = ctx["sst"], ctx["sd"]
                state.logger.info("[Main] Extracted slice from subscription context (sub_id={}): sst={} sd={}".format(sub_id, sst, sd))
            else:
                sst, sd = 1, 1
                state.logger.warning("[Main] Could not extract slice info (sub_id={} type={}, context_keys={}), using default sst={} sd={}".format(
                    sub_id, type(sub_id).__name__, list(state.kpm_xapp_global.subscription_context.keys()), sst, sd))

        meas_data_lst = ind_msg_format_1.meas_data_lst

        for j in range(ind_msg_format_1.meas_data_lst_len):
            serv_vals = []
            neigh_vals = {}

            for k in range(meas_data_lst[j].meas_record_len):
                meas_record_lst_el = meas_data_lst[j].meas_record_lst[k]
                if ind_msg_format_1.meas_info_lst[k].meas_type.type.value != meas_type_enum.NAME_MEAS_TYPE:
                    state.logger.info("[Main] Not supported meas type {}".format(
                        ind_msg_format_1.meas_info_lst[k].meas_type.type.value))
                    continue

                meas_type     = ind_msg_format_1.meas_info_lst[k].meas_type.value.name
                meas_type_str = bytes(np.ctypeslib.as_array(meas_type.buf, shape=(meas_type.len,))).decode('utf-8')
                meas_record   = meas_record_lst_el

                if meas_record.value.value == meas_value_e.INTEGER_MEAS_VALUE:
                    val = meas_record.union.int_val
                elif meas_record.value.value == meas_value_e.REAL_MEAS_VALUE:
                    val = meas_record.union.real_val
                else:
                    continue

                state.logger.info("{}:{}".format(meas_type_str, val))

                if serv_pattern.fullmatch(meas_type_str):
                    serv_vals.append(val)
                elif m := neigh_pattern.fullmatch(meas_type_str):
                    n = int(m.group(1))
                    neigh_vals.setdefault(n, []).append(val)

            serv_RSRP = np.mean([v for v in serv_vals if v != 0]) if any(v != 0 for v in serv_vals) else 0
            if serv_RSRP == 0:
                continue

            ue_key     = "{}:sst{}sd{}".format(str(ue_id), sst, sd)
            existing   = topo.get_record(gnb_idx, str(ue_id), sst, sd)
            zero_counts = state.neigh_zero_counts \
                              .setdefault(gnbid, {}) \
                              .setdefault(ue_key, {})

            neigh_RSRP = np.full(state.gnb_count_global, 0.0)
            for n in range(state.gnb_count_global):
                vals     = neigh_vals.get(n, [])
                non_zero = [v for v in vals if v != 0]
                if non_zero:
                    neigh_RSRP[n]  = np.mean(non_zero)
                    zero_counts[n] = 0
                else:
                    count          = zero_counts.get(n, 0) + 1
                    zero_counts[n] = count
                    if count < 5 and existing:
                        neigh_RSRP[n] = existing.get(f"value_neigh_{n}", 0.0)
                    # count >= 5: stays 0 (neighbor considered gone)

            record = {
                "timestamp":  int(time.time() * 1000),
                "sst":        sst,
                "sd":         sd,
                "ue_id":      str(ue_id),
                "value_serv": float(serv_RSRP),
                **{f"value_neigh_{i}": float(neigh_RSRP[i]) for i in range(state.gnb_count_global)},
            }
            topo.upsert_record(gnb_idx, str(ue_id), sst, sd, record)

        topo.cleanup_stale(gnb_idx)

    topo.write_csv()


# ── xApp callbacks ────────────────────────────────────────────────────────────

def shutdown():
    state.logger.info("[Main] Shutting down")
    topo.write_csv()


def sub_failed_callback(json_data):
    state.logger.info("[Main] subscription failed: {}".format(json_data))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="IAB xApp")
    parser.add_argument("-r", "--route_file", metavar="<route_file>",
                        help="path of xApp route file",
                        type=str, default="./config/uta_rtg.rt")
    args = parser.parse_args()

    neighbor_list  = conf_parser.parse_neighbor_list("neighborhood.conf")
    gnb_list       = conf_parser.get_gnb_list(neighbor_list)
    gnb_neighbors  = conf_parser.get_gnb_neighbors(neighbor_list, gnb_list)
    iab_associations = conf_parser.parse_iab_associations("iab_associations.conf")

    print(json.dumps(gnb_list, indent=2))
    print(json.dumps(gnb_neighbors, indent=2))
    print("IAB associations:", json.dumps({k: v for k, v in iab_associations.items()}, indent=2))

    xapp_gen = xDevSMRMRXapp("0.0.0.0", route_file=args.route_file)

    kpm_xapp = XappKpmFrame(xapp_gen,
                            xapp_gen.logger,
                            xapp_gen.server,
                            xapp_gen.get_xapp_name(),
                            xapp_gen.rmr_port,
                            xapp_gen.http_port,
                            xapp_gen.get_pltnamespace(),
                            xapp_gen.get_app_namespace())

    # Initialise shared state
    state.logger                 = xapp_gen.logger
    state.gnb_list_global        = gnb_list
    state.gnb_neighbors_global   = gnb_neighbors
    state.gnb_count_global       = len(gnb_list) - 1
    state.csv_file_global        = "network_topology.csv"
    state.kpm_xapp_global        = kpm_xapp
    state.iab_associations_global = iab_associations
    state.topology_data          = [[] for _ in gnb_list]

    # Flask web server
    flask_app = web_server.create_app()

    def run_flask():
        try:
            flask_app.run(host="0.0.0.0", port=5555, debug=False, threaded=True)
        except Exception as e:
            state.logger.error("[Flask] Web server crashed: {}".format(e))

    threading.Thread(target=run_flask, daemon=True).start()
    state.logger.info("[Main] Flask web server started on port 5555")

    # Periodic stale-data cleanup
    threading.Thread(target=topo.periodic_cleanup, daemon=True).start()

    # Register xApp handlers and signals
    xapp_gen.register_shutdown(shutdown)
    xapp_gen.register_handler(kpm_xapp.handle)
    kpm_xapp.register_ind_msg_callback(handler=indication_callback)
    kpm_xapp.register_sub_fail_callback(handler=sub_failed_callback)
    signal.signal(signal.SIGINT,  kpm_xapp.terminate)
    signal.signal(signal.SIGTERM, kpm_xapp.terminate)

    time.sleep(10)

    # Subscription management
    subscribed_gnbs = state.subscribed_gnbs_global
    ev_trigger_tuple = (0, 1000)

    def subscribe_to_gnb(gnb_name):
        gnb, gnb_info = xapp_gen.get_selected_e2node_info(gnb_name)
        if not gnb:
            return False

        ran_function_description = kpm_xapp.get_ran_function_description(json_ran_info=gnb_info)
        func_def_dict = ran_function_description.get_dict_of_values()
        state.logger.debug("[Main] gNB {} - Available functions: {}".format(gnb_name, func_def_dict))

        if len(func_def_dict[format_action_def_e.FORMAT_4_ACTION_DEFINITION]) == 0:
            selected_format = format_action_def_e.FORMAT_1_ACTION_DEFINITION
        else:
            selected_format = format_action_def_e.FORMAT_4_ACTION_DEFINITION

        if selected_format == format_action_def_e.END_ACTION_DEFINITION:
            state.logger.error("[Main] gNB {} - No supported action definition format".format(gnb_name))
            return False

        func_def_sub_dict = {selected_format: func_def_dict[selected_format]}
        state.logger.debug("[Main] gNB {} - Selected functions: {}".format(gnb_name, func_def_dict[selected_format]))

        all_ok = True
        # Subscription for normal users
        status = kpm_xapp.subscribe(
            gnb=gnb,
            ev_trigger=ev_trigger_tuple,
            func_def=func_def_sub_dict,
            ran_period_ms=1000,
            sst=1,
            sd=0,
        )
        if status != 201:
            state.logger.error("[Main] gNB {} sst={} sd={} - subscription failed (status: {})".format(
                gnb_name, 1, 0, status))
            all_ok = False
        else:
            state.logger.info("[Main] gNB {} sst={} sd={} - subscription OK".format(gnb_name, 1, 0))


        for sd in state.IAB_NODES:
            status = kpm_xapp.subscribe(
                gnb=gnb,
                ev_trigger=ev_trigger_tuple,
                func_def=func_def_sub_dict,
                ran_period_ms=1000,
                sst=2,
                sd=sd,
            )
            if status != 201:
                state.logger.error("[Main] gNB {} sst={} sd={} - subscription failed (status: {})".format(
                    gnb_name, 2, sd, status))
                all_ok = False
            else:
                state.logger.info("[Main] gNB {} sst={} sd={} - subscription OK".format(gnb_name, 2, sd))
        return all_ok

    def subscription_loop():
        """Periodically subscribe to missing gNBs and detect disconnections."""
        while True:
            # Detect disconnected gNBs
            for gnb_name in list(subscribed_gnbs):
                gnb, _ = xapp_gen.get_selected_e2node_info(gnb_name)
                if gnb is None:
                    state.logger.warning("[Sub] gNB {} appears disconnected, cleaning up subscriptions".format(gnb_name))
                    for sub_id in list(kpm_xapp.subscription_id.get(gnb_name, [])):
                        try:
                            kpm_xapp.subscriber.Unsubscribe(sub_id)
                        except Exception:
                            pass
                    kpm_xapp.subscription_id.pop(gnb_name, None)
                    subscribed_gnbs.discard(gnb_name)

            # Subscribe to missing / reconnected gNBs
            for gnb_name in [g for g in gnb_list if g not in subscribed_gnbs]:
                try:
                    state.logger.info("[Sub] Trying to subscribe to {}...".format(gnb_name))
                    if subscribe_to_gnb(gnb_name):
                        subscribed_gnbs.add(gnb_name)
                        state.logger.info("[Sub] Successfully subscribed to {}".format(gnb_name))
                    else:
                        state.logger.warning("[Sub] gNB {} not available yet, will retry".format(gnb_name))
                except Exception as e:
                    state.logger.error("[Sub] gNB {} - exception during subscribe: {}, will retry".format(gnb_name, e))
            time.sleep(10)

    threading.Thread(target=subscription_loop, daemon=True).start()

    state.logger.info("[Main] Starting xapp")
    xapp_gen.run()
