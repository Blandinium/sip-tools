# SIP-tools
Some Asterisk-related scripts

## update-asterisk.sh
Will install or update Asterisk 22 from source. Previous build folders are kept, to allow easy rollback.

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

