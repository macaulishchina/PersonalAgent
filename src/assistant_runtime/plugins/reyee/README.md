# Reyee observations

The protocol and client-state comparison live here. Instance lifecycle and configuration
use [observation sources](../observations/README.md). Install the `reyee` extra.

The router adapter encrypts login using the passphrase in its login page (OpenSSL
salted AES-256-CBC/MD5 derivation required by this firmware). Only auth and the fixed
client read endpoint exist in the driver. An expired/malformed response invalidates
the session and causes bounded exponential backoff, with at most one login per
sampling attempt. HTTP is appropriate only on the user's trusted LAN. No diagnostic
logging includes query strings or response bodies. Unknown optional client fields
are ignored; unknown identity/link fields and totals mismatches reject the snapshot.
MAC is source-local identity. IP, wired/wireless, AP serial, SSID and band are compared;
signal/traffic counters do not produce changes. Disappearance needs two complete
successful snapshots. Failures reset the comparison baseline. Raw router online time
is retained only as evidence, never used as a trusted occurrence timestamp.
