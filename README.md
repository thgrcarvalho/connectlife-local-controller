# ConnectLife Local Controller

Local/hybrid control for a compatible Hisense air conditioner that uses the
ConnectLife cloud. The project combines normal ConnectLife API control with a
local replay fallback for recorded commands.

**No credentials, device identifiers, keys, signed captures, or personal logs
are included in this repository.** Each user must capture commands from their
own AC. The capture procedure is reusable; the captured bytes are not.

## Features

- Online control through the ConnectLife API.
- Health checks for cloud reachability, appliance connectivity, and recent
  target-AC TLS activity, including partial device-link outages where the
  upstream connection cannot be established.
- Target-only DNS interception and capture, leaving other LAN devices alone.
- Bounded capture retention with explicit protection for the offline session.
- Automatic fallback to local replay after three actionable online health failures.
- Automatic return to online mode after two successful cloud checks, with time
  for the appliance to reconnect before its connectivity is enforced again.
- Relay, replay, and DNS process supervision with bounded exponential restart backoff.
- Serialized health transitions and client commands on one event loop, with
  cancellable local I/O and bounded queued/action execution time.
- Thirty-second healthy cloud polling and five-second failure/recovery polling.
- Power, temperature, mode, fan-speed, and Sleep commands when recorded.
- Online self-diagnosis and offline diagnosis from live fault flags.
- Status updates when the physical remote changes the AC.
- Windows Credential Manager storage for ConnectLife credentials.
- Localhost-only command sockets and background startup without a console.
- Capture inspection without printing captured payloads.

This is not a general implementation of the ConnectLife encryption protocol.
Offline replay supports only commands recorded from the same AC. It does not
implement provisioning, factory reset, OTA updates, or key recovery.

## Architecture

```text
Desktop CLI / another localhost client
                |
                v
      controller.py (one instance)
          |             |
   healthy cloud    cloud unavailable
          |             |
 ConnectLife API    replay_server.py
          |             |
     Hisense cloud      local AC

relay.py: real-cloud relay and capture path
windows_dns.py: narrow hostname override forwarded to the PC
```

Online mode allows the ConnectLife app and PC control to coexist. During
standalone replay, ConnectLife considers the AC offline. Offline control
requires the PC and AC to be on the same LAN.

## Requirements

- A compatible ConnectLife AC whose firmware accepts the local test
  certificate. This was verified on one Hisense window AC; other models and
  firmware versions may not work.
- A Windows PC with Python 3.12. WSL is optional for development tools, but the
  reliable runtime layout uses only native Windows files and processes.
- A router that can override one DNS hostname without making the PC the DNS
  server for the whole network.
- Administrator privileges for low-numbered ports and firewall rules.
- The Python packages in `requirements.txt`.

The relay/replay services use TCP 443 and the DNS proxy uses UDP 53. Keep them
on a trusted LAN only.

## Installation

For a dependable startup, deploy the controller on a local Windows filesystem,
not through a `\\wsl.localhost` path. Install its dependencies from the repository
root:

```powershell
python.exe -m pip install --target fakecloud\.windows-deps -r requirements.txt
```

WSL is useful for the optional capture-inspection tools:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Generate a new local certificate. Do not reuse certificates from another user:

```sh
bash fakecloud/gen_cert.sh
```

`server.crt` and `server.key` are generated locally and ignored by Git. Bash and
OpenSSL are needed only to generate the certificate, not by the running service.

Copy and edit the controller configuration:

```powershell
Copy-Item config.example.json fakecloud\config.json
```

Configuration fields:

- `bind_ip`: this PC's LAN IP.
- `ac_ip`: the target AC's LAN IP. The relay uses it to isolate local health and
  command state from any other ConnectLife devices on the network. A DHCP
  reservation for the AC is recommended.
- `upstream_dns`: the normal LAN resolver used by the DNS proxy, usually the
  ISP modem/router.
- `cloud_upstream_dns`: a non-hijacked resolver, such as a public DNS service,
  used to discover the real device-link address whenever the relay reconnects.
  Do not use the router that contains the device-link hostname override.
- `cloud_upstream_ip`: optional fallback if dynamic resolution fails. Resolve it
  outside the hijack before adding it. `null` disables the fallback.
- `hijack_domain`: normally `ac-eu-link.hijuconn.com`.
- `offline_capture`: capture basename, without `.c2s.bin` or `.s2c.bin`.
- `command_map`: JSON file mapping action names to captured command indices.
- `capture_retention_days`: age limit for old capture binaries; `0` disables
  cleanup. The configured `offline_capture` is always protected. Non-target
  devices are not captured when `ac_ip` is set.
