# WADMP GPS Updater

## What This Application Does

This console application reads LTE cell information stored in WADMP for a device identified by MAC address, queries the OpenCell / Unwired Labs API for approximate coordinates, and writes the resulting latitude, longitude, and altitude back to WADMP.

The script supports both interactive mode and a `--mac` command line argument. It validates input, handles common network and API failures, and writes technical diagnostics to `app.log`.

## File Structure

```text
.
|-- wadmp_gps_updater.py
|-- config.env.example
|-- requirements.txt
|-- README.md
```

## Prerequisites

- Windows with Python 3.11 or newer installed
- Network access to the WADMP environment
- A valid WADMP username and password
- A valid OpenCell / Unwired Labs token

## Installation

1. Open a Windows console in the project directory.
2. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Configuration

1. Copy `config.env.example` to `config.env`.
2. Edit `config.env` and fill in all required values.

### Required Configuration Values

- `OPENCELL_TOKEN`: OpenCell / Unwired Labs API token
- `DMP_USERNAME`: WADMP login name
- `DMP_PASSWORD`: WADMP login password
- `OPENCELL_API_URL`: OpenCell API URL, for example `https://eu1.unwiredlabs.com/v2/process.php`
- `DMP_TOKEN_URL`: OAuth2 token endpoint URL, for example `https://gateway.wadmp3.com/public/auth/connect/token`
- `DMP_API_BASE_URL`: WADMP API base URL, for example `https://gateway.wadmp3.com/api`
- `DMP_PLMN_FIELD`: Monitoring field name that stores the PLMN value, for example `MoPlmn`
- `DMP_CELL_FIELD`: Monitoring field name that stores the cell identifier, for example `MoCell`
- `DMP_GPS_LAT_FIELD`: GPS latitude field name, for example `GpsLat`
- `DMP_GPS_LON_FIELD`: GPS longitude field name, for example `GpsLon`
- `DMP_GPS_ALT_FIELD`: GPS altitude field name, for example `GpsAlt`

### Optional Configuration Values

- `DMP_MAC`: Default MAC address if `--mac` is not supplied
- `DMP_MNC_LENGTH`: Set to `2` or `3` if the PLMN format in your network requires explicit MNC length
- `REQUEST_TIMEOUT_SECONDS`: HTTP timeout in seconds. Default: `20`
- `VERIFY_TLS`: `true` or `false`. Default: `true`

## Running the Script

Interactive mode:

```powershell
python wadmp_gps_updater.py
```

Direct MAC address mode:

```powershell
python wadmp_gps_updater.py --mac AA:BB:CC:DD:EE:FF
```

Verbose mode:

```powershell
python wadmp_gps_updater.py --mac AA:BB:CC:DD:EE:FF --verbose
```

Custom configuration file:

```powershell
python wadmp_gps_updater.py --config custom-config.env
```

## Accepted MAC Address Formats

The script accepts these formats:

- `AA:BB:CC:DD:EE:FF`
- `AA-BB-CC-DD-EE-FF`
- `AABBCCDDEEFF`

The MAC address is normalized internally before use.

## Console Flow

Typical output:

- `Authenticating to WADMP...`
- `Reading device data from WADMP...`
- `Device found.`
- `Reading cellular fields...`
- `Querying OpenCell API...`
- `Updating WADMP GPS fields...`
- `Update completed successfully.`

## Logging

The script writes logs to `app.log`.

The log includes:

- Startup
- Entered MAC address
- Authentication success or failure
- Cellular field values read from WADMP
- OpenCell request summary
- OpenCell response summary
- WADMP update summary
- Errors and stack traces

Sensitive values such as passwords and full bearer tokens are not written to the log.

## Data Handling Notes

- Cell ID can be stored as a decimal or hexadecimal string.
- Values beginning with `0x` are treated as hexadecimal.
- Values containing hexadecimal letters `A-F` are also treated as hexadecimal.
- The script reads `PLMN` and splits it into `MCC` and `MNC`.
- If `DMP_MNC_LENGTH` is not set, a 5-digit PLMN is interpreted as `3+2` and a 6-digit PLMN as `3+3`.
- GPS altitude is always written as numeric `0`.
- The OpenCell lookup is best-effort because the current WADMP design does not provide TAC.

## Typical Errors and Troubleshooting

### `Configuration file not found`

Create `config.env` from `config.env.example` and verify the file path.

### `Invalid configuration line`

Check `config.env` for invalid lines. Each line must use `KEY=value` format.

### `Invalid MAC address`

Use one of the accepted MAC formats and make sure it contains exactly 12 hexadecimal characters.

### `WADMP authentication failed`

Verify:

- Username and password
- Token URL
- Network access to WADMP
- TLS settings if your environment uses a private certificate

### `No device found for the specified MAC address`

Confirm that:

- The MAC address belongs to a device in WADMP
- The monitoring endpoint is reachable
- The configured `DMP_PLMN_FIELD` and `DMP_CELL_FIELD` exist for the device

### `Required WADMP field is missing` or `Required WADMP field is empty`

Make sure the configured field names match the exact names used in WADMP and that the device record already contains the PLMN and Cell ID values.

### `OpenCell lookup returned no coordinates`

Possible causes:

- No match for the supplied LTE data
- Invalid PLMN or Cell ID values
- Incorrect MCC or MNC split
- API token issue or request quota problem

### `WADMP GPS update failed`

Check:

- User permissions in WADMP
- Whether the configured GPS fields are writable

### Timeout or connection errors

Verify DNS resolution, proxy settings, firewall rules, VPN access, and whether the remote endpoints are reachable from the machine running the script.

## Exit Codes

- `0`: Success
- `1`: Application or runtime error
- `130`: Cancelled by user

## Notes About WADMP API Paths

The script uses:

- Device lookup path: `/monitoring/devices/{macAddress}`
- GPS update path: `/management/devices/{macAddress}`

The lookup reads the configured monitoring fields such as `MoPlmn` and `MoCell`. The update writes `GpsLat`, `GpsLon`, and `GpsAlt` directly to the device management endpoint.
