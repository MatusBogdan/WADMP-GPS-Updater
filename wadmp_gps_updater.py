import argparse
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests import Response, Session
from requests.exceptions import ConnectionError, RequestException, Timeout


DEFAULT_CONFIG_PATH = Path("config.env")
DEFAULT_LOG_PATH = Path("app.log")
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MONITORING_DEVICE_PATH = "/monitoring/devices/{mac_address}"
DEFAULT_UPDATE_DEVICE_PATH = "/management/devices/{mac_address}"
MAC_PATTERN = re.compile(r"^[0-9A-Fa-f]{12}$")


class AppError(Exception):
    """Raised for expected application failures."""


@dataclass(frozen=True)
class FieldNames:
    plmn: str
    cell_id: str
    gps_latitude: str
    gps_longitude: str
    gps_altitude: str


@dataclass(frozen=True)
class Config:
    dmp_username: str
    dmp_password: str
    opencell_token: str
    dmp_token_url: str
    dmp_api_base_url: str
    opencell_api_url: str
    fields: FieldNames
    mnc_length: int | None = None
    default_mac: str | None = None
    monitoring_device_path: str = DEFAULT_MONITORING_DEVICE_PATH
    update_device_path: str = DEFAULT_UPDATE_DEVICE_PATH
    request_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    verify_tls: bool = True


@dataclass(frozen=True)
class CellularData:
    cell_id: int
    mcc: str
    mnc: str


@dataclass(frozen=True)
class OpenCellResult:
    latitude: float
    longitude: float
    accuracy: float | None


@dataclass(frozen=True)
class DeviceRecord:
    fields: dict[str, Any]


def setup_logging(verbose: bool) -> logging.Logger:
    logger = logging.getLogger("wadmp_gps_updater")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    file_handler = logging.FileHandler(DEFAULT_LOG_PATH, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console_handler)

    logger.debug("Application startup.")
    return logger


def print_status(message: str) -> None:
    print(message, flush=True)


