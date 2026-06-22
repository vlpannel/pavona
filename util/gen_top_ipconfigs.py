#!/usr/bin/env python3
# Copyright lowRISC contributors (OpenTitan project).
# Copyright zeroRISC Inc.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
"""Generate ipgen IP configuration files from a complete top configuration.
"""
import argparse
import logging as log
from pathlib import Path
from copy import deepcopy
from typing import Any, Dict, List
import hjson
from basegen.typing import ConfigT, ParamsT
from ipgen.lib import IpConfig, IpTemplate
from topgen.lib import (load_cfg, is_ipgen, get_ipgen_params, num_rom_ctrl,
                        find_module, find_modules, find_module_by_name)
from topgen.merge import extract_clocks, amend_resets
from ipgen.clkmgr_gen import get_clkmgr_params

from topgen.clocks import Clocks
from topgen.resets import Resets
from design.lib.OtpMemMap import OtpMemMap
from topgen.secure_prng import SecurePrngFactory
from collections import OrderedDict, defaultdict


REPO_TOP = Path(__file__).parents[1].resolve()
IP_TEMPLATES_PATH = REPO_TOP / "hw" / "ip_templates"

HEADER = """// Copyright lowRISC contributors (OpenTitan project).
// Licensed under the Apache License, Version 2.0, see LICENSE for details.
// SPDX-License-Identifier: Apache-2.0
//
// ------------------- W A R N I N G: A U T O - G E N E R A T E D   C O D E !! -------------------//
// PLEASE DO NOT HAND-EDIT THIS FILE. IT HAS BEEN AUTO-GENERATED WITH THE FOLLOWING COMMAND:
// util/top_ipconfigs.py {} -o {}

"""


def _get_alert_handler_params(top: ConfigT, name: str) -> ParamsT:
    """Returns parameters for alert_hander ipgen from top config."""
    # default values
    esc_cnt_dw = 32
    accu_cnt_dw = 16
    # leave this constant
    n_classes = 4

    # Count number of alerts and LPGs, accepting lack of alert_lpgs in top.
    # They are added after the merge pass, so the ip_block won't have them.
    n_alerts = sum(a.get("width", 1) for a in top["alert"] if a.get("handler") == name)
    n_lpgs_int = len(top.get("alert_lpgs", []))
    n_lpgs = n_lpgs_int
    n_lpgs_incoming_offset = n_lpgs
    # Add incoming alerts and their LPGs
    for alerts in top['incoming_alert'].values():
        n_alerts += len(alerts)
        # Number of LPGs is maximum index + 1
        n_lpgs += max(alert['lpg_idx'] for alert in alerts) + 1

    if n_alerts < 1:
        # set number of alerts to 1 such that the config is still valid
        # that input will be tied off
        n_alerts = 1
        log.warning(f"{name} has no alerts attached to it; is it needed?")

    async_on = []
    async_on_format = "1'b{:01b}"
    lpg_map = []
    if n_lpgs:
        # format to print lpg indices in decimal format
        n_lpg_width = n_lpgs.bit_length()
        lpg_idx_format = f"{n_lpg_width}'d{{:d}}"

    for alert in top["alert"]:
        if alert["handler"] != name:
            continue

        for _ in range(alert["width"]):
            async_on.append(async_on_format.format(int(alert["async"])))
            if n_lpgs_int:
                lpg_map.append(lpg_idx_format.format(int(alert["lpg_idx"])))

    if "incoming_alert" in top:
        lpg_prev_offset = n_lpgs_incoming_offset
        for alerts in top['incoming_alert'].values():
            for alert in alerts:
                for _ in range(alert["width"]):
                    async_on.append(async_on_format.format(int(
                        alert["async"])))
                    lpg_map.append(
                        lpg_idx_format.format(lpg_prev_offset +
                                              int(alert["lpg_idx"])))
            lpg_prev_offset += max(alert['lpg_idx'] for alert in alerts) + 1
    module = find_module(top["module"], name, use_base_template_type=False)

    n_esc_sev = module["param_decl"]["EscNumSeverities"]
    ping_cnt_dw = module["param_decl"]["EscPingCountWidth"]
    ipgen_params = get_ipgen_params(module)
    ipgen_params.update({
        "module_instance_name": module["type"],
        "n_alerts": n_alerts,
        "esc_cnt_dw": esc_cnt_dw,
        "accu_cnt_dw": accu_cnt_dw,
        "async_on": async_on,
        "n_classes": n_classes,
        "n_esc_sev": n_esc_sev,
        "ping_cnt_dw": ping_cnt_dw,
        "n_lpg": n_lpgs,
        "lpg_map": lpg_map,
    })
    return ipgen_params


