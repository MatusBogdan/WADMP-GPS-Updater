# WADMP GPS Updater

## What This Application Does

This console application is designed for bulk processing of WADMP routers. It reads LTE cell information for all online routers in a company, queries a configurable location provider API for approximate coordinates, and writes the resulting latitude, longitude, and altitude back to WADMP through a CSV bulk update.

The script validates input, handles common network and API failures, and writes technical diagnostics to `app.log`.

## File Structure

```text
.
|-- wadmp_gps_updater.py
|-- config.env.example
|-- requirements.txt
|-- README.md
|-- deploy/
|   |-- wadmp-gps-updater.service
|   |-- wadmp-gps-updater.timer
```

Runtime-generated items created during execution:

```text
output/
location_cache.json
app.log
```

## Prerequisites

- Windows or Linux with Python 3.11 or newer installed
- Network access to the WADMP environment
- A valid WADMP username and password
- A valid location API credential for the selected provider
- A valid WADMP company ID

## Installation

1. Open a terminal in the project directory.
2. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Linux example:

```bash
python3 -m pip install -r requirements.txt
```

## Configuration

1. Copy `config.env.example` to `config.env`.
2. Edit `config.env` and fill in all required values.

### Required User-Editable Values

- `LOCATION_PROVIDER`: Supported values are `opencell` and `here`
- `DMP_USERNAME`: WADMP login name
- `DMP_PASSWORD`: WADMP login password
- `DMP_COMPANY_ID`: Company ID used for bulk reading online devices and CSV upload
- `OUTPUT_RETENTION_DAYS`: Delete generated CSV files in `output` older than this number of days. Default: `14`
- `LOG_RETENTION_DAYS`: Delete `app.log` if it is older than this number of days. Allowed range: `1` to `90`. Default: `14`
- `RUN_INTERVAL_HOURS`: `0` runs the batch once. A positive integer repeats the batch every N hours until the process is stopped. Allowed range: `0` to `720`. Default: `0`

### Provider-Specific Values

If `LOCATION_PROVIDER="opencell"`:

- `OPENCELL_TOKEN`: OpenCell / Unwired Labs API token
- `OPENCELL_API_URL`: OpenCell API URL

If `LOCATION_PROVIDER="here"`:

- `HERE_API_KEY`: HERE Positioning API key
- `HERE_API_URL`: HERE Positioning API locate endpoint. Default: `https://pos.ls.hereapi.com/positioning/v1/locate`
- `HERE_FALLBACK`: HERE cell fallback mode. Default: `area`

### Usually Do Not Change

- `DMP_PLMN_FIELD`: Monitoring field name storing PLMN. Default: `MoPlmn`
- `DMP_CELL_FIELD`: Monitoring field name storing cell ID. Default: `MoCell`
- `DMP_MAC_ADDRESS_FIELD`: Device MAC field name for batch read. Default: `MacAddress`
- `DMP_GPS_LAT_FIELD`: GPS latitude field name. Default: `GpsLat`
- `DMP_GPS_LON_FIELD`: GPS longitude field name. Default: `GpsLon`
- `DMP_GPS_ALT_FIELD`: GPS altitude field name. Default: `GpsAlt`
- `DMP_ONLINE_FIELD`: Device online status field name. Default: `Online`
- `DMP_ONLINE_VALUE`: Value used to identify online devices. Default: `1`
- `DMP_MNC_LENGTH`: Set to `2` or `3` if PLMN parsing requires explicit MNC length
- `DMP_BATCH_PAGE_SIZE`: Number of devices per page in batch mode. Default: `100`
- `LOCATION_CACHE_PATH`: Path to the persistent location cache JSON file. Default: `location_cache.json`
- `DMP_LONG_OPERATION_TIMEOUT_SECONDS`: Maximum wait time for long operation completion. Default: `120`

### Do Not Change Unless Explicitly Instructed

- `DMP_TOKEN_URL`: OAuth2 token endpoint URL
- `DMP_API_BASE_URL`: WADMP API base URL
- `DMP_LONG_OPERATION_PATH`: Long operation detail endpoint path. Default: `/long-operations/{operation_id}`
- `DMP_LONG_OPERATION_POLL_SECONDS`: Poll interval for long operation status checks. Default: `2`
- `REQUEST_TIMEOUT_SECONDS`: HTTP timeout in seconds. Default: `20`
- `VERIFY_TLS`: `true` or `false`. Default: `true`

## Running the Script

Standard one-time batch mode:

```powershell
python wadmp_gps_updater.py
```

Linux example:

```bash
python3 wadmp_gps_updater.py
```

Repeated batch mode every 2 hours:

```env
RUN_INTERVAL_HOURS="2"
```

The process will keep running until it is stopped with `Ctrl+C`.

For Linux production deployment, prefer the `systemd` service and timer described below and keep `RUN_INTERVAL_HOURS="0"`.

Verbose mode:

```powershell
python wadmp_gps_updater.py --verbose
```

Custom configuration file:

```powershell
python wadmp_gps_updater.py --config custom-config.env --verbose
```

## Confirmed WADMP API Flow

Batch read:

- `POST /monitoring/devices/companies/{companyId}`

Batch CSV write:

- `POST /management/devices/long-operations/fields/csv`

## Linux Service Setup

For Linux, the recommended approach is:

- keep `RUN_INTERVAL_HOURS="0"` in `config.env`
- run one batch per execution
- let `systemd` trigger the script on schedule

This is more reliable than keeping one long-running terminal process alive.

### 1. Prepare the application directory

Example target directory:

