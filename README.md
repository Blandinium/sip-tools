# SIP-tools
Some Asterisk-related scripts

## update-asterisk.sh
Will install or update Asterisk 22 from source. Previous build folders are kept, to allow easy rollback.

With `--check`, it installs nothing. It compares the latest 22.x release with the installed version (`asterisk -V`, no root needed) and sends a notification if a newer one exists, once per release. Run it daily from cron:

```
17 8 * * * /usr/local/sbin/update-asterisk-22.sh --check >> ~/update-asterisk-check.log 2>&1
```

Environment settings for `--check`:

| Variable | Default | Meaning |
|---|---|---|
| `ASTERISK_NOTIFY_HOOK` | `/usr/local/bin/pushover.sh` | Command called with the message as argument; empty disables notifications |
| `ASTERISK_BIN` | `asterisk` from `PATH`, else `/usr/sbin/asterisk` | Binary queried for the installed version |
| `ASTERISK_CHECK_STATE` | `${XDG_STATE_HOME:-~/.local/state}/update-asterisk/notified` | Last version notified about |

## tg200_sim_status.sh
The SIM cards in my Yeastar TG200 often lose their connection and don't seem to automatically recover. This script will check the SIM status in the web interface. If a SIM remains in an error state for at least 2 script runs, and other SIMs are either in error or IDLE (no call is ongoing), the TG200 will be rebooted (Ideally, I could reboot only the offending module. But in my experience, that does not resolve the error, while a full system reboot does)

It also sends a notification when:
- a SIM changes state;
- the web interface can't be reached or the login fails;
- a SIP trunk, as shown on the TG200's status page, is not `OK` for `SIP_FAIL_RUNS` runs in a row (the TG200 can't reach its SIP server).

Notifications about unreachability and SIP trunks repeat every `REPEAT_HOURS` while the fault lasts, and a final one is sent on recovery.

Run it from cron, e.g. every 10 minutes:

```
*/10 * * * * /usr/bin/flock -n ~/.cache/tg200_sim_status.lock /bin/bash ~/bin/tg200_sim_status.sh >> ~/tg200_sim_status.log 2>&1
```

### Configuration
Files live in `${XDG_CONFIG_HOME:-~/.config}/yeastar/`, named after the device ID (`tg200` by default, set `YEASTAR_ID` to change it). The script keeps its state files there too.

Required, one value per file:
- `<id>.host`: host name or IP address of the web interface
- `<id>.user`: web interface user name
- `<id>.pass`: web interface password

Optional, `<id>.conf` with `KEY=value` lines (`#` starts a comment). Each setting can also be given as an environment variable `YEASTAR_<KEY>`, which takes precedence over the file.

| Key | Default | Meaning |
|---|---|---|
| `NOTIFY_HOOK` | `/usr/local/bin/pushover.sh` | Command called with the message as its argument. Empty disables notifications. |
| `REPEAT_HOURS` | `6` | Repeat fault notifications this often while the fault lasts. `0` disables repeats. |
| `SIP_FAIL_RUNS` | `2` | Consecutive runs a SIP trunk must be down before notifying. |

Example `tg200.conf`:

```
NOTIFY_HOOK=/usr/local/bin/pushover.sh
REPEAT_HOURS=6
SIP_FAIL_RUNS=2
```

## tg200_clock_watchdog.sh
Keeps the TG200's clock close to the host's own (NTP-synced) clock. My TG200 has no battery-backed clock and sometimes comes back from a power cut in 1970. With a clock that far off, its OpenVPN client rejects the server certificate and the tunnel stays down.

Each run reads the TG200's time from the `Date` header of its login page, which needs no login. If it is off by more than `MAX_DRIFT_MINUTES`, the script logs in and sets the time on the Date and Time page. The page only takes a time in manual mode, so the script posts that first, then switches NTP back on if it was on, and clicks "Apply Changes". The form has no seconds, so the clock ends up within about 30 seconds. My TG200's NTP client fixes small drift, slowly, but not a clock that is far off; the watchdog covers that case.

