# SIP-tools
Some Asterisk-related scripts

## update-asterisk.sh
Will install or update Asterisk 22 from source. Previous build folders are kept, to allow easy rollback.

## tg200_sim_status.sh
The SIM cards in my Yeastar TG200 often lose their connection and don't seem to automatically recover. This script will check the SIM status in the web interface. If a SIM remains in an error state for at least 2 script runs, and other SIMs are either in error or IDLE (no call is ongoing), the TG200 will be rebooted (Ideally, I could reboot only the offending module. But in my experience, that does not resolve the error, while a full system reboot does)

