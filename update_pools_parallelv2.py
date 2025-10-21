#!/usr/bin/env python3
"""
# update_pools_parallelv2.py
Bulk update the 'pools' key inside Verus ccminer config across multiple hosts using SSH and a common password. Features include:
 - Remote config backup w/pruning. Will create a folder and store 5 versions of your last changed config with the original version saved as config.json.orig
 - Disable or enable a pool URL, or replace the entire pools list from a JSON file
 - switchpool trigger via telnet on localhost:4068 (remote) - Must have api enabled. Script will function minus switchpool if no api access.
 - Concurrent SSH connections (default workers=10)
 - Only tested on Linux/Termux- Written for interacting with Termux specifically.

**REQUIRES (on system running script):** python and paramiko

**REQUIRES (on target system):** telnet 

| Command | Description | OP |
| --- | --- | --- |
| `--enable-url` | --enable-url "stratum+tcp://ca.vipor.net:5045" | or |
| `--disable-url` | --disable-url "stratum+tcp://ca.vipor.net:5045" | or |
| `--set-pools-json` | --set-pools-json <pools.json> |
| `--switch-pool` | Trigger via telnet on (remote) localhost:4068 |
| `--range` | --range 10.10.10.100-10.10.10.200 | or |
| `--cidr` | --range 10.10.10.0/24 |

## Usage examples:

Disable a single pool URL on a range and switch pool afterwards.
Use the exact pool url and port.

`python3 update_pools_parallelv2.py --range 10.10.10.100-10.10.10.200 --username root --password secret --disable-url "stratum+tcp://ca.vipor.net:5045" --switch-pool`

Replace pools from file, use 20 workers (default 10 processes ~150 clients in <30 seconds)

`python3 update_pools_parallelv2.py --cidr 10.10.10.0/24 --username root --password secret --set-pools-json new_pools.json --workers 20`

Just send switchpool to a set of hosts (no config changes)

`python3 update_pools_parallelv2.py --range 10.10.10.120-10.10.10.130 --username root --password secret --switch-pool`

Format your pools.json just like the pools: block including the closing brackets. [{ }] ie:
````
[{
            "name": "VIPOR-SOLO-West",
            "url": "stratum+tcp://usw.vipor.net:5045",
            "timeout": 60,
            "disabled": 0
        },
        {
            "name": "VIPOR-SOLO-SouthWest",
 ...
        }]
````

"""

from __future__ import annotations
import argparse
import json
import sys
import time
from ipaddress import ip_network, ip_address
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Dict, Any, Tuple

import paramiko

# === Remote config paths - Home-relative paths ===
CONFIG_PATH_RAW = "~/ccminer/config.json"
BACKUP_DIR_RAW = "~/ccminer/configbackups"

# === Defaults ===
DEFAULT_SSH_PORT = 8022
DEFAULT_WORKERS = 10
SSH_CONNECT_TIMEOUT = 10  # seconds


# ---------------- utilities ----------------
def expand_range(range_str: str) -> List[str]:
    """Expand 'start-end' IPv4 range (inclusive) or return single IP."""
    if "-" in range_str:
        start_str, end_str = range_str.split("-", 1)
        start = int(ip_address(start_str.strip()))
        end = int(ip_address(end_str.strip()))
        if end < start:
            raise ValueError("Range end must be >= start")
        return [str(ip_address(i)) for i in range(start, end + 1)]
    else:
        return [range_str]


def expand_cidr(cidr_str: str) -> List[str]:
    net = ip_network(cidr_str, strict=False)
    return [str(ip) for ip in net.hosts()]


# ---------------- SSH helpers ----------------
def run_ssh_command(client: paramiko.SSHClient, cmd: str) -> Tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode("utf-8", errors="ignore")
    err = stderr.read().decode("utf-8", errors="ignore")
    exit_status = stdout.channel.recv_exit_status()
    return exit_status, out, err