- `reconnect_grace_seconds`: time allowed for the AC to reconnect after entering
  online mode before appliance disconnects count as failed health checks.
- `local_control_retry_seconds`: deadline for replay's localhost control service
  to become ready when a client request arrives immediately after failover.
- `health_interval_seconds`: cloud polling interval while healthy; defaults to 30.
- `health_retry_seconds`: polling interval during failures and offline recovery;
  defaults to 5. Child supervision continues every five seconds independently.
- `router_path_warning_seconds`: log a warning when the cloud reports the AC
  online but no target TLS activity is seen for this long; defaults to 3600.
  This does not change modes or commands. `0` disables the warning.
- `rehearsal_interval_seconds`: optional offline rehearsal interval; `0` disables
  rehearsal. See the rehearsal section before enabling it.
- `rehearsal_timeout_seconds` and `rehearsal_command`: reconnect deadline and the
  recorded command used by an enabled rehearsal.

Relative capture paths are resolved from `fakecloud/`. `config.json` is ignored
by Git. `AC_LOCAL_CONFIG` can point to a configuration in another location.

## Router DNS configuration

Give **only the target AC** the PC as its DHCP DNS server. The Windows proxy
returns the synthetic device-link address only to `ac_ip` and forwards all other
queries normally. Do not create a LAN-wide hostname override: another
ConnectLife device may reject the local certificate. Leave WAN DNS automatic.

For routers using OpenWrt-style UCI, reserve the AC address and tag its DHCP
options. Replace the example MAC and addresses:

```uci
config host
    option name 'target-ac'
    option mac 'aa:bb:cc:dd:ee:ff'
    option ip '192.168.31.101'
    option tag 'ac_dns'

config tag 'ac_dns'
    option dhcp_option '6,192.168.31.100'
```

Here `192.168.31.101` is the AC and `192.168.31.100` is the PC. Remove any
existing global `config domain` entry for `ac-eu-link.hijuconn.com`. Apply the
configuration, restart the router's DHCP/DNS service, and renew the AC's DHCP
lease if necessary. Reboot the router and confirm persistence. Router DNS must
return real public addresses for both the device-link hostname and ordinary
names; only the AC receives the PC proxy through DHCP.

On other routers, use the equivalent per-client DHCP DNS option. If per-client
DNS is unavailable, do not apply the override to other ConnectLife devices.

### Stock firmware without per-client DHCP options

Some Xiaomi stock firmware records DHCP tags but does not generate the options.
A router-local secondary DNS responder avoids that limitation and also avoids a
source-address mismatch in redirected replies. It replaces the Windows DNS proxy
for this route. For example, run a second `dnsmasq` on router port 5354 with a
persistent configuration such as:

```text
port=5354
no-resolv
server=192.168.1.1
listen-address=192.168.31.1
bind-interfaces
address=/ac-eu-link.hijuconn.com/192.168.31.100
pid-file=/tmp/ac-local-dnsmasq.pid
```

Use the router's normal upstream resolver and replace the PC/router example
addresses. Redirect only the target AC's DNS traffic to this local responder:

```uci
config redirect 'ac_dns_udp'
    option name 'AC-DNS-UDP'
    option src 'lan'
    option dest 'lan'
    option proto 'udp'
    option src_ip '192.168.31.101'
    option src_dport '53'
    option dest_ip '192.168.31.1'
    option dest_port '5354'
    option target 'DNAT'
```

Add the corresponding TCP rule. Remove any global device-link domain override
and do not add source NAT for this router-local destination. Persist the
secondary service in `/data` and start it through a firewall include, which the
tested Xiaomi firmware runs at boot and on every firewall restart:

```uci
config include 'ac_local_dns'
    option path '/data/ac-local-dns.sh'
    option type 'script'
```

Use this exact `/data/ac-local-dns.sh` startup script:

```sh
#!/bin/sh
if [ -f /tmp/ac-local-dnsmasq.pid ] && kill -0 "$(cat /tmp/ac-local-dnsmasq.pid)" 2>/dev/null; then
    exit 0
fi
exec dnsmasq --conf-file=/data/ac-local-dnsmasq.conf
```

After creating the configuration and script, apply the UCI sections shown above,
then enable and start the service:

```sh
chmod 700 /data/ac-local-dns.sh
uci set firewall.ac_local_dns=include
uci set firewall.ac_local_dns.path=/data/ac-local-dns.sh
uci set firewall.ac_local_dns.type=script
uci commit dhcp
uci commit firewall
/data/ac-local-dns.sh
/etc/init.d/firewall restart
```

