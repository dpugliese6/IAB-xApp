"""
Parsing utilities for neighborhood.conf and iab_associations.conf.
"""

import re


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
        return raw[2:].lower()
    return int(raw)


def get_field(key, block):
    m = re.search(rf'{key}\s*=\s*(0x[0-9a-fA-F]+|\d+)', block)
    return parse_value(m.group(1)) if m else None


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


def parse_entry(entry_block):
    ncfg_m = re.search(r'neighbour_cell_configuration\s*=\s*\((.+?)\)\s*;', entry_block, re.DOTALL)
    neighbors = []
    if ncfg_m:
        for nb_block in split_blocks(ncfg_m.group(1)):
            neighbors.append(parse_neighbor_cell(nb_block))

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

    return [parse_entry(block) for block in split_blocks(nl_m.group(1))]


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
        plmn   = parse_plmn(plmn_m.group(1))
        gnb_id = get_field("gNB_ID", block)
        sst    = get_field("sst", block)
        sd     = get_field("sd", block)
        if gnb_id is None or sst is None or sd is None:
            continue

        mcc_str    = str(plmn["mcc"]).zfill(3)
        mnc_str    = f"0{plmn['mnc']}" if plmn["mnc_length"] == 2 else str(plmn["mnc"])
        gnb_id_str = gnb_id if isinstance(gnb_id, str) else str(gnb_id)
        gnb_name   = f"gnb_{mcc_str}_{mnc_str}_{gnb_id_str.zfill(8)}"

        associations.setdefault(gnb_name, []).append((sst, sd))

    return associations


def get_gnb_list(neighbor_list):
    result = []
    for entry in neighbor_list:
        plmn       = entry["plmn"]
        mcc_str    = str(plmn["mcc"]).zfill(3)
        mnc_str    = f"0{plmn['mnc']}" if plmn["mnc_length"] == 2 else str(plmn["mnc"])
        gnb_id_str = entry["gNB_ID"].zfill(8)
        result.append(f"gnb_{mcc_str}_{mnc_str}_{gnb_id_str}")
    return result


def get_gnb_neighbors(neighbor_list, gnb_list):
    result = []
    for entry in neighbor_list:
        neighbors = []
        for nb in entry["neighbours"]:
            plmn       = nb["plmn"]
            mcc_str    = str(plmn["mcc"]).zfill(3)
            mnc_str    = f"0{plmn['mnc']}" if plmn["mnc_length"] == 2 else str(plmn["mnc"])
            gnb_id_str = nb["gNB_ID"].zfill(8)
            neighbors.append(f"gnb_{mcc_str}_{mnc_str}_{gnb_id_str}")
        result.append(neighbors)
    return result
