# Agent.md

## Project Goals
This project is to test how I can make GPT work under my guidance using my phone alone. In particular, the exercise will be to create some sort of homepage plugin that can read `docker ps -a` and automatically add to `services.yaml` the actual containers that are running. If Caddy is present, it will check for the `Caddyfile` and ask to add the proxy URLs. Configuration will also be included.

## Current Context
The configuration options will include either a dashboard refresh button for updates or an automatic regular update feature. Additionally, there will be configuration options to divide containers into groups using widgets, bookmarks, etc.

## Development Plan
### Stage 1
- Parse the container list and add it to `services.yaml` without overwriting existing entries.
 (The button option seemed to be more tricky than originally thought, so should be reported to later stages).

### Stage 2
- Search for the presence of Caddy and automatically grab associated URLs from the `Caddyfile` if it exists.

### Stage 3
- Expand configuration possibilities to include widgets for organizing containers and enable features for automatic updates.