The form takes local time. The script doesn't work that out from the timezone name, because the TG200 has its own, older timezone rules, which differ from current ones for some zones. Instead it measures the offset the TG200 itself applies: the Date and Time page shows the TG200's local time, and the HTTP `Date` header of the same response its UTC time. If the clock was far off, that offset can be for the wrong side of a DST change; the script then checks the result, measures again and retries once. If the clock is still wrong, it notifies.

It also notifies when it set the clock, and when it can't reach the web interface (repeated every `REPEAT_HOURS`, with a final message on recovery).

The script needs to reach the TG200's web interface directly, so run it on a host on the same LAN. Use `--dry-run` to log in and show what would be posted, without changing anything.

Run it from cron, e.g. every 15 minutes:

```
*/15 * * * * /usr/bin/flock -n ~/.cache/tg200_clock_watchdog.lock /bin/bash ~/bin/tg200_clock_watchdog.sh >> ~/tg200_clock_watchdog.log 2>&1
```

### Configuration
The same files as `tg200_sim_status.sh`, in `${XDG_CONFIG_HOME:-~/.config}/yeastar/`: `<id>.host`, `<id>.user`, `<id>.pass`, and optionally `<id>.conf`. Settings can also be given as `YEASTAR_<KEY>` environment variables.

| Key | Default | Meaning |
|---|---|---|
| `MAX_DRIFT_MINUTES` | `10` | Set the clock when it is off by more than this. |
| `LAN_SUBNET` | (none) | First three octets of a /24, e.g. `192.168.1`. If set, and the TG200 doesn't answer at the last known address, the script searches this /24 for it (by its web page, not just an open port). `<id>.host` is then optional. |
| `NOTIFY_HOOK` | `/usr/local/bin/pushover.sh` | Command called with the message as its argument. Empty disables notifications. |
| `REPEAT_HOURS` | `6` | Repeat fault notifications this often while the fault lasts. `0` disables repeats. |

The address where the TG200 last answered is kept in `<id>.clock.foundhost` and tried first on the next run, then `<id>.host`, then the search. If `<id>.conf` is shared with `tg200_sim_status.sh`, that script logs the keys above as unknown; that is harmless.

## phone_reachability.sh
Checks that SIP phones registered with Asterisk (PJSIP) can actually be reached, and sends a notification when one can't. A phone fails the check when:
- its AOR has no contact in `pjsip show contacts`, or none from the expected host;
- the contact's status is not `Avail` (Asterisk's SIP OPTIONS qualify got no answer);
- optionally, it doesn't answer a single ping.

After `FAIL_RUNS` failed runs in a row it notifies, repeats every `REPEAT_HOURS` while the phone stays unreachable, and notifies once when it recovers.

Asterisk only marks a contact `Unavail` if it qualifies it, so set `qualify_frequency` on the phone's AOR, for example:

```
[2001](phone-aor)
qualify_frequency=60
qualify_timeout=5.0
```

Without it the status is `NonQual`, and the script can only check that the phone is registered. A crashed softphone then still looks registered until its registration expires. Asterisk applies a new `qualify_frequency` to a contact only when the phone next registers, so after changing it, re-register the phone (or wait for its registration to renew).

`pjsip show contacts` cuts the contact URI to a fixed width of about 46 characters, counting the `<aor>/` prefix. `CONTACT_HOST` only matches if the host falls within that width; with long user names or IPv6 addresses, leave it unset.

The script runs `asterisk -rx`, which normally needs root.

### Configuration
One config file per phone, `KEY=value` lines (`#` starts a comment). Pass one or more config files; each phone is checked independently, with its own state and lock:

```
phone_reachability.sh /etc/phone-reachability/*.conf
```

| Key | Default | Meaning |
|---|---|---|
| `AOR` | (required) | PJSIP AOR of the phone, e.g. `2001` |
| `NAME` | config file name without `.conf` | Name used in messages and state file names. Must be unique. |
| `CONTACT_HOST` | (any) | Host or IP address the contact must be registered from |
| `PING_HOST` | (no ping) | Host or IP address to ping. The check fails only if no ping gets a reply. |
| `PING_COUNT` | `3` | Pings per run |
| `PING_TIMEOUT` | `5` | Seconds to wait for each reply |
| `FAIL_RUNS` | `2` | Consecutive failed runs before notifying |
| `REPEAT_HOURS` | `6` | Repeat the notification this often while the phone stays unreachable. `0` disables repeats. |
| `NOTIFY_HOOK` | `/usr/local/bin/pushover.sh` | Command called with the message as its argument. Empty disables notifications. |
| `ASTERISK` | `/usr/sbin/asterisk` | Path to the Asterisk binary |
| `STATE_DIR` | `/var/lib/phone-reachability` as root, else `~/.local/state/phone-reachability` | Where the state files are kept |