def _get_pinmux_params(top: ConfigT) -> ParamsT:
    """Gets parameters for pinmux ipgen from top config."""
    # Generation without pinmux and pinout configuration is not supported.
    assert "pinmux" in top
    assert "pinout" in top

    pinmux = top["pinmux"]

    # Get number of wakeup detectors
    if "num_wkup_detect" in pinmux:
        num_wkup_detect = pinmux["num_wkup_detect"]
    else:
        num_wkup_detect = 1

    if num_wkup_detect <= 0:
        # TODO: add support for no wakeup counter case
        log.error("Topgen does currently not support generation of a top " +
                  "without DIOs.")
        return

    if "wkup_cnt_width" in pinmux:
        wkup_cnt_width = pinmux["wkup_cnt_width"]
    else:
        wkup_cnt_width = 8

    if wkup_cnt_width <= 1:
        log.error("Wakeup counter width must be greater equal 2.")
        return

    # MIO Pads
    n_mio_pads = pinmux["io_counts"]["muxed"]["pads"]

    # Total inputs/outputs
    # Reuse the counts from the merge phase
    n_mio_periph_in = (pinmux["io_counts"]["muxed"]["inouts"] +
                       pinmux["io_counts"]["muxed"]["inputs"])
    n_mio_periph_out = (pinmux["io_counts"]["muxed"]["inouts"] +
                        pinmux["io_counts"]["muxed"]["outputs"])
    n_dio_periph_in = (pinmux["io_counts"]["dedicated"]["inouts"] +
                       pinmux["io_counts"]["dedicated"]["inputs"])
    n_dio_periph_out = (pinmux["io_counts"]["dedicated"]["inouts"] +
                        pinmux["io_counts"]["dedicated"]["outputs"])
    n_dio_pads = (pinmux["io_counts"]["dedicated"]["inouts"] +
                  pinmux["io_counts"]["dedicated"]["inputs"] +
                  pinmux["io_counts"]["dedicated"]["outputs"])

    # Generation with zero MIO/DIO pads is currently not supported.
    assert (n_mio_pads > 0)
    assert (n_dio_pads > 0)

    log.info("Generating pinmux with following info from hjson:")
    log.info("num_wkup_detect: %d" % num_wkup_detect)
    log.info("wkup_cnt_width:  %d" % wkup_cnt_width)
    log.info("n_mio_periph_in:  %d" % n_mio_periph_in)
    log.info("n_mio_periph_out: %d" % n_mio_periph_out)
    log.info("n_dio_periph_in:  %d" % n_dio_periph_in)
    log.info("n_dio_periph_out: %d" % n_dio_periph_out)
    log.info("n_dio_pads:       %d" % n_dio_pads)

    # Get the pinmux instance
    pinmux_module = find_module(top["module"], "pinmux")
    ipgen_params = get_ipgen_params(pinmux_module)
    ipgen_params.update({
        "n_wkup_detect": num_wkup_detect,
        "wkup_cnt_width": wkup_cnt_width,
        "n_mio_pads": n_mio_pads,
        "n_mio_periph_in": n_mio_periph_in,
        "n_mio_periph_out": n_mio_periph_out,
        "n_dio_pads": n_dio_pads,
        "n_dio_periph_in": n_dio_periph_in,
        "n_dio_periph_out": n_dio_periph_out,
        "enable_usb_wakeup": pinmux['enable_usb_wakeup'],
        "enable_strap_sampling": pinmux['enable_strap_sampling'],
    })
    return ipgen_params