Verify the process, listener, redirect, and ordinary router DNS:

```sh
kill -0 "$(cat /tmp/ac-local-dnsmasq.pid)"
netstat -lntu | grep 5354
iptables -t nat -S | grep 5354
nslookup www.google.com 192.168.31.1
```

Reboot the router and repeat those checks. The AC should then appear in the
relay log and report `tls_recent=true` through the localhost relay status.

To remove the custom path and restore direct-cloud DNS, run:

```sh
uci delete firewall.ac_dns_udp
uci delete firewall.ac_dns_tcp
uci delete firewall.ac_local_dns
uci commit firewall
/etc/init.d/firewall restart
kill "$(cat /tmp/ac-local-dnsmasq.pid)"
rm -f /data/ac-local-dnsmasq.conf /data/ac-local-dns.sh
rm -f /tmp/ac-local-dnsmasq.pid
```

Keep a copy of the exact `/data` files and target UCI sections outside the
router. A factory reset or firmware update can remove `/data`; restore those
artifacts and repeat verification rather than recreating a global override.
While the cloud reports the AC online, `router_path_warning_seconds` makes a
missing relay path visible in the controller log.

The relay resolves the real cloud hostname through `cloud_upstream_dns` on each
new upstream connection. This resolver must be outside the hostname override.
An optional verified `cloud_upstream_ip` is used only if that lookup fails.

## Capture signed commands from your own AC

Captures can contain session tokens, device identifiers, status data, and
signed commands. Keep them private. The `fakecloud/captures/` directory is
ignored by Git.

1. Generate the certificate and configure a target-only DNS path.
2. Start the real-cloud relay with the PC and AC LAN IPs plus a non-hijacked resolver:

   ```powershell
   python.exe fakecloud\relay.py --bind 192.168.31.100 --ac-ip 192.168.31.101 --upstream-dns 1.1.1.1
   ```

   Replace the example addresses. Raw payloads are not logged by default.
   `--log-payloads` exists for deliberate local research and may expose secrets.

3. Reconnect or power-cycle only the AC if necessary to make it establish a new
   TLS session through the relay.
4. In one uninterrupted ConnectLife session, perform every command you want to
   replay. Include the complete handshake, power on/off, every temperature,
   modes, fan speeds, and Sleep on/off as applicable.
5. Stop the relay after the commands have been acknowledged. It writes:

   ```text
   fakecloud/captures/<timestamp>-<connection>.c2s.bin
   fakecloud/captures/<timestamp>-<connection>.s2c.bin
   ```

6. List the captured cloud-to-AC command indices without printing payloads:

   ```sh
   .venv/bin/python fakecloud/capture_tools.py list \
     fakecloud/captures/<capture>.s2c.bin
   ```

   Indices are zero-based among `id=201` frames, not among all frames. Compare
   this list with the order in which you performed commands in the app.

7. Create `fakecloud/captures/commands.json` using
   `examples/commands.json` as a starting point. Supported controller actions
   are `on`, `off`, `temp16` through `temp30`, `fan`, `cool`, `dry`, `eco`,
   `fanlow`, `fanmed`, `fanhigh`, `sleep`, and `sleepoff`.

8. Validate the map:

   ```sh
   .venv/bin/python fakecloud/capture_tools.py validate \
     fakecloud/captures/<capture>.s2c.bin fakecloud/captures/commands.json
   ```

9. Set `offline_capture` in `fakecloud/config.json` to
   `captures/<capture>` and `command_map` to `captures/commands.json`.

**Use one complete capture session. Do not mix command frames from different
sessions.** A command map alone is insufficient; the matching `.s2c.bin` file
and the complete recorded handshake responses are required.

## Standalone replay

Replay does not contact the cloud and does not require the device key:

```powershell
python.exe fakecloud\replay_server.py --bind 192.168.31.100 `
  --capture fakecloud\captures\<capture> `
  --command-map fakecloud\captures\commands.json
```

Use another terminal for status and commands:

```powershell
python.exe fakecloud\replay_cli.py status
python.exe fakecloud\replay_cli.py on
python.exe fakecloud\replay_cli.py temp26
python.exe fakecloud\replay_cli.py sleepoff
```

ConnectLife is offline while replay owns the AC connection. Stop replay and
start the relay (or the hybrid controller) to return to real-cloud operation.

## Hybrid controller

Store your ConnectLife credentials once in Windows Credential Manager:

```powershell
cd fakecloud
python.exe controller_cli.py setup
python.exe controller_cli.py run
```