Example `/etc/phone-reachability/alice.conf`:

```
AOR=2001
CONTACT_HOST=192.0.2.30
PING_HOST=192.0.2.30
```

Example `/etc/cron.d/phone-reachability`, every 5 minutes:

```
*/5 * * * * root /usr/local/sbin/phone_reachability.sh /etc/phone-reachability/*.conf >> /var/log/phone-reachability.log 2>&1
```

Phones that sleep (Android Doze) can answer pings and SIP OPTIONS slowly or not at all for short periods. If you get false alarms, raise `FAIL_RUNS`, `PING_TIMEOUT` or `qualify_timeout`.

## sms_bridge.py

Requires Linux and Python 3.11 or newer.

Bridges incoming SMS on a Yeastar TG-series gateway to the SIP phone paired
with that SIM. Authenticated phones can send SMS through their own SIM or
message each other by extension or by any configured number alias. Local
messages show the sender's primary number so replies stay local.

The bridge looks up registered UDP contacts over Asterisk AMI and sends SIP
MESSAGE directly. In Asterisk 22.12.0, `MessageSend` reports submission while
the phone's final response stays inside Asterisk. The bridge confirms a SIP
delivery when the phone returns a matching 2xx. It tries contacts marked
`Unreachable` last.

### Configuration

Copy `examples/sms-bridge.conf` to `/etc/sms-bridge/bridge.conf` and replace
the example values. Run one bridge instance per gateway. Create a dedicated
`sms-bridge` system user and group. Give it read access to config and
credentials (root-owned, group `sms-bridge`, mode 0640). Each credentials file
contains one `username:password` line. The AMI pair must match the account in
`examples/manager.conf`. Use trusted private networks for TG API and SIP UDP.

| Section | Setting | Default | Meaning |
|---|---|---|---|
| `[bridge]` | `ami_credentials_file` | required | Asterisk AMI credentials file. AMI always connects over IPv4 loopback. |
| | `ami_port` | `5038` | Local AMI TCP port. |
| | `sip_domain` | required | SIP domain or address phones use for replies, optionally with port. |
| | `sip_bind` | route-selected | Local IPv4 or IPv6 address used to send SIP UDP from an ephemeral port. |
| | `db_path` | `/var/lib/sms-bridge/queue.sqlite3` | SQLite queue path. The service user needs write access to its parent directory. |
| | `notify_hook` | `/usr/local/bin/pushover.sh` | Executable called with one message argument. Empty disables external notifications. |
| | `repeat_hours` | `6` | Interval for repeating TG API and AMI outage alerts. Zero disables repeats. A recovery alert follows a successful login. |
| | `retry_hours` | `6` | Retry window for SIP deliveries, failure replies and SMS waiting for an available gateway. |
| | `retry_seconds` | `60` | Delay between queue attempts. |
| | `multipart_seconds` | `120` | Time to wait for remaining inbound SMS parts before delivering with an incomplete marker. |
| | `api_timeout_seconds` | `10` | Timeout for API login and AMI requests. TCP keepalive detects idle connection loss. |
| | `send_status_seconds` | `120` | Time to wait for the first TG send status. |
| | `send_settle_seconds` | `30` | Quiet time after the last successful TG status before clearing the SMS. |
| | `sip_timeout_seconds` | `8` | Time to wait per UDP contact, including transaction retransmissions. |
| | `rate_per_hour` | `20` | Maximum admitted outbound SMS per endpoint per rolling hour. |
| | `rate_per_day` | `100` | Maximum admitted outbound SMS per endpoint per rolling day. |
| `[gateway ID]` | `host` | required | TG API host on the private network. |
| | `port` | `5038` | TG API TCP port. |
| | `credentials_file` | required | TG API credentials file. |
| | `country_code` | required | International prefix for local numbers, e.g. `+1`. |
| `[endpoint NAME]` | `gateway` | required | ID of the endpoint's TG gateway. |
| | `span` | required | TG API span, equal to the SIM slot plus one. Use each span once per gateway. |
| | `numbers` | required | Unique E.164 aliases separated by commas or spaces, primary first. |
| | `aor` | endpoint name | PJSIP AOR used to find the phone's contacts. |