```bash
sudo mkdir -p /opt/wadmp-gps-updater
sudo cp wadmp_gps_updater.py requirements.txt config.env /opt/wadmp-gps-updater/
sudo cp -r deploy /opt/wadmp-gps-updater/
cd /opt/wadmp-gps-updater
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p output
```

### 2. Create a dedicated service user

```bash
sudo useradd --system --home /opt/wadmp-gps-updater --shell /usr/sbin/nologin wadmpgps
sudo chown -R wadmpgps:wadmpgps /opt/wadmp-gps-updater
```

### 3. Install the `systemd` unit files

The repository contains ready-made files:

- `deploy/wadmp-gps-updater.service`
- `deploy/wadmp-gps-updater.timer`

Copy them to `systemd`:

```bash
sudo cp /opt/wadmp-gps-updater/deploy/wadmp-gps-updater.service /etc/systemd/system/
sudo cp /opt/wadmp-gps-updater/deploy/wadmp-gps-updater.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### 4. Adjust the timer interval if needed

Default timer interval is every 2 hours:

```ini
OnUnitActiveSec=2h
```

If you want a different Linux service schedule, edit:

- `/etc/systemd/system/wadmp-gps-updater.timer`

Then reload:

```bash
sudo systemctl daemon-reload
```

### 5. Enable and start the timer

```bash
sudo systemctl enable --now wadmp-gps-updater.timer
```

### 6. Check status and logs

```bash
sudo systemctl status wadmp-gps-updater.timer
sudo systemctl status wadmp-gps-updater.service
journalctl -u wadmp-gps-updater.service -n 100 --no-pager
```

### 7. Run one manual test

```bash
sudo systemctl start wadmp-gps-updater.service
sudo systemctl status wadmp-gps-updater.service
```

### Notes

- The Linux `systemd` timer is the preferred production mode.
- The internal `RUN_INTERVAL_HOURS` loop is still available, but it is better suited for manual terminal runs.
- If you use the Linux `systemd` timer, do not also use a repeating `RUN_INTERVAL_HOURS` value.
- If you change the application path, user, or virtualenv path, update the service file accordingly.

## Console Flow

- `Authenticating to WADMP...`
- `Reading online devices from WADMP...`
- `Loaded N online devices.`
- `Querying OPENCELL API for online devices...`
- `Querying HERE API for online devices...`
- `Prepared N GPS updates. Uploading CSV to WADMP...`
- `CSV accepted by WADMP. Waiting for long operation ID...`
- `Batch update completed successfully.`
- `Batch cycle failed: ...`
- `Next resync will run in N hour(s) at YYYY-MM-DD HH:MM:SS. Press Ctrl+C to stop.`

## Logging

The script writes logs to `app.log`.
It also saves every generated batch CSV file to the `output` directory before upload.
CSV files in `output` older than the configured `OUTPUT_RETENTION_DAYS` value are deleted automatically at startup.
The main `app.log` file is also deleted automatically at startup if it is older than the configured `LOG_RETENTION_DAYS` value.
The upload uses that saved CSV file directly through the Python `requests` multipart upload, following the same simple file-post pattern used by the WADMP developers.
The generated CSV uses `;` as the delimiter because that is required by the WADMP CSV import endpoint.

The log includes:

- Startup
- Authentication success or failure
- Batch page loading summaries
- Location provider request summaries
- Location cache load/save summaries
- Output cleanup summaries
- Saved CSV file path
- CSV upload summary
- Long operation polling summaries
- Scheduled next-run summaries
- Scheduled retry summaries after failed cycles
- Errors and stack traces

Sensitive values such as passwords and full bearer tokens are not written to the log.

## Data Handling Notes

- Cell ID can be stored as a decimal or hexadecimal string.
- Values beginning with `0x` are treated as hexadecimal.
- Values containing hexadecimal letters `A-F` are also treated as hexadecimal.
- The script reads PLMN and splits it into MCC and MNC.
- If `DMP_MNC_LENGTH` is not set, a 5-digit PLMN is interpreted as `3+2` and a 6-digit PLMN as `3+3`.
- GPS altitude is always written as numeric `0`.
- Devices with missing or invalid `MoPlmn`, `MoCell`, or `MacAddress` are skipped.
- Location results are cached by `MCC + MNC + Cell ID`, so repeated BTS lookups are reused across devices and across runs.
- The lookup is best-effort because TAC is not available in WADMP.
- If `RUN_INTERVAL_HOURS` is greater than `0`, a failed cycle is logged and the application continues with the next scheduled run instead of exiting immediately.

## Typical Errors and Troubleshooting

### `Configuration file not found`

Create `config.env` from `config.env.example` and verify the file path.

### `Invalid configuration line`

Check `config.env` for invalid lines. Each line must use `KEY=value` format.

### `No online devices were returned by WADMP`

Verify:

- The company ID is correct
- The `DMP_ONLINE_FIELD` and `DMP_ONLINE_VALUE` match your WADMP environment
- The account can access the company devices

### `No GPS updates were generated for online devices`

Possible causes:

- Online devices are missing `MoPlmn` or `MoCell`
- PLMN parsing failed
- The selected location provider returned no match for all devices

### `WADMP CSV GPS update failed`

Check:

- The company ID
- The CSV target field names
- User permissions in WADMP
- Whether the configured GPS fields are writable

### Timeout or connection errors

Verify DNS resolution, proxy settings, firewall rules, VPN access, and whether the remote endpoints are reachable from the machine running the script.

## Exit Codes

- `0`: Success
- `1`: Application or runtime error
- `130`: Cancelled by user 