The credential target is `ConnectLife/local-controller`. Credentials are not
written to `config.json` or the repository. `reset-credentials` deletes the
stored entry.

From another terminal in `fakecloud/`:

```powershell
python.exe controller_cli.py status
python.exe controller_cli.py diagnose
python.exe controller_cli.py on
python.exe controller_cli.py off
python.exe controller_cli.py temp26
python.exe controller_cli.py eco
python.exe controller_cli.py fanhigh
python.exe controller_cli.py sleep
python.exe controller_cli.py sleepoff
```

The controller validates mode constraints: temperature requires power on and a
temperature-capable mode, Fan mode has no temperature or Sleep control, and Dry
mode fixes fan speed to High. Client actions, health checks, and mode changes
are serialized so they cannot modify shared API and connection state concurrently.
A server-side 30-second deadline covers both lock waiting and execution. Requests
whose clients disconnect while queued are discarded before execution.

Cloud API failures can trigger offline replay whether or not the AC is present.
If the API is reachable but reports the appliance offline, the controller checks
recent target-AC TLS activity reported by the relay. The relay records this
activity before attempting the Hisense upstream connection, so a partial
device-link outage remains visible. The history expires after 90 seconds without
an active connection or new handshake, avoiding perpetual failover while the AC
is absent. Other devices
have isolated state and cannot affect this decision. Offline recovery initially
requires only cloud reachability; after switching online,
`reconnect_grace_seconds` allows the AC to reconnect. The controller restarts
missing or exited relay, replay, and DNS children with bounded exponential
backoff after repeated failures.

For invisible startup at Windows login, create a Startup shortcut that runs
`pythonw.exe controller_cli.py run` using an absolute native Windows script path
and a native Windows `fakecloud/` working directory. Do not use a WSL UNC path
for this runtime shortcut. Keep exactly one controller instance running.

## Optional offline rehearsal

Replay compatibility can change if the cloud rotates session material or a
firmware update changes the protocol. An optional rehearsal can periodically
verify that the AC acknowledges one selected recorded command before a real
outage. It does not prove that every command or every future capture will work.

Rehearsal is disabled by default. To enable it, set
`rehearsal_interval_seconds` to a positive interval and choose a
`rehearsal_command` present in the command map. Each rehearsal:

1. Stops the real-cloud relay and starts offline replay.
2. Waits up to `rehearsal_timeout_seconds` for the AC to reconnect.
3. Sends the selected command and requires its acknowledgment.
4. Restores online relay mode, including after a failed rehearsal.

This temporarily disconnects ConnectLife and changes the selected AC setting;
the original setting is not automatically restored. Choose a command whose
effect is acceptable and do not enable frequent rehearsals.

## Diagnosis

Online diagnosis uses ConnectLife's self-check API and reports the entries in
`selfCheckFailedList`. Offline diagnosis evaluates live fault flags for:

- Indoor room and coil temperature sensors.
- Indoor/outdoor communication.
- Indoor control-panel EEPROM.
- Overcooling and overheating protection.

`self_check_inspect.py` is an optional standalone metadata inspector. Its output
file contains identifiers and is ignored by Git.

## Troubleshooting

- **Missing configuration:** copy `config.example.json` to
  `fakecloud/config.json` and replace every placeholder.
- **AC does not reconnect:** confirm its DHCP lease supplies the PC as DNS, the
  proxy accepts its `ac_ip`, and TCP 443/UDP 53 are allowed on the LAN.
- **Relay loops back to itself:** configure `cloud_upstream_dns` as a resolver
  outside the hostname override. Check any optional `cloud_upstream_ip` fallback
  in the same way.
- **Offline command is unavailable:** record that action in the same complete
  capture and update the command map.
- **ConnectLife shows the AC offline:** this is expected during standalone
  replay; return to online mode for simultaneous app/cloud access.
- **Another device has certificate errors:** remove any LAN-wide hostname
  override and use only the per-AC DNS configuration.
- **DNS fails when the PC is off:** only the target AC should use the PC relay.
  Other clients must receive normal DNS from the router, with WAN DNS automatic.
- **After a router reboot:** verify that the per-client DNS rule and any
  router-local responder persist.

## Security and limitations

Use this only on devices and networks you own or are authorized to administer.
The tested firmware accepts a self-signed certificate for the local relay.
Keep capture files, payload logs, credentials, and certificates private, and do
not expose the relay or control sockets to untrusted networks.

No firmware is flashed by this project. No OTA, provisioning, reset, or key
operations are supported. A future firmware update may change compatibility.

## License

MIT. See [LICENSE](LICENSE).
