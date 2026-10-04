# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use
**Report a vulnerability** on this repository's Security tab (GitHub private
vulnerability reporting). Include what you found, how to reproduce it, and the
versions of fusion-electronics-mcp and Fusion you used.

We aim to reply within a week and to fix confirmed problems in the next release.

## What the server can reach

- The server talks to Fusion through the bundled add-in on 127.0.0.1, behind a
  per-session token stored in your user folder. The add-in runs only design
  commands from a fixed list (`ALLOWED_VERBS` in the add-in); it has no command
  that runs scripts, ULPs or programs.
- The only internet access is `check_jlc_orientation` with `fetch=true`, which
  downloads footprints from easyeda.com.
- MCP tools act with your permissions in Fusion: an agent connected to this
  server can edit any design you have open. Connect agents you trust, and review
  changes before you save.