Merge the example Asterisk snippets into the existing config. Set
`message_context=messages` only on the phones with `identify_by=username`,
inbound digest auth and an auth username matching the endpoint name. The
bridge relies on Asterisk authenticating the SIP From user against that
endpoint. It accepts IPv6 hosts in the From URI, including the unbracketed
form Asterisk emits. The dialplan uses Asterisk's `func_base64` and `app_userevent` modules to
encode the From header, destination and body and emit the AMI event. Set `message_context=messages-drop` on trunks and
other untrusted endpoints so MESSAGEs never reach a voice dialplan. Keep AMI
on loopback with the permissions shown in the example. AMI needs the
`reporting` write permission for `Getvar`.

Install the script at `/usr/local/lib/sms-bridge/sms_bridge.py` and install
`examples/sms-bridge.service` as `sms-bridge.service`. The unit creates the
private state directory. Use an executable notification hook accessible
inside the service sandbox if you enable it. Reload Asterisk's manager and
dialplan after merging the snippets, and apply the PJSIP endpoint settings.
Then enable and start the bridge unit.

### Queue and retries

Messages to each phone are delivered in arrival order, with up to 16 deliveries
running at once across phones. A message awaiting retry holds back later messages
until it is delivered or marked failed.
When a pending SIP delivery is due and its endpoint is no longer configured,
the bridge marks the message failed or discards it if it is a failure reply.
It logs either outcome once.

If an AMI or TG API event handler raises an error, including a database
error, the bridge logs the failure without message text and continues reading
events on the same connection.

A SIP delivery that reaches its retry deadline stays in the private queue and
generates one alert. This includes failure replies, which get a fresh window
when created. List and requeue failed deliveries as the service user or root.
Each manual retry starts a fresh window.

```sh
sudo -u sms-bridge /usr/local/lib/sms-bridge/sms_bridge.py list
sudo -u sms-bridge /usr/local/lib/sms-bridge/sms_bridge.py retry 42
sudo -u sms-bridge /usr/local/lib/sms-bridge/sms_bridge.py retry
```

Numbers can begin with `+`, `00`, or a national leading zero. The bridge strips
spaces. Incoming texts from alphanumeric senders and short codes show their
sender ID as the display name. The bridge refuses replies to those IDs. Avoid reusing their identifiers as
local extension names. The rolling rate limit counts admitted outbound SMS,
including queued or failed sends, and excludes local delivery.

The bridge sends each SMS once. Any failed part triggers a reply from the
original destination. A missing first status produces an uncertain-outcome
reply. After a successful status the gateway can send the next SMS while
the bridge waits for any further parts' statuses.

The bridge URL-encodes outgoing text. It accepts tabs and line breaks and
normalises CRLF and lone CR to LF. It refuses other control characters.
It encodes supplementary characters such as emoji as CESU-8 surrogate pairs
for the gateway. Very long encoded SMS still need live testing to establish
the gateway's limit.

Incoming multipart SMS is assembled in part order. A leading encoded BOM
marks UTF-8. Without it, the bridge decodes the text as ISO-8859-15.
GSM-7 Greek capitals remain untested. The gateway drops emoji from incoming
SMS. If no text remains, the bridge delivers a placeholder explaining the loss.

The queue stores no delivered message history. Logs and alerts include routing
metadata and omit message text. The API and AMI only deliver events while
connected, so events emitted during an outage may be lost. Multipart SMS
without a gateway reference can be ambiguous when two messages from the same
sender arrive together.

Run the local tests against loopback mocks with
`python3 -m unittest discover -s tests -v`.
