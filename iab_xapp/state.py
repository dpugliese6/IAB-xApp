"""
Shared mutable state for the IAB xApp.
All modules must access these via  `import state; state.X`
(not `from state import X`) so that reassignments in __main__ are visible.
"""

STALE_TIMEOUT_MS = 5000

# SST and SD ranges for subscriptions
SST_SET  = [1, 2]
SD_RANGE = list(range(0, 11))  # 0 to 10

# topology_data[i] = list of measurement dicts for gnb_list[i]
# each dict: {timestamp, sst, sd, ue_id, value_serv, value_neigh_0, ...}
topology_data           = []
gnb_list_global         = []
gnb_neighbors_global    = []
gnb_count_global        = 0
csv_file_global         = None
kpm_xapp_global         = None
iab_associations_global = {}   # gnb_name -> list of (sst, sd)
subscribed_gnbs_global  = set() # gnb_names currently subscribed/connected

logger = None