def resolve_remote_path(client: paramiko.SSHClient, path: str) -> str:
    if path.startswith("~"):
        stdin, stdout, stderr = client.exec_command(f'echo {path}')
        full_path = stdout.read().decode().strip()
        return full_path
    return path


def make_remote_backup(client: paramiko.SSHClient, config_path: str, backup_dir: str) -> Tuple[bool, str]:
    cmd = f"""
set -e
mkdir -p {backup_dir}
cd {backup_dir}

if [ ! -f config.json.orig ]; then
    cp {config_path} config.json.orig 2>/dev/null || true
fi

[ -f config.json.5 ] && rm -f config.json.5 || true

for i in 4 3 2 1; do
    if [ -f config.json.$i ]; then mv config.json.$i config.json.$((i+1)); fi
done

cp {config_path} config.json.1 2>/dev/null || true
"""
    es, out, err = run_ssh_command(client, cmd)
    success = (es == 0)
    msg = (out + err).strip()
    return success, msg


def remote_send_switchpool(client: paramiko.SSHClient) -> Tuple[bool, str]:
    cmd = '(echo switchpool; sleep 1) | telnet localhost 4068 2>&1 || true'
    es, out, err = run_ssh_command(client, cmd)
    combined = (out + err).lower()
    if "ok|" in combined:
        return True, "switchpool ok"
    return False, combined.strip() or f"exit_status={es}"


# ---------------- config update logic ----------------
def update_pools_list(existing_pools: List[Dict[str, Any]],
                      disable_url: Optional[str] = None,
                      enable_url: Optional[str] = None,
                      new_pools: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    if new_pools is not None:
        return new_pools

    for p in existing_pools:
        if disable_url and p.get("url") == disable_url:
            if p.get("disabled") != 1:
                p["disabled"] = 1
        elif enable_url and p.get("url") == enable_url:
            if p.get("disabled") != 0:
                p["disabled"] = 0

    return existing_pools


def process_host(ip: str, username: str, password: str, port: int,
                 disable_url: Optional[str], enable_url: Optional[str],
                 new_pools: Optional[List[Dict[str, Any]]],
                 do_switchpool: bool) -> Dict[str, Any]:

    result = {"ip": ip, "success": False, "updated": False, "switched": False, "msg": ""}

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, port=port, username=username, password=password, timeout=SSH_CONNECT_TIMEOUT)
    except Exception as e:
        result["msg"] = f"SSH connect failed: {type(e).__name__}: {e}"
        return result

    sftp = None
    try:
        # Resolve actual config and backup paths
        config_path = resolve_remote_path(client, CONFIG_PATH_RAW)
        backup_dir = resolve_remote_path(client, BACKUP_DIR_RAW)

        if disable_url or enable_url or new_pools is not None:
            ok, bak_msg = make_remote_backup(client, config_path, backup_dir)
            if not ok:
                result["msg"] = f"backup failed: {bak_msg}"
                client.close()
                return result

            try:
                sftp = client.open_sftp()
                with sftp.open(config_path, 'r') as rf:
                    data = rf.read().decode('utf-8')
            except IOError as e:
                result["msg"] = f"read config failed: {type(e).__name__}: {e}"
                client.close()
                return result

            try:
                config = json.loads(data)
            except Exception as e:
                result["msg"] = f"json parse failed: {type(e).__name__}: {e}"
                client.close()
                return result

            user_val = config.get("user")

            pools_existing = config.get("pools", [])
            pools_updated = update_pools_list(pools_existing, disable_url=disable_url, enable_url=enable_url, new_pools=new_pools)
            config["pools"] = pools_updated

            if user_val is not None:
                config["user"] = user_val

            try:
                new_json = json.dumps(config, indent=4)
                with sftp.open(config_path, 'w') as wf:
                    wf.write(new_json)
            except Exception as e:
                result["msg"] = f"write config failed: {type(e).__name__}: {e}"
                client.close()
                return result

            result["updated"] = True
            result["msg"] = "config updated"

        if do_switchpool:
            switched_ok, switched_msg = remote_send_switchpool(client)
            result["switched"] = switched_ok
            if result["msg"]:
                result["msg"] += " | "
            result["msg"] += f"switchpool: {switched_msg}"

        result["success"] = bool(result["updated"] or result["switched"])

    except Exception as e:
        result["msg"] = f"Unhandled error: {type(e).__name__}: {e}"
    finally:
        try:
            if sftp:
                sftp.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass

    return result


