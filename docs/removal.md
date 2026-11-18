# Removal

1. Go to Settings → Devices & services → Solis.
2. Open the entry's menu and choose Delete.

That removes the config entry, its devices and its entities, the saved
snapshot, and any repairs the entry had open. The SolisCloud API key is not
revoked. Revoke it in SolisCloud (Account → Basic settings → API
management) if this Home Assistant instance should stop reading the account.
A local stick keeps running as before.

To uninstall the custom component, remove it in HACS or delete
`config/custom_components/solis`, then restart. Delete the config entry
first, or Home Assistant will report a missing integration.