def _get_pwrmgr_params(top: ConfigT) -> ParamsT:
    """Extracts parameters for pwrmgr ipgen."""
    # Count number of wakeups
    n_wkups = len(top["wakeups"])
    log.info("Found {} wakeup signals".format(n_wkups))

    if n_wkups < 1:
        n_wkups = 1
        log.warning(
            "The design has no wakeup sources. Low power not supported.")

    # Count number of reset requests
    n_rstreqs = len(top["reset_requests"]["peripheral"])
    log.info("Found {} reset request signals".format(n_rstreqs))

    if n_rstreqs < 1:
        n_rstreqs = 1
        log.warning("The design has no reset request sources. "
                    "Reset requests are not supported.")

    n_rom_ctrl = num_rom_ctrl(top['module'])
    assert n_rom_ctrl > 0

    # Add another artificial ROM_CTRL input to allow IBEX halting that input
    if top['power'].get('halt_ibex_via_rom_ctrl', False):
        n_rom_ctrl += 1

    clocks = top["clocks"]
    assert isinstance(clocks, Clocks)
    src_clks = [obj.name for obj in clocks.srcs.values() if not obj.aon]

    pwrmgr = find_module(top["module"], "pwrmgr")
    ipgen_params = get_ipgen_params(pwrmgr)
    ipgen_params.update({
        "NumWkups": n_wkups,
        "Wkups": top["wakeups"],
        "NumRstReqs": n_rstreqs,
        "rst_reqs": top["reset_requests"],
        "wait_for_external_reset": top['power']['wait_for_external_reset'],
        "NumRomInputs": n_rom_ctrl,
        "has_aon_clk": any(obj.aon for obj in clocks.srcs.values()),
        "src_clks": src_clks
    })
    return ipgen_params


def _get_rstmgr_params(top: ConfigT) -> ParamsT:
    """Extracts parameters for rstmgr ipgen."""
    # Parameters needed for generation
    reset_obj = top["resets"]
    assert isinstance(reset_obj, Resets)

    # unique clocks
    clks = reset_obj.get_clocks()
    clocks = top["clocks"]
    assert isinstance(clocks, Clocks)
    src_freqs = {sv.name: sv.freq for sv in clocks.srcs.values()}
    src_freqs.update({dv.name: dv.freq for dv in clocks.derived_srcs.values()})
    # Create a dictionary indexed by clks containing their frequency.
    clk_freqs = {clk: src_freqs[clk] for clk in clks}

    # resets sent to reset struct
    output_rsts = reset_obj.get_top_resets()

    # sw controlled resets: dict indexed by device containing the clock
    sw_rsts = OrderedDict([(r.name, r.clock.name)
                           for r in reset_obj.get_sw_resets()])
    # rst_ni
    rstmgr = find_module(top["module"], "rstmgr")
    rst_ni = rstmgr["reset_connections"]

    # leaf resets
    leaf_rsts = reset_obj.get_generated_resets()

    # Number of reset requests
    n_rstreqs = len(top["reset_requests"]["peripheral"])

    # Will connect to alert_handler
    with_alert_handler = find_module(top['module'],
                                     'alert_handler') is not None

    ipgen_params = get_ipgen_params(rstmgr)
    ipgen_params.update({
        "clk_freqs": clk_freqs,
        "reqs": top["reset_requests"],
        "power_domains": top["power"]["domains"],
        "num_rstreqs": n_rstreqs,
        "sw_rsts": sw_rsts,
        "output_rsts": output_rsts,
        "leaf_rsts": leaf_rsts,
        "rst_ni": rst_ni['rst_ni']['name'],
        "export_rsts": top["exported_rsts"],
        "with_alert_dump": with_alert_handler,
    })
    return ipgen_params


def _get_flash_ctrl_params(top: ConfigT) -> ParamsT:
    """Extracts parameters for flash_ctrl ipgen."""

    # Parameters needed for generation
    flash_mems = find_modules(top["module"], "flash_ctrl", True)
    if len(flash_mems) > 1:
        log.error("This design does not currently support multiple flashes")
        return
    elif len(flash_mems) == 0:
        raise ValueError(
            "In _get_flash_ctrl_params for design with no flash_ctrl")

    ipgen_params = get_ipgen_params(flash_mems[0])
    ipgen_params.update(vars(flash_mems[0]["memory"]["mem"]["config"]))
    ipgen_params.pop('base_addrs', None)
    return ipgen_params


