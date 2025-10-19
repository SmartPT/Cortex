#!/usr/bin/env python3
"""
Connects to switches via SSH, fetches MAC addresses learned on ACCESS ports only,
and saves the results in a CSV file (MAC Address, Port, Switch IP).
Trunk ports (including port-channels and their member links) are excluded.
"""

import paramiko
import csv
import re
from pathlib import Path
import os

# ─── CONFIG ────────────────────────────────────────────────────────────────
SWITCH_LIST = "switches.txt"   # File with one IP per line
SSH_USER = os.getenv("SSH_USER", "")
SSH_PASS = os.getenv("SSH_PASS", "")
SSH_PORT = 22
OUTPUT_CSV = "mac_ports_access_only.csv"

# Commands (Cisco IOS/IOS-XE style)
SHOW_MAC_CMD = "show mac address-table"
SHOW_TRUNK_CMD = "show interfaces trunk"
SHOW_EC_SUMMARY_CMD = "show etherchannel summary"
# ───────────────────────────────────────────────────────────────────────────

def read_switch_list(file_path: str):
    return [line.strip() for line in Path(file_path).read_text().splitlines() if line.strip()]

def ssh_run_command(host: str, command: str) -> str:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, username=SSH_USER, password=SSH_PASS, port=SSH_PORT, timeout=15)
        stdin, stdout, stderr = client.exec_command(command)
        return stdout.read().decode(errors="ignore")
    finally:
        client.close()

# ── Parsing helpers ────────────────────────────────────────────────────────
def parse_trunk_ports(trunk_output: str) -> set[str]:
    """
    Parses `show interfaces trunk` and returns a set of trunk interface names,
    e.g., {'Gi0/7','Po1'}.
    """
    trunks: set[str] = set()
    for line in trunk_output.splitlines():
        line = line.strip()
        # Lines with: <Port> <Mode> <Encapsulation> <Status> ...
        # We capture the first column when Status contains 'trunking'
        m = re.match(r"^(\S+)\s+\S+\s+\S+\s+trunking\b", line, flags=re.IGNORECASE)
        if m:
            trunks.add(m.group(1))
    return trunks

def parse_portchannel_members(ec_summary_output: str) -> dict[str, set[str]]:
    """
    Parses `show etherchannel summary` to map PoX -> {Gi..., Te..., etc}.
    Returns dict like {'Po1': {'Gi0/1','Gi0/2'}}.
    """
    mapping: dict[str, set[str]] = {}
    for line in ec_summary_output.splitlines():
        # Example:
        # "1      Po1(SU)     LACP      Gi0/1(P) Gi0/2(P)"
        po = re.search(r"\bPo(\d+)\(", line)
        if not po:
            continue
        po_name = f"Po{po.group(1)}"
        # collect all interface tokens like Gi0/1(P), Te1/0/1(D), Et1(P), etc.
        members = set()
        for tok in line.split():
            # Strip trailing role marks like (P), (D), (H)
            t = re.sub(r"\([A-Za-z]+\)$", "", tok)
            if re.match(r"^(Gi|Fa|Te|Hu|Et)\d+([/\.]\d+){0,3}$", t):  # flexible enough for Gi1/0/1, Te0/0/0, etc.
                members.add(t)
        if members:
            mapping[po_name] = members
    return mapping

def parse_mac_output(output: str, switch_ip: str, excluded_ports: set[str]):
    """
    Parses Cisco-style 'show mac address-table' and filters out entries learned
    on trunk or port-channel/member ports listed in excluded_ports.
    """
    results = []
    for line in output.splitlines():
        # Typical line: "  10   0011.2233.4455   DYNAMIC   Gi1/0/1"
        # Also skip lines that show the CPU or Router as the port.
        m = re.search(r"\b([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}|[0-9a-fA-F]{12})\b.*\b([A-Za-z]+[0-9/\.]+)\b", line)
        if not m:
            continue
        mac = m.group(1).lower()
        port = m.group(2)

        # Ignore non-physical "ports" like CPU/Router
        if port.upper() in {"CPU", "ROUTER"}:
            continue

        if port not in excluded_ports:
            results.append((mac, port, switch_ip))
    return results

def build_excluded_ports(host: str) -> set[str]:
    """
    Finds all trunk ports (PoX and standalone trunks) and expands PoX to member interfaces.
    Returns a set of interface names to exclude.
    """
    excluded = set()

    trunk_out = ssh_run_command(host, SHOW_TRUNK_CMD)
    ec_out = ssh_run_command(host, SHOW_EC_SUMMARY_CMD)

    trunks = parse_trunk_ports(trunk_out)  # e.g., {'Gi0/7','Po1'}
    excluded.update(trunks)

    po_members = parse_portchannel_members(ec_out)  # {'Po1': {'Gi0/1','Gi0/2'}}
    for po, members in po_members.items():
        if po in trunks:
            excluded.update(members)  # exclude member links of trunked Po
    return excluded

# ── Main collection logic ──────────────────────────────────────────────────
def collect_mac_port_data(switch_ips: list[str]) -> list[tuple[str, str, str]]:
    all_data = []
    for ip in switch_ips:
        print(f"🔌 Connecting to {ip}")
        try:
            excluded_ports = build_excluded_ports(ip)
            print(f"   • Excluding {len(excluded_ports)} trunk/Po member ports on {ip}: {sorted(excluded_ports)}")

            mac_out = ssh_run_command(ip, SHOW_MAC_CMD)
            mac_port_switch = parse_mac_output(mac_out, ip, excluded_ports)
            all_data.extend(mac_port_switch)
        except Exception as e:
            print(f"❌ Failed on {ip}: {e}")
    return all_data

def save_to_csv(data: list[tuple[str, str, str]], filename: str):
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["MAC Address", "Port", "Switch IP"])
        writer.writerows(sorted(set(data)))  # de-dup + sort for readability

def main():
    switch_ips = read_switch_list(SWITCH_LIST)
    data = collect_mac_port_data(switch_ips)
    save_to_csv(data, OUTPUT_CSV)
    print(f"✅ ACCESS-only MAC list saved to: {OUTPUT_CSV} (rows: {len(data)})")

if __name__ == "__main__":
    main()
