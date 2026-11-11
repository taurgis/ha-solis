# Troubleshooting

**Invalid authentication.** The key id or secret was rejected, or the
signature did not match. Create a new key in SolisCloud and use
reauthentication. A portal password is not accepted. The log does not contain
the secret. Diagnostics redact the key id, the secret and the config-entry
unique id, which is the key id.

**Clock.** SolisCloud rejects requests whose `Date` header is more than 15
minutes from UTC. If the response includes a Date header, the client signs
the next attempt with that clock. If it still fails, set the Home Assistant
host clock. The repair is `clock_skew`.

**No plants.** The key can sign requests but the account returned no
stations. A plant without an id looks the same. Rename the plant in
SolisCloud so an id is generated, which is the same workaround the upstream
README documents.

**Abnormal data.** SolisCloud answered with code `1`. That is an account-side
fault. Disabling and recreating the API key, or a Solis ticket, is what
clears it.

**SolisCloud is unreachable.** The repair `cloud_outage` opens after three
failed polls, or immediately when the last good snapshot is already more than
15 minutes old. Live readings are hidden. Energy totals stay put. The repair
clears itself on the next successful poll. A single blip does not open it and
does not start reauthentication. An HTML 403 from a gateway is this case, not
a bad key.

**Power unavailable while energy is still there.** The inverter's
`dataTimestamp` is older than 15 minutes, or that inverter's detail call
failed while the rest of the account was readable. Energy totals keep the
last value. Status reads `stale`. A restart during an outage loads those
totals again from the last snapshot and from RestoreSensor.

**Power is 0.** The inverter state is offline and the sample is fresh.

**Energy dropped to 0 in the middle of the day.** A null or zero sample is
held at the last good value for that period. If the number still falls, the
cloud sent a lower reading after the plant-local period rolled. In British
Summer Time that used to show up around 23:00 when the plant offset stayed on
winter time (hultenvp/solis-sensor issue 533). Leave the plant timezone
empty so Home Assistant's zone is used when it is within an hour of the
Solis offset, or set `Europe/London` (or your zone) explicitly. `plant`
forces the numeric offset.

**Two Solis integrations.** `hultenvp/solis-sensor` and this rewrite share the
`solis` folder. Only one can be installed. Remove the other and restart.

**Duplicate entities after migration.** Reload the entry. Migration runs on
every poll and keeps the legacy entity id when both the old and the new
unique id exist. An inverter that was offline at the first restart is
migrated when it next appears.