# ---------------- main / CLI ----------------
def main():
    p = argparse.ArgumentParser(description="Bulk update ccminer config 'pools' and optionally call switchpool (telnet).")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--range", help="IP range (inclusive), e.g. 10.10.10.100-10.10.10.150")
    group.add_argument("--cidr", help="CIDR block, e.g. 10.10.10.0/24")
    p.add_argument("--username", required=True, help="SSH username")
    p.add_argument("--password", required=True, help="SSH password")
    p.add_argument("--port", type=int, default=DEFAULT_SSH_PORT, help=f"SSH port (default {DEFAULT_SSH_PORT})")
    p.add_argument("--disable-url", help="Pool URL to set disabled=1")
    p.add_argument("--enable-url", help="Pool URL to set disabled=0")
    p.add_argument("--set-pools-json", help="Path to JSON file containing replacement pools list")
    p.add_argument("--switch-pool", action="store_true", help="Send 'switchpool' to localhost:4068 on the remote host")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Concurrent SSH workers (default {DEFAULT_WORKERS})")
    args = p.parse_args()

    if not any([args.disable_url, args.enable_url, args.set_pools_json, args.switch_pool]):
        print("Error: specify at least one action: --disable-url, --enable-url, --set-pools-json, or --switch-pool", file=sys.stderr)
        sys.exit(2)

    try:
        if args.range:
            hosts = expand_range(args.range)
        else:
            hosts = expand_cidr(args.cidr)
    except Exception as e:
        print(f"Invalid range/cidr: {e}", file=sys.stderr)
        sys.exit(2)

    new_pools = None
    if args.set_pools_json:
        try:
            with open(args.set_pools_json, 'r', encoding='utf-8') as f:
                new_pools = json.load(f)
            if not isinstance(new_pools, list):
                raise ValueError("The new pools JSON must be an array/list of pool objects")
        except Exception as e:
            print(f"Failed to load --set-pools-json: {e}", file=sys.stderr)
            sys.exit(2)

    total = len(hosts)
    if total == 0:
        print("No hosts found to process.", file=sys.stderr)
        sys.exit(0)

    print(f"Starting: {total} hosts, workers={args.workers}, ssh-port={args.port}")
    start_time = time.time()

    results = []
    succeeded = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        future_to_ip = {
            ex.submit(process_host, ip, args.username, args.password, args.port,
                      args.disable_url, args.enable_url, new_pools, args.switch_pool): ip
            for ip in hosts
        }

        for i, fut in enumerate(as_completed(future_to_ip), 1):
            ip = future_to_ip[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"ip": ip, "success": False, "updated": False, "switched": False, "msg": f"executor error: {e}"}
            results.append(res)
            if res.get("success"):
                succeeded += 1
                status = "OK"
            else:
                failed += 1
                status = "FAIL"
            print(f"[{i}/{total}] {ip} {status} - {res.get('msg', '')}")

    elapsed = time.time() - start_time
    print("\n=== Summary ===")
    print(f"Total hosts : {total}")
    print(f"Successes   : {succeeded}")
    print(f"Failures    : {failed}")
    print(f"Elapsed     : {elapsed:.2f}s")

    if failed:
        print("\nFailed hosts details:")
        for r in results:
            if not r.get("success"):
                print(f" - {r['ip']}: {r.get('msg')}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