def load_config(config_path: Path) -> Config:
    if not config_path.exists():
        raise AppError(
            f"Configuration file not found: {config_path}. "
            "Create config.env from config.env.example."
        )

    try:
        raw = parse_env_config(config_path)
    except OSError as exc:
        raise AppError(f"Unable to read configuration file: {exc}") from exc

    required_keys = [
        "OPENCELL_TOKEN",
        "DMP_USERNAME",
        "DMP_PASSWORD",
        "OPENCELL_API_URL",
        "DMP_TOKEN_URL",
        "DMP_API_BASE_URL",
        "DMP_PLMN_FIELD",
        "DMP_CELL_FIELD",
        "DMP_GPS_LAT_FIELD",
        "DMP_GPS_LON_FIELD",
        "DMP_GPS_ALT_FIELD",
    ]
    for key in required_keys:
        if key not in raw:
            raise AppError(f"Missing configuration key: {key}")

    def read_text_key(key: str) -> str:
        value = raw.get(key, "").strip()
        if not value:
            raise AppError(f"Missing or invalid configuration value: {key}")
        return value

    timeout_value = parse_positive_int(
        raw.get("REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)),
        "REQUEST_TIMEOUT_SECONDS",
    )
    verify_tls = parse_bool(raw.get("VERIFY_TLS", "true"), "VERIFY_TLS")

    mnc_length_raw = raw.get("DMP_MNC_LENGTH", "").strip()
    mnc_length = None
    if mnc_length_raw:
        mnc_length = parse_positive_int(mnc_length_raw, "DMP_MNC_LENGTH")
        if mnc_length not in (2, 3):
            raise AppError("Configuration value 'DMP_MNC_LENGTH' must be 2 or 3.")

    default_mac = raw.get("DMP_MAC", "").strip() or None
    if default_mac is not None:
        default_mac = normalize_mac_address(default_mac)

    return Config(
        dmp_username=read_text_key("DMP_USERNAME"),
        dmp_password=read_text_key("DMP_PASSWORD"),
        opencell_token=read_text_key("OPENCELL_TOKEN"),
        dmp_token_url=read_text_key("DMP_TOKEN_URL"),
        dmp_api_base_url=read_text_key("DMP_API_BASE_URL").rstrip("/"),
        opencell_api_url=read_text_key("OPENCELL_API_URL"),
        fields=FieldNames(
            plmn=read_text_key("DMP_PLMN_FIELD"),
            cell_id=read_text_key("DMP_CELL_FIELD"),
            gps_latitude=read_text_key("DMP_GPS_LAT_FIELD"),
            gps_longitude=read_text_key("DMP_GPS_LON_FIELD"),
            gps_altitude=read_text_key("DMP_GPS_ALT_FIELD"),
        ),
        mnc_length=mnc_length,
        default_mac=default_mac,
        request_timeout_seconds=timeout_value,
        verify_tls=verify_tls,
    )


def parse_env_config(config_path: Path) -> dict[str, str]:
    config: dict[str, str] = {}
    lines = config_path.read_text(encoding="utf-8").splitlines()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AppError(
                f"Invalid configuration line {line_number}. Expected KEY=value format."
            )
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise AppError(f"Invalid configuration line {line_number}. Missing key name.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        config[key] = value
    return config


def parse_positive_int(raw_value: str, key_name: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise AppError(f"Configuration value '{key_name}' must be an integer.") from exc
    if value <= 0:
        raise AppError(f"Configuration value '{key_name}' must be a positive integer.")
    return value


def parse_bool(raw_value: str, key_name: str) -> bool:
    value = raw_value.strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    raise AppError(f"Configuration value '{key_name}' must be true or false.")


def normalize_mac_address(mac_address: str) -> str:
    compact = re.sub(r"[^0-9A-Fa-f]", "", mac_address or "")
    if not MAC_PATTERN.fullmatch(compact):
        raise AppError(
            "Invalid MAC address. Accepted formats: AA:BB:CC:DD:EE:FF, "
            "AA-BB-CC-DD-EE-FF, AABBCCDDEEFF."
        )
    compact = compact.upper()
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def read_mac_address(cli_mac: str | None, config: Config) -> str:
    if cli_mac:
        return normalize_mac_address(cli_mac)
    if config.default_mac:
        return config.default_mac
    return normalize_mac_address(input("Enter device MAC address: ").strip())


def mask_secret(value: str, keep_start: int = 4, keep_end: int = 2) -> str:
    if len(value) <= keep_start + keep_end:
        return "*" * len(value)
    hidden_length = len(value) - keep_start - keep_end
    return f"{value[:keep_start]}{'*' * hidden_length}{value[-keep_end:]}"


def ensure_success_json(response: Response, action: str) -> Any:
    try:
        response.raise_for_status()
    except RequestException as exc:
        detail = safe_response_text(response)
        raise AppError(f"{action} failed with HTTP {response.status_code}: {detail}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise AppError(f"{action} returned invalid JSON.") from exc


def safe_response_text(response: Response, limit: int = 300) -> str:
    text = response.text.strip()
    if not text:
        return "no response body"
    return text[:limit]


def perform_request(
    session: Session,
    method: str,
    url: str,
    *,
    timeout: int,
    logger: logging.Logger,
    action: str,
    verify_tls: bool,
    **kwargs: Any,
) -> Response:
    try:
        return session.request(method, url, timeout=timeout, verify=verify_tls, **kwargs)
    except Timeout as exc:
        logger.exception("%s timed out.", action)
        raise AppError(f"{action} timed out.") from exc
    except ConnectionError as exc:
        logger.exception("%s connection failed.", action)
        raise AppError(f"{action} failed because the remote service is unreachable.") from exc
    except RequestException as exc:
        logger.exception("%s request error.", action)
        raise AppError(f"{action} failed due to an HTTP request error.") from exc


def authenticate_wadmp(session: Session, config: Config, logger: logging.Logger) -> str:
    payload = {
        "grant_type": "password",
        "username": config.dmp_username,
        "password": config.dmp_password,
        "client_id": "python",
    }
    logger.info(
        "Authenticating to WADMP. token_url=%s username=%s",
        config.dmp_token_url,
        mask_secret(config.dmp_username),
    )

    response = perform_request(
        session,
        "POST",
        config.dmp_token_url,
        timeout=config.request_timeout_seconds,
        logger=logger,
        action="WADMP authentication",
        verify_tls=config.verify_tls,
        data=payload,
    )
    data = ensure_success_json(response, "WADMP authentication")

    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise AppError("WADMP authentication succeeded but no access token was returned.")

    logger.info("WADMP authentication succeeded. token=%s", mask_secret(access_token))
    return access_token


def build_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def get_device_by_mac(
    session: Session,
    config: Config,
    access_token: str,
    mac_address: str,
    logger: logging.Logger,
) -> DeviceRecord:
    encoded_mac = quote(mac_address, safe="")
    url = build_url(
        config.dmp_api_base_url,
        config.monitoring_device_path.format(mac_address=encoded_mac),
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    params = [("fields", config.fields.plmn), ("fields", config.fields.cell_id)]

    logger.info("Looking up device by MAC. url=%s mac=%s", url, mac_address)
    response = perform_request(
        session,
        "GET",
        url,
        timeout=config.request_timeout_seconds,
        logger=logger,
        action="WADMP device lookup",
        verify_tls=config.verify_tls,
        headers=headers,
        params=params,
    )
    data = ensure_success_json(response, "WADMP device lookup")

    if data.get("success") is False:
        message = data.get("message") or "lookup was rejected by WADMP"
        raise AppError(f"WADMP device lookup failed: {message}")

    device_payload = extract_device_payload(data)
    fields = extract_fields_map(device_payload)

    logger.info("Device lookup succeeded. field_count=%s", len(fields))
    return DeviceRecord(fields=fields)


def extract_device_payload(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], dict):
            return data["data"]
        if "data" in data and isinstance(data["data"], list):
            if not data["data"]:
                raise AppError("No device found for the specified MAC address.")
            first_item = data["data"][0]
            if isinstance(first_item, dict):
                return first_item
        if isinstance(data, dict):
            return data
    raise AppError("WADMP device lookup returned an unexpected result format.")


def extract_fields_map(device_payload: dict[str, Any]) -> dict[str, Any]:
    scalar_fields: dict[str, Any] = {}
    for key, value in device_payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            scalar_fields[key] = value
    if scalar_fields:
        return scalar_fields
    raise AppError("Unable to extract device fields from WADMP response.")


def get_required_field(fields: dict[str, Any], field_name: str) -> str:
    if field_name not in fields:
        raise AppError(f"Required WADMP field is missing: {field_name}")

    value = fields[field_name]
    if value is None:
        raise AppError(f"Required WADMP field is empty: {field_name}")

    text = str(value).strip()
    if not text:
        raise AppError(f"Required WADMP field is empty: {field_name}")
    return text


def parse_integer_field(raw_value: str, field_name: str) -> int:
    candidate = raw_value.strip()
    if not candidate:
        raise AppError(f"Field '{field_name}' is empty.")

    is_hex = False
    if candidate.lower().startswith("0x"):
        candidate = candidate[2:]
        is_hex = True
    elif re.search(r"[A-Fa-f]", candidate):
        is_hex = True

    base = 16 if is_hex else 10
    try:
        return int(candidate, base)
    except ValueError as exc:
        raise AppError(
            f"Field '{field_name}' contains an invalid numeric value: {raw_value}"
        ) from exc


def parse_mcc_mnc(raw_value: str, field_name: str) -> str:
    value = raw_value.strip()
    if not value.isdigit():
        raise AppError(f"Field '{field_name}' must contain digits only.")
    if not 2 <= len(value) <= 3:
        raise AppError(f"Field '{field_name}' must contain 2 or 3 digits.")
    return value


def parse_plmn(raw_value: str, mnc_length: int | None) -> tuple[str, str]:
    value = raw_value.strip()
    if not value.isdigit():
        raise AppError("PLMN field must contain digits only.")
    if len(value) not in (5, 6):
        raise AppError("PLMN field must contain 5 or 6 digits.")

    mcc = value[:3]
    inferred_mnc_length = mnc_length if mnc_length is not None else (2 if len(value) == 5 else 3)
    mnc = value[3:]
    if len(mnc) != inferred_mnc_length:
        raise AppError("PLMN value length does not match the configured DMP_MNC_LENGTH.")

    return parse_mcc_mnc(mcc, "MCC"), parse_mcc_mnc(mnc, "MNC")


def extract_cellular_data(fields: dict[str, Any], config: Config, logger: logging.Logger) -> CellularData:
    raw_cell_id = get_required_field(fields, config.fields.cell_id)
    raw_plmn = get_required_field(fields, config.fields.plmn)
    mcc, mnc = parse_plmn(raw_plmn, config.mnc_length)

    cellular_data = CellularData(
        cell_id=parse_integer_field(raw_cell_id, config.fields.cell_id),
        mcc=mcc,
        mnc=mnc,
    )

    logger.info(
        "Read cellular fields. plmn_raw=%s cell_id_raw=%s mcc=%s mnc=%s parsed_cell_id=%s",
        raw_plmn,
        raw_cell_id,
        cellular_data.mcc,
        cellular_data.mnc,
        cellular_data.cell_id,
    )
    return cellular_data


def query_opencell(
    session: Session,
    config: Config,
    cellular_data: CellularData,
    logger: logging.Logger,
) -> OpenCellResult:
    payload = {
        "token": config.opencell_token,
        "radio": "lte",
        "mcc": cellular_data.mcc,
        "mnc": cellular_data.mnc,
        "cells": [{"cid": cellular_data.cell_id}],
        "address": 0,
    }

    logger.info(
        "Querying OpenCell API. url=%s token=%s mcc=%s mnc=%s cell_id=%s",
        config.opencell_api_url,
        mask_secret(config.opencell_token),
        cellular_data.mcc,
        cellular_data.mnc,
        cellular_data.cell_id,
    )
    response = perform_request(
        session,
        "POST",
        config.opencell_api_url,
        timeout=config.request_timeout_seconds,
        logger=logger,
        action="OpenCell lookup",
        verify_tls=config.verify_tls,
        json=payload,
    )
    data = ensure_success_json(response, "OpenCell lookup")

    status = data.get("status")
    if status and status != "ok":
        message = data.get("message") or data.get("balance") or "unknown API error"
        raise AppError(f"OpenCell lookup failed: {message}")

    latitude = data.get("lat")
    longitude = data.get("lon")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        raise AppError("OpenCell lookup returned no coordinates for the supplied cell data.")

    accuracy = data.get("accuracy")
    accuracy_value = float(accuracy) if isinstance(accuracy, (int, float)) else None

    logger.info(
        "OpenCell lookup succeeded. lat=%s lon=%s accuracy=%s",
        latitude,
        longitude,
        accuracy_value,
    )
    return OpenCellResult(
        latitude=float(latitude),
        longitude=float(longitude),
        accuracy=accuracy_value,
    )


def update_gps_fields(
    session: Session,
    config: Config,
    access_token: str,
    mac_address: str,
    result: OpenCellResult,
    logger: logging.Logger,
) -> None:
    encoded_mac = quote(mac_address, safe="")
    url = build_url(
        config.dmp_api_base_url,
        config.update_device_path.format(mac_address=encoded_mac),
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "data": [
            {"field_name": config.fields.gps_latitude, "value": result.latitude},
            {"field_name": config.fields.gps_longitude, "value": result.longitude},
            {"field_name": config.fields.gps_altitude, "value": 0},
        ]
    }

    logger.info(
        "Updating WADMP GPS fields. url=%s mac=%s payload=%s",
        url,
        mac_address,
        payload,
    )
    response = perform_request(
        session,
        "POST",
        url,
        timeout=config.request_timeout_seconds,
        logger=logger,
        action="WADMP GPS update",
        verify_tls=config.verify_tls,
        headers=headers,
        json=payload,
    )
    if response.status_code not in (200, 201, 202, 204):
        detail = safe_response_text(response)
        raise AppError(f"WADMP GPS update failed with HTTP {response.status_code}: {detail}")

    if response.content:
        data = ensure_success_json(response, "WADMP GPS update")
        if isinstance(data, dict) and data.get("success") is False:
            message = data.get("message") or "unknown update error"
            raise AppError(f"WADMP GPS update failed: {message}")

    logger.info(
        "WADMP GPS update succeeded. mac=%s lat=%s lon=%s accuracy=%s",
        mac_address,
        result.latitude,
        result.longitude,
        result.accuracy,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update WADMP GPS fields using LTE cell data and OpenCell lookup."
    )
    parser.add_argument(
        "--mac",
        help="Device MAC address. If omitted, the script uses DMP_MAC or prompts interactively.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the configuration file. Default: config.env",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable more detailed console logging.",
    )
    return parser


def run() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    logger = setup_logging(verbose=args.verbose)

    try:
        config = load_config(Path(args.config))
        mac_address = read_mac_address(args.mac, config)
        logger.info("Using MAC address: %s", mac_address)

        with requests.Session() as session:
            print_status("Authenticating to WADMP...")
            access_token = authenticate_wadmp(session, config, logger)

            print_status("Reading device data from WADMP...")
            device = get_device_by_mac(session, config, access_token, mac_address, logger)
            print_status("Device found.")

            print_status("Reading cellular fields...")
            cellular_data = extract_cellular_data(device.fields, config, logger)

            print_status("Querying OpenCell API...")
            location = query_opencell(session, config, cellular_data, logger)

            print_status("Updating WADMP GPS fields...")
            update_gps_fields(session, config, access_token, mac_address, location, logger)

        print_status("Update completed successfully.")
        if args.verbose and location.accuracy is not None:
            print_status(
                f"Resolved coordinates: latitude={location.latitude}, "
                f"longitude={location.longitude}, accuracy={location.accuracy} m."
            )
        return 0
    except AppError as exc:
        logger.error("Application error: %s", exc)
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logger.warning("Execution interrupted by user.")
        print("Operation cancelled by user.", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected application failure.")
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
