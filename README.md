# update_pools_parallelv2.py
Bulk update the 'pools' key inside Verus ccminer config across multiple hosts using SSH and a common password. Features include:
 - Remote config backup w/pruning. Will create a folder and store 5 versions of your last changed config with the original version saved as config.json.orig
 - Disable or enable a pool URL, or replace the entire pools list from a JSON file.
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
| `--switchpool` | Trigger via telnet on (remote) localhost:4068 |
| `--range` | --range 10.10.10.100-10.10.10.200 | or |
| `--cidr` | --range 10.10.10.0/24 |

## Usage examples:

Disable a single pool URL on a range and switch pool afterwards.
Use the exact pool url and port.

`python3 update_pools_parallelv2.py --range 10.10.10.100-10.10.10.200 --username root --password secret --disable-url "stratum+tcp://ca.vipor.net:5045" --switch-pool`

Replace pools from file, use 20 workers (default 10 processes ~150 clients in <30 seconds).

`python3 update_pools_parallelv2.py --cidr 10.10.10.0/24 --username root --password secret --set-pools-json new_pools.json --workers 20`

Just send switchpool to a set of hosts (no config changes).

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
