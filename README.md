# SIP-tools
Some Asterisk-related scripts

## update-asterisk.sh
Will install or update Asterisk 22 from source. Previous build folders are kept, to allow easy rollback.

## tg200_sim_status.sh
A work in progress. The SIM cards in my Yeastar TG200 often lose their connection and don't seem to automatically recover. This script will check the SIM status in the web interface. Currently, it just notifies me of status changes. I future, I'll have it reboot the TG200 when SIMs get stuck in the initialising state. 

