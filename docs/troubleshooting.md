# Troubleshooting

**Invalid authentication.** The key id or secret was rejected, or the
signature did not match. Create a new key in SolisCloud and use
reauthentication. A portal password is not accepted.

**Clock.** SolisCloud rejects requests whose `Date` header is more than 15
minutes from UTC. Set the Home Assistant host clock. The repair is
`clock_skew`.

**No plants.** The key can sign requests but the account returned no
stations. A plant without an id looks the same. Rename the plant in
SolisCloud so an id is generated, which is the same workaround the upstream
README documents.

**Abnormal data.** SolisCloud answered with code `1`. That is an account-side
fault. Disabling and recreating the API key, or a Solis ticket, is what
clears it.

**Entities unavailable.** The last poll failed (timeout or HTTP 5xx). They
return when a poll succeeds. A single failed `inverterList` does not delete
inverters that were already known.

**Power unavailable while energy is still there.** The inverter's
`dataTimestamp` is older than 15 minutes. Energy totals keep the last value.
Status reads `stale`.

**Power is 0.** The inverter state is offline and the sample is fresh.
