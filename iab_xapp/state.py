"""
Shared mutable state for the IAB xApp.
All modules must access these via  `import state; state.X`
(not `from state import X`) so that reassignments in __main__ are visible.
"""

STALE_TIMEOUT_MS = 5000

n_iab_nodes = 1 #TODO: extract number from associations file
IAB_NODES = list(range(1, n_iab_nodes+1)) 

# topology_data[i] = list of measurement dicts for gnb_list[i]
# each dict: {timestamp, sst, sd, ue_id, value_serv, value_neigh_0, ...}
topology_data           = []
gnb_list_global         = []
gnb_neighbors_global    = []
gnb_count_global        = 0
csv_file_global         = None
kpm_xapp_global         = None
xapp_gen_global         = None
iab_associations_global = {}   # gnb_name -> list of (sst, sd)
subscribed_gnbs_global  = set() # gnb_names currently subscribed/connected
ue_ids                  = {}   # gnb_name -> {ue_key -> {"id": int, "type": int (ue_id_e2sm_e)}}
neigh_zero_counts       = {}   # gnb_name -> {ue_key -> {neigh_idx -> consecutive_zero_count}}

logger = None
