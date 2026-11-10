# Removal

Settings → Devices & services → Solis → the entry → delete.

That removes the config entry, its devices and its entities. Recorder history
for those entities is deleted with them. Poll-failure repairs for that entry
are removed on unload. The SolisCloud API key is not revoked. Revoke it in
SolisCloud if this Home Assistant instance should stop reading the account.

To uninstall the custom component, remove it in HACS or delete
`config/custom_components/solis`, then restart. Delete the config entry
first, or Home Assistant will report a missing integration.
