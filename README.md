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
