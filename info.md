SolisCloud plants, inverters, alarms and export meters in Home Assistant.

Reads use an API key and secret. The integration does not send inverter
control commands. Polling follows the cloud's roughly five-minute sample
and stays under the platform rate limit.

Replacing `hultenvp/solis-sensor` keeps the `solis` domain. Uninstall that
copy before installing this one. Existing entity ids are migrated when the
old unique id is still in the registry.