def _get_otp_ctrl_params(top: ConfigT,
                         out_path: Path) -> ParamsT:

    def has_flash_keys(parts, path) -> bool:
        """Determines if the SECRET1 partition has flash key seeds.

        This assumes the flash keys are in the secret1 partition, and are
        named "flash*key_seed" (case doesn't matter). If in some future
        otp mmap the location of these keys changes we can revisit this
        detection.
        """
        for p in parts:
            if p["name"].lower() == "secret1":
                secret1_partition = p
                break
        else:
            raise ValueError(f"SECRET1 partition not found in {path}")
        flash_keys = [i for i in secret1_partition["items"]
                      if i["name"].lower().startswith("flash")
                      and i["name"].lower().endswith("key_seed")]
        return len(flash_keys) > 0

    def get_param(name: str, param_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        for p in param_list:
            if p["name"] == name:
                return p
        return None

    """Returns the parameters extracted from the otp_mmap.hjson file."""
    otp_mmap_path = out_path / "data" / "otp" / "otp_ctrl_mmap.hjson"
    otp_mmap = OtpMemMap.from_mmap_path(otp_mmap_path, generate_fresh_keys=True).config
    enable_flash_keys = has_flash_keys(otp_mmap["partitions"], otp_mmap_path)
    otp_ctrl = find_module(top["module"], "otp_ctrl")

    # Add the full and non-sanitized OTP map for a later dump to the secrets file.
    otp_ctrl["otp_mmap"] = deepcopy(otp_mmap)

    # Now inject the Key/digest/IV/PartInv into the "extdata" randtype params
    # The param list might not be available on the first pass. Only propagate
    # the values if the param list is available and non-empty.
    if "param_list" in otp_ctrl:
        for i, key in enumerate(otp_mmap["scrambling"]["keys"]):
            p = get_param(f"RndCnstScrmblKey{i}", otp_ctrl["param_list"])
            if p is not None:
                p["default"] = hex(int(key["value"]))
            key["value"] = "<random>"

        for i, digest in enumerate(otp_mmap["scrambling"]["digests"]):
            p = get_param(f"RndCnstDigestConst{i}", otp_ctrl["param_list"])
            if p is not None:
                p["default"] = hex(int(digest["cnst_value"]))
            digest["cnst_value"] = "<random>"

        for i, digest in enumerate(otp_mmap["scrambling"]["digests"]):
            p = get_param(f"RndCnstDigestIV{i}", otp_ctrl["param_list"])
            if p is not None:
                p["default"] = hex(int(digest["iv_value"]))
            digest["iv_value"] = "<random>"

        # Parse the part_inv_default data from the OTP map into a simple dict that we can use
        # when rendering the random constant package.
        part_inv_data = []
        rev_partitions = otp_mmap["partitions"][::-1]
        offset = int(rev_partitions[0]["offset"]) + int(rev_partitions[0]["size"])
        for part in rev_partitions:
            part_data = {
                "size": part["size"] * 8,
                "items": []
            }
            for item in part["items"][::-1]:
                if offset != item["offset"] + item["size"]:
                    part_data["items"].append({
                        "comment": "unallocated space",
                        "size": (offset - item["size"] - item["offset"]) * 8,
                        "inv_default": 0
                    })
                    offset = item["offset"] + item["size"]
                part_data["items"].append({
                    "size": item["size"] * 8,
                    "inv_default": item["inv_default"]
                })
                # Sanitize the seed dependent inv_default values.
                if item["inv_default"] != 0:
                    item["inv_default"] = "<random>"
                offset -= item["size"]
            part_inv_data.append(part_data)

        p = get_param("RndCnstPartInvDefault", otp_ctrl["param_list"])
        if p is not None:
            p["default"] = part_inv_data

    ipgen_params = get_ipgen_params(otp_ctrl)
    ipgen_params.update({
        "otp_mmap": otp_mmap,
        "enable_flash_key": enable_flash_keys,
    })
    return ipgen_params


def _get_ac_range_check_params(top: ConfigT) -> ParamsT:
    """Extracts parameters for ac_range_check ipgen."""
    # Determine RACL params from the top-level config, otherwise use ipgen's
    # default values
    racl_params = {}
    if "racl_config" in top:
        racl_params = {
            "nr_role_bits": top["racl"]["nr_role_bits"],
            "nr_ctn_uid_bits": top["racl"]["nr_ctn_uid_bits"]
        }

    # Get the AC Range Check instance
    module = find_module(top['module'], 'ac_range_check')
    ipgen_params = get_ipgen_params(module)
    ipgen_params.update(racl_params)
    ipgen_params.update({
        "module_instance_name": module["type"]
    })
    return ipgen_params


def _get_racl_params(top: ConfigT) -> ParamsT:
    """Extracts parameters for racl_ctrl ipgen."""
    module = find_module(top["module"], "racl_ctrl")
    racl_group = module.get("ipgen_params", {}).get("racl_group", "Null")
    if len(top["racl"]["policies"]) == 1:
        # If there is only one set of policies, take the first one
        policies = list(top["racl"]["policies"].values())[0]
    else:
        # More than one policy, we need to find the matching set of policies
        policies = top["racl"]["policies"][racl_group]

    num_subscribing_ips = defaultdict(int)
    for m in top["module"]:
        racl_mappings = m.get("racl_mappings", {})

        for group in set(
            mapping['racl_group'] for mapping in racl_mappings.values()
        ):
            num_subscribing_ips[group] += 1

    ipgen_params = get_ipgen_params(module)
    ipgen_params.update({
        "module_instance_name": module["type"],
        "nr_role_bits": top["racl"]["nr_role_bits"],
        "nr_ctn_uid_bits": top["racl"]["nr_ctn_uid_bits"],
        "nr_policies": top["racl"]["nr_policies"],
        'nr_subscribing_ips': num_subscribing_ips[racl_group],
        "racl_group": racl_group,
        "policies": policies
    })
    return ipgen_params


def _get_rv_plic_params(top: ConfigT, name: str) -> ParamsT:
    """Gets parameters for plic ipgen from top config."""
    # Get this PLIC instance
    module = find_module_by_name(top["module"], name)

    # Count number of interrupts
    # Interrupt source 0 is tied to 0 to conform RISC-V PLIC spec.
    # So, total number of interrupts are the number of entries in the list + 1
    num_srcs = 1
    for intr in top["interrupt"]:
        if intr.get("plic") == name:
            num_srcs += int(intr["width"]) if "width" in intr else 1

    num_targets = len(module.get("targets", []))

    if num_srcs <= 1:
        log.warning(f"no interrupts are connected to {name}, is it needed?")

    if num_srcs > 1024:
        log.error(f"RISC-V PLIC Error: Configured interrupt sources ({num_srcs}) "
                  "exceed the maximum of 1024.")
        return

    if num_targets < 1:
        log.warning(f"{name} specifies no targets, is it needed?")

    ipgen_params = get_ipgen_params(module)
    ipgen_params.update({
        "module_instance_name": name,
        "src": num_srcs,
        "target": num_targets,
        "prio": 3,
    })
    return ipgen_params


def _get_basic_ipgen_params(topcfg: Dict[str, object], template_type: str) -> Dict[str, object]:
    """Extracts parameters for given ipgen IP."""
    module = find_module(topcfg["module"], template_type)

    ipgen_params = get_ipgen_params(module)
    ipgen_params.update({
        "module_instance_name": module["type"]
    })
    return ipgen_params


def get_params(topcfg: Dict[str, object], module: Dict, cfg_path: Path) -> ParamsT:
    match module["template_type"]:
        case "alert_handler":
            return _get_alert_handler_params(topcfg, module["name"])
        case "pinmux":
            return _get_pinmux_params(topcfg)
        case "pwrmgr":
            return _get_pwrmgr_params(topcfg)
        case "rstmgr":
            return _get_rstmgr_params(topcfg)
        case "flash_ctrl":
            return _get_flash_ctrl_params(topcfg)
        case "otp_ctrl":
            # complete top config should be within hw/top_*/data/autogen
            return _get_otp_ctrl_params(topcfg, cfg_path)
        case "ac_range_check":
            return _get_ac_range_check_params(topcfg)
        case "racl_ctrl":
            return _get_racl_params(topcfg)
        case "rv_plic":
            return _get_rv_plic_params(topcfg, module["name"])
        case "clkmgr":
            return get_clkmgr_params(topcfg)
        case _:
            return _get_basic_ipgen_params(topcfg, module["template_type"])


def ipgen_is_selected(ip: dict, valid_ips: list[str] | None = None):
    if valid_ips is None:
        return True
    return (ip.get("template_type") in valid_ips
            or ip.get("name") in valid_ips
            or "xbar_" + ip.get("name") in valid_ips)  # for xbars


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--completecfg", "-t",
                        type=Path,
                        required=True,
                        help="Complete top configuration.")
    parser.add_argument("--seedcfg", "-s",
                        type=Path,
                        default=None,
                        help="Top seed configuration. Required for some ipgen IPs.")
    parser.add_argument("--outdir", "-o",
                        required=True,
                        type=Path,
                        help="Output directory.")
    parser.add_argument("--ips", "-i",
                        type=str,
                        nargs="*",
                        default=None,
                        help="Only get ipconfig file for specific ipgen template"
                        " types. If not specified, will generate ipconfigs for all"
                        " ipgen IPs within the top.")
    parser.add_argument("--xbars", "-x",
                        action=argparse.BooleanOptionalAction,
                        help="Create configuration Hjsons for the top level's xbars.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    log.basicConfig(format="%(filename)s:%(lineno)d: %(levelname)s: %(message)s",
                    level=log.DEBUG if args.verbose else log.ERROR)

    args.outdir.mkdir(parents=True, exist_ok=True)
    completecfg = load_cfg(args.completecfg)
    seedcfg = load_cfg(args.seedcfg) if args.seedcfg is not None else {}

    # can use either seed cfg or generated secrets cfg
    seed_value = (seedcfg.get("topgen_seed")
                  or seedcfg.get("seed", {}).get("topgen_seed", {}).get("value"))
    if seed_value is None:
        log.warning("no top level seed value found (may be required by some IPs)")

    extract_clocks(completecfg)
    amend_resets(completecfg, {}, allow_missing_blocks=True)  # name_to_block not needed

    ipgen_modules = [module for module in completecfg["module"]
                     if is_ipgen(module) and ipgen_is_selected(module, args.ips)]

    for module in ipgen_modules:
        log.info(f"generating ipconfig for {module['name']}")
        if seed_value is not None:
            SecurePrngFactory.create("topgen", seed_value)  # reset random seed every time

        params = get_params(completecfg, module, args.completecfg.parents[2])
        params |= {"topname": completecfg["name"], "uniquified_modules": {}}

        template = IpTemplate.from_template_path(
            IP_TEMPLATES_PATH / module["template_type"])

        module_name = params.get("module_instance_name", module["template_type"])
        inst_name = f"top_{completecfg['name']}_{module_name}"
        ip_config = IpConfig(template.params, inst_name, params)

        outfile = args.outdir / (ip_config.instance_name + ".hjson")
        ip_config.to_file(outfile, HEADER.format(args.completecfg.relative_to(REPO_TOP),
                                                 args.outdir.relative_to(REPO_TOP)))

    if args.xbars:
        for xbar in completecfg["xbar"]:
            if not ipgen_is_selected(xbar, args.ips):
                continue
            xbar_name = xbar["name"]
            log.info(f"generating xbar {xbar_name}")
            xbar_hjson_path = args.outdir / f"xbar_{xbar_name}.gen.hjson"
            xbar_hjson_path.write_text(HEADER.format(args.completecfg, args.outdir)
                                       + hjson.dumps(xbar, for_json=True) + '\n')


if __name__ == "__main__":
    main()
