# BF4_BOT Project Instructions

## Fridge interactions are strictly READ-ONLY
This project monitors a Bluefors dilution refrigerator; the fridge PC is the
machine being worked on. ALL interactions with the fridge -- log files, the
REST API, any device or control channel -- must be read-only. Never write,
send commands, or change fridge state. This is critical lab infrastructure.
