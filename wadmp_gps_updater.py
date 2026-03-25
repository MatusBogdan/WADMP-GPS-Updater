import argparse
import csv
import io
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from requests import Response, Session
from requests.exceptions import ConnectionError, RequestException, Timeout


DEFAULT_CONFIG_PATH = Path("config.env")
DEFAULT_LOG_PATH = Path("app.log")
DEFAULT_OUTPUT_DIR = Path("output")
DEFAULT_CACHE_PATH = Path("location_cache.json")
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_BATCH_PAGE_SIZE = 100
DEFAULT_LOCATION_PROVIDER = "opencell"
DEFAULT_MONITORING_COMPANY_PATH = "/monitoring/devices/companies/{company_id}"
DEFAULT_UPDATE_CSV_PATH = "/management/devices/long-operations/fields/csv"
DEFAULT_LONG_OPERATION_PATH = "/long-operations/{operation_id}"
DEFAULT_ONLINE_FIELD = "Online"
DEFAULT_ONLINE_VALUE = "1"
DEFAULT_RUN_INTERVAL_HOURS = 0
DEFAULT_LONG_OPERATION_POLL_SECONDS = 2
DEFAULT_LONG_OPERATION_TIMEOUT_SECONDS = 120
DEFAULT_HERE_API_URL = "https://pos.ls.hereapi.com/positioning/v1/locate"
DEFAULT_HERE_FALLBACK = "area"
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
    mac_address: str = "MacAddress"


@dataclass(frozen=True)
class Config:
    location_provider: str
    dmp_username: str
    dmp_password: str
    opencell_token: str | None
    here_api_key: str | None
    dmp_token_url: str
    dmp_api_base_url: str
    opencell_api_url: str | None
    here_api_url: str
    here_fallback: str
    fields: FieldNames
    company_id: int
    online_field: str = DEFAULT_ONLINE_FIELD
    online_value: str = DEFAULT_ONLINE_VALUE
    run_interval_hours: int = DEFAULT_RUN_INTERVAL_HOURS
    batch_page_size: int = DEFAULT_BATCH_PAGE_SIZE
    mnc_length: int | None = None
    cache_path: Path = DEFAULT_CACHE_PATH
    output_retention_days: int = 14
    log_retention_days: int = 14
    monitoring_company_path: str = DEFAULT_MONITORING_COMPANY_PATH
    update_csv_path: str = DEFAULT_UPDATE_CSV_PATH
    long_operation_path: str = DEFAULT_LONG_OPERATION_PATH
    long_operation_poll_seconds: int = DEFAULT_LONG_OPERATION_POLL_SECONDS
    long_operation_timeout_seconds: int = DEFAULT_LONG_OPERATION_TIMEOUT_SECONDS
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
class BatchDevice:
    fields: dict[str, Any]


@dataclass(frozen=True)
class CsvUpdateRow:
    mac_address: str
    latitude: float
    longitude: float
    altitude: int = 0


@dataclass
class BatchStats:
    total_online: int = 0
    total_loaded: int = 0
    processed: int = 0
    skipped_missing: int = 0
    skipped_invalid: int = 0
    lookup_failed: int = 0
    ready_for_upload: int = 0
    unique_cell_keys: int = 0
    cache_hits: int = 0
    cache_misses: int = 0


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
        "DMP_USERNAME",
        "DMP_PASSWORD",
        "DMP_COMPANY_ID",
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

    location_provider = raw.get("LOCATION_PROVIDER", DEFAULT_LOCATION_PROVIDER).strip().lower() or DEFAULT_LOCATION_PROVIDER
    if location_provider not in {"opencell", "here"}:
        raise AppError("Configuration value 'LOCATION_PROVIDER' must be 'opencell' or 'here'.")

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

    batch_page_size = parse_positive_int(
        raw.get("DMP_BATCH_PAGE_SIZE", str(DEFAULT_BATCH_PAGE_SIZE)),
        "DMP_BATCH_PAGE_SIZE",
    )
    run_interval_hours = parse_non_negative_int(
        raw.get("RUN_INTERVAL_HOURS", str(DEFAULT_RUN_INTERVAL_HOURS)),
        "RUN_INTERVAL_HOURS",
    )
    if run_interval_hours > 720:
        raise AppError("Configuration value 'RUN_INTERVAL_HOURS' must be between 0 and 720.")
    output_retention_days = parse_positive_int(
        raw.get("OUTPUT_RETENTION_DAYS", "14"),
        "OUTPUT_RETENTION_DAYS",
    )
    if output_retention_days > 90:
        raise AppError("Configuration value 'OUTPUT_RETENTION_DAYS' must be between 1 and 90.")
    log_retention_days = parse_positive_int(
        raw.get("LOG_RETENTION_DAYS", "14"),
        "LOG_RETENTION_DAYS",
    )
    if log_retention_days > 90:
        raise AppError("Configuration value 'LOG_RETENTION_DAYS' must be between 1 and 90.")
    long_operation_poll_seconds = parse_positive_int(
        raw.get("DMP_LONG_OPERATION_POLL_SECONDS", str(DEFAULT_LONG_OPERATION_POLL_SECONDS)),
        "DMP_LONG_OPERATION_POLL_SECONDS",
    )
    long_operation_timeout_seconds = parse_positive_int(
        raw.get("DMP_LONG_OPERATION_TIMEOUT_SECONDS", str(DEFAULT_LONG_OPERATION_TIMEOUT_SECONDS)),
        "DMP_LONG_OPERATION_TIMEOUT_SECONDS",
    )

    opencell_token: str | None = None
    opencell_api_url: str | None = None
    here_api_key: str | None = None
    here_api_url = raw.get("HERE_API_URL", DEFAULT_HERE_API_URL).strip() or DEFAULT_HERE_API_URL
    here_fallback = raw.get("HERE_FALLBACK", DEFAULT_HERE_FALLBACK).strip() or DEFAULT_HERE_FALLBACK

    if location_provider == "opencell":
        opencell_token = read_text_key("OPENCELL_TOKEN")
        opencell_api_url = read_text_key("OPENCELL_API_URL")
    else:
        here_api_key = read_text_key("HERE_API_KEY")

    return Config(
        location_provider=location_provider,
        dmp_username=read_text_key("DMP_USERNAME"),
        dmp_password=read_text_key("DMP_PASSWORD"),
        opencell_token=opencell_token,
        here_api_key=here_api_key,
        dmp_token_url=read_text_key("DMP_TOKEN_URL"),
        dmp_api_base_url=read_text_key("DMP_API_BASE_URL").rstrip("/"),
        opencell_api_url=opencell_api_url,
        here_api_url=here_api_url,
        here_fallback=here_fallback,
        fields=FieldNames(
            plmn=read_text_key("DMP_PLMN_FIELD"),
            cell_id=read_text_key("DMP_CELL_FIELD"),
            gps_latitude=read_text_key("DMP_GPS_LAT_FIELD"),
            gps_longitude=read_text_key("DMP_GPS_LON_FIELD"),
            gps_altitude=read_text_key("DMP_GPS_ALT_FIELD"),
            mac_address=raw.get("DMP_MAC_ADDRESS_FIELD", "MacAddress").strip() or "MacAddress",
        ),
        company_id=parse_positive_int(read_text_key("DMP_COMPANY_ID"), "DMP_COMPANY_ID"),
        online_field=raw.get("DMP_ONLINE_FIELD", DEFAULT_ONLINE_FIELD).strip() or DEFAULT_ONLINE_FIELD,
        online_value=raw.get("DMP_ONLINE_VALUE", DEFAULT_ONLINE_VALUE).strip() or DEFAULT_ONLINE_VALUE,
        run_interval_hours=run_interval_hours,
        batch_page_size=batch_page_size,
        mnc_length=mnc_length,
        cache_path=Path(raw.get("LOCATION_CACHE_PATH", str(DEFAULT_CACHE_PATH)).strip() or str(DEFAULT_CACHE_PATH)),
        output_retention_days=output_retention_days,
        log_retention_days=log_retention_days,
        long_operation_path=raw.get("DMP_LONG_OPERATION_PATH", DEFAULT_LONG_OPERATION_PATH).strip() or DEFAULT_LONG_OPERATION_PATH,
        long_operation_poll_seconds=long_operation_poll_seconds,
        long_operation_timeout_seconds=long_operation_timeout_seconds,
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


def parse_non_negative_int(raw_value: str, key_name: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise AppError(f"Configuration value '{key_name}' must be an integer.") from exc
    if value < 0:
        raise AppError(f"Configuration value '{key_name}' must be zero or a positive integer.")
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
            "Invalid MAC address encountered in WADMP data. "
            "Expected 12 hexadecimal characters."
        )
    compact = compact.upper()
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


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


def build_json_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json-patch+json",
    }

def fetch_online_devices(
    session: Session,
    config: Config,
    access_token: str,
    logger: logging.Logger,
) -> list[BatchDevice]:
    devices: list[BatchDevice] = []
    page = 1
    total_items: int | None = None

    while True:
        page_items, current_total = fetch_online_devices_page(
            session,
            config,
            access_token,
            page,
            logger,
        )
        if total_items is None:
            total_items = current_total
            logger.info("Batch read total online devices reported by WADMP: %s", total_items)

        if not page_items:
            break

        devices.extend(page_items)
        logger.info(
            "Loaded online devices page=%s page_items=%s accumulated=%s",
            page,
            len(page_items),
            len(devices),
        )

        if len(devices) >= current_total:
            break
        page += 1

    return devices


def fetch_online_devices_page(
    session: Session,
    config: Config,
    access_token: str,
    page: int,
    logger: logging.Logger,
) -> tuple[list[BatchDevice], int]:
    url = build_url(
        config.dmp_api_base_url,
        config.monitoring_company_path.format(company_id=config.company_id),
    )
    payload = {
        "filters": [
            {
                "field_name": config.online_field,
                "rule": {
                    "operator_id": "Equals",
                    "operands": [config.online_value],
                },
            }
        ],
        "fields": [
            {"name": config.fields.mac_address},
            {"name": config.fields.plmn},
            {"name": config.fields.cell_id},
        ],
        "page": page,
        "page_size": config.batch_page_size,
    }

    logger.info(
        "Requesting online devices page. url=%s company_id=%s page=%s page_size=%s",
        url,
        config.company_id,
        page,
        config.batch_page_size,
    )
    response = perform_request(
        session,
        "POST",
        url,
        timeout=config.request_timeout_seconds,
        logger=logger,
        action=f"WADMP online device list page {page}",
        verify_tls=config.verify_tls,
        headers=build_json_headers(access_token),
        data=json.dumps(payload),
    )
    data = ensure_success_json(response, f"WADMP online device list page {page}")
    if data.get("success") is False:
        message = data.get("message") or "WADMP rejected the online device list request"
        raise AppError(f"WADMP online device list failed: {message}")

    raw_items = data.get("data")
    if not isinstance(raw_items, list):
        raise AppError("WADMP online device list returned an unexpected result format.")

    total_items = data.get("total_items")
    if not isinstance(total_items, int) or total_items < 0:
        raise AppError("WADMP online device list did not return a valid total_items value.")

    page_items: list[BatchDevice] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        page_items.append(BatchDevice(fields=extract_fields_map(item)))

    return page_items, total_items


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


def build_cell_cache_key(cellular_data: CellularData) -> str:
    return f"{cellular_data.mcc}|{cellular_data.mnc}|{cellular_data.cell_id}"


def load_opencell_cache(cache_path: Path, logger: logging.Logger) -> dict[str, OpenCellResult]:
    if not cache_path.exists():
        logger.info("Location cache file does not exist yet. path=%s", cache_path)
        return {}

    try:
        raw_data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Location cache could not be loaded. Starting with empty cache. path=%s", cache_path)
        return {}

    if not isinstance(raw_data, dict):
        logger.warning("Location cache has invalid structure. Starting with empty cache. path=%s", cache_path)
        return {}

    cache: dict[str, OpenCellResult] = {}
    for key, value in raw_data.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        latitude = value.get("latitude")
        longitude = value.get("longitude")
        accuracy = value.get("accuracy")
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            continue
        accuracy_value = float(accuracy) if isinstance(accuracy, (int, float)) else None
        cache[key] = OpenCellResult(
            latitude=float(latitude),
            longitude=float(longitude),
            accuracy=accuracy_value,
        )

    logger.info("Loaded location cache entries. path=%s entries=%s", cache_path, len(cache))
    return cache


def save_opencell_cache(
    cache_path: Path,
    cache: dict[str, OpenCellResult],
    logger: logging.Logger,
) -> None:
    serializable = {
        key: {
            "latitude": value.latitude,
            "longitude": value.longitude,
            "accuracy": value.accuracy,
        }
        for key, value in cache.items()
    }
    try:
        cache_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to save location cache. path=%s error=%s", cache_path, exc)
        return

    logger.info("Saved location cache entries. path=%s entries=%s", cache_path, len(cache))


def query_opencell(
    session: Session,
    config: Config,
    cellular_data: CellularData,
    logger: logging.Logger,
) -> OpenCellResult:
    if not config.opencell_token or not config.opencell_api_url:
        raise AppError("OpenCell provider is selected but OPENCELL configuration is incomplete.")

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


def query_here(
    session: Session,
    config: Config,
    cellular_data: CellularData,
    logger: logging.Logger,
) -> OpenCellResult:
    if not config.here_api_key:
        raise AppError("HERE provider is selected but HERE_API_KEY is missing.")

    payload = {
        "lte": [
            {
                "mcc": int(cellular_data.mcc),
                "mnc": int(cellular_data.mnc),
                "cid": cellular_data.cell_id,
            }
        ]
    }
    params = {
        "apiKey": config.here_api_key,
        "fallback": config.here_fallback,
    }

    logger.info(
        "Querying HERE Positioning API. url=%s api_key=%s fallback=%s mcc=%s mnc=%s cell_id=%s",
        config.here_api_url,
        mask_secret(config.here_api_key),
        config.here_fallback,
        cellular_data.mcc,
        cellular_data.mnc,
        cellular_data.cell_id,
    )
    response = perform_request(
        session,
        "POST",
        config.here_api_url,
        timeout=config.request_timeout_seconds,
        logger=logger,
        action="HERE lookup",
        verify_tls=config.verify_tls,
        params=params,
        json=payload,
    )
    data = ensure_success_json(response, "HERE lookup")

    location = data.get("location")
    if not isinstance(location, dict):
        raise AppError("HERE lookup returned no location object for the supplied cell data.")

    latitude = location.get("lat")
    longitude = location.get("lng")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        raise AppError("HERE lookup returned no coordinates for the supplied cell data.")

    accuracy = location.get("accuracy")
    accuracy_value = float(accuracy) if isinstance(accuracy, (int, float)) else None

    logger.info(
        "HERE lookup succeeded. lat=%s lon=%s accuracy=%s",
        latitude,
        longitude,
        accuracy_value,
    )
    return OpenCellResult(
        latitude=float(latitude),
        longitude=float(longitude),
        accuracy=accuracy_value,
    )


def query_location(
    session: Session,
    config: Config,
    cellular_data: CellularData,
    logger: logging.Logger,
) -> OpenCellResult:
    if config.location_provider == "here":
        return query_here(session, config, cellular_data, logger)
    return query_opencell(session, config, cellular_data, logger)

def build_csv_payload(rows: list[CsvUpdateRow], config: Config) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            config.fields.mac_address,
            config.fields.gps_latitude,
            config.fields.gps_longitude,
            config.fields.gps_altitude,
        ]
    )
    for row in rows:
        writer.writerow([row.mac_address, row.latitude, row.longitude, row.altitude])
    return buffer.getvalue().encode("utf-8")


def save_csv_payload(csv_bytes: bytes, logger: logging.Logger) -> Path:
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = DEFAULT_OUTPUT_DIR / f"gps_updates_{timestamp}.csv"
    output_path.write_bytes(csv_bytes)
    logger.info("Saved GPS CSV payload to disk. path=%s", output_path)
    return output_path


def cleanup_old_output_files(retention_days: int, logger: logging.Logger) -> None:
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff_timestamp = time.time() - (retention_days * 24 * 60 * 60)
    deleted_count = 0

    for file_path in DEFAULT_OUTPUT_DIR.glob("*.csv"):
        try:
            if file_path.stat().st_mtime < cutoff_timestamp:
                file_path.unlink()
                deleted_count += 1
                logger.info("Deleted old output CSV file. path=%s", file_path)
        except OSError as exc:
            logger.warning("Failed to delete old output CSV file. path=%s error=%s", file_path, exc)

    logger.info(
        "Output cleanup completed. retention_days=%s deleted_files=%s",
        retention_days,
        deleted_count,
    )


def cleanup_old_log_file(retention_days: int) -> str | None:
    cutoff_timestamp = time.time() - (retention_days * 24 * 60 * 60)
    try:
        if DEFAULT_LOG_PATH.exists() and DEFAULT_LOG_PATH.stat().st_mtime < cutoff_timestamp:
            DEFAULT_LOG_PATH.unlink()
            return f"Deleted old log file: {DEFAULT_LOG_PATH}"
    except OSError as exc:
        return f"Failed to delete old log file {DEFAULT_LOG_PATH}: {exc}"
    return None


def wait_for_long_operation(
    session: Session,
    config: Config,
    access_token: str,
    operation_id: int,
    logger: logging.Logger,
) -> None:
    url = build_url(
        config.dmp_api_base_url,
        config.long_operation_path.format(operation_id=operation_id),
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    deadline = time.monotonic() + config.long_operation_timeout_seconds
    last_logged_state: str | None = None

    while True:
        response = perform_request(
            session,
            "GET",
            url,
            timeout=config.request_timeout_seconds,
            logger=logger,
            action=f"WADMP long operation status {operation_id}",
            verify_tls=config.verify_tls,
            headers=headers,
        )
        result = ensure_success_json(response, f"WADMP long operation status {operation_id}")
        if isinstance(result, dict) and result.get("success") is False:
            message = result.get("message") or "unknown long operation error"
            raise AppError(f"WADMP long operation failed: {message}")

        data = result.get("data")
        if not isinstance(data, dict):
            raise AppError("WADMP long operation status returned an unexpected result format.")

        state = str(data.get("state", "")).strip()
        failed_items = data.get("failed_items")
        operation_result = data.get("result")
        if state != last_logged_state:
            logger.info(
                "Long operation status changed. operation_id=%s state=%s failed_items=%s",
                operation_id,
                state,
                failed_items,
            )
            last_logged_state = state

        if state == "Finished":
            if isinstance(failed_items, int) and failed_items > 0:
                raise AppError(
                    f"WADMP long operation finished with {failed_items} failed item(s)."
                )
            return

        if state == "FinishedWithErrors":
            raise AppError(build_long_operation_error_message(operation_result, failed_items))

        if state in {"Failed", "Canceled", "Cancelled"}:
            raise AppError(f"WADMP long operation ended with state '{state}'.")

        if time.monotonic() >= deadline:
            raise AppError(
                f"WADMP long operation did not finish within {config.long_operation_timeout_seconds} seconds."
            )

        time.sleep(config.long_operation_poll_seconds)


def build_long_operation_error_message(operation_result: Any, failed_items: Any) -> str:
    if isinstance(operation_result, dict):
        items = operation_result.get("failed_items")
        if isinstance(items, list) and items:
            first_item = items[0]
            if isinstance(first_item, dict):
                identifier = str(first_item.get("identifier", "")).strip()
                error_message = str(first_item.get("error_message", "")).strip()
                details = []
                if identifier:
                    details.append(f"identifier={identifier}")
                if error_message:
                    details.append(f"error={error_message}")
                if details:
                    return "WADMP long operation finished with errors: " + ", ".join(details)

    if isinstance(failed_items, int) and failed_items > 0:
        return f"WADMP long operation finished with {failed_items} failed item(s)."

    return "WADMP long operation finished with errors."


def upload_csv_updates(
    session: Session,
    config: Config,
    access_token: str,
    rows: list[CsvUpdateRow],
    logger: logging.Logger,
) -> int:
    csv_bytes = build_csv_payload(rows, config)
    output_path = save_csv_payload(csv_bytes, logger)
    url = build_url(config.dmp_api_base_url, config.update_csv_path)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    data = {
        "CompanyId": str(config.company_id),
    }

    logger.info(
        "Uploading GPS CSV update. url=%s company_id=%s row_count=%s csv_size_bytes=%s csv_path=%s",
        url,
        config.company_id,
        len(rows),
        len(csv_bytes),
        output_path,
    )
    with output_path.open("rb") as file_handle:
        files = {"File": file_handle}
        response = perform_request(
            session,
            "POST",
            url,
            timeout=config.request_timeout_seconds,
            logger=logger,
            action="WADMP CSV GPS update",
            verify_tls=config.verify_tls,
            headers=headers,
            files=files,
            data=data,
        )
    result = ensure_success_json(response, "WADMP CSV GPS update")
    if isinstance(result, dict) and result.get("success") is False:
        message = result.get("message") or "unknown CSV upload error"
        raise AppError(f"WADMP CSV GPS update failed: {message}")

    data = result.get("data")
    if not isinstance(data, dict):
        raise AppError("WADMP CSV GPS update did not return a valid operation payload.")

    operation_id = data.get("id")
    if not isinstance(operation_id, int) or operation_id <= 0:
        raise AppError("WADMP CSV GPS update did not return a valid long operation ID.")

    logger.info("WADMP CSV GPS update accepted. row_count=%s operation_id=%s", len(rows), operation_id)
    return operation_id


def process_online_devices(
    session: Session,
    config: Config,
    devices: list[BatchDevice],
    logger: logging.Logger,
) -> tuple[list[CsvUpdateRow], BatchStats]:
    rows: list[CsvUpdateRow] = []
    stats = BatchStats(total_online=len(devices), total_loaded=len(devices))
    opencell_cache = load_opencell_cache(config.cache_path, logger)
    unique_keys_seen: set[str] = set()

    for device in devices:
        stats.processed += 1
        raw_mac = str(device.fields.get(config.fields.mac_address, "")).strip()
        try:
            mac_address = normalize_mac_address(raw_mac)
        except AppError:
            stats.skipped_invalid += 1
            logger.warning("Skipping device with invalid MAC address. raw_mac=%s", raw_mac)
            continue

        try:
            cellular_data = extract_cellular_data(device.fields, config, logger)
        except AppError as exc:
            stats.skipped_missing += 1
            logger.warning("Skipping device due to missing or invalid cellular data. mac=%s error=%s", mac_address, exc)
            continue

        cell_key = build_cell_cache_key(cellular_data)
        unique_keys_seen.add(cell_key)

        cached_location = opencell_cache.get(cell_key)
        if cached_location is not None:
            stats.cache_hits += 1
            location = cached_location
            logger.info("Using cached location result. mac=%s cell_key=%s", mac_address, cell_key)
        else:
            stats.cache_misses += 1
            try:
                location = query_location(session, config, cellular_data, logger)
            except AppError as exc:
                stats.lookup_failed += 1
                logger.warning("Skipping device due to location lookup failure. mac=%s error=%s", mac_address, exc)
                continue
            opencell_cache[cell_key] = location

        rows.append(
            CsvUpdateRow(
                mac_address=mac_address,
                latitude=location.latitude,
                longitude=location.longitude,
                altitude=0,
            )
        )

    stats.ready_for_upload = len(rows)
    stats.unique_cell_keys = len(unique_keys_seen)
    save_opencell_cache(config.cache_path, opencell_cache, logger)
    return rows, stats


def run_batch_mode(
    session: Session,
    config: Config,
    access_token: str,
    logger: logging.Logger,
) -> BatchStats:
    print_status("Reading online devices from WADMP...")
    devices = fetch_online_devices(session, config, access_token, logger)
    if not devices:
        raise AppError("No online devices were returned by WADMP.")

    print_status(f"Loaded {len(devices)} online devices.")
    print_status(f"Querying {config.location_provider.upper()} API for online devices...")
    csv_rows, stats = process_online_devices(session, config, devices, logger)
    if not csv_rows:
        raise AppError("No GPS updates were generated for online devices.")

    print_status(f"Prepared {len(csv_rows)} GPS updates. Uploading CSV to WADMP...")
    operation_id = upload_csv_updates(session, config, access_token, csv_rows, logger)
    print_status(f"CSV accepted by WADMP. Waiting for long operation {operation_id}...")
    wait_for_long_operation(session, config, access_token, operation_id, logger)
    return stats


def execute_batch_cycle(
    session: Session,
    config: Config,
    logger: logging.Logger,
) -> BatchStats:
    print_status("Authenticating to WADMP...")
    access_token = authenticate_wadmp(session, config, logger)
    return run_batch_mode(session, config, access_token, logger)


def wait_for_next_run(run_interval_hours: int, logger: logging.Logger, reason: str) -> None:
    next_run_timestamp = time.time() + (run_interval_hours * 3600)
    next_run_text = datetime.fromtimestamp(next_run_timestamp).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(
        "Scheduled next batch run. reason=%s run_interval_hours=%s next_run=%s",
        reason,
        run_interval_hours,
        next_run_text,
    )
    print_status(
        f"Next resync will run in {run_interval_hours} hour(s) at {next_run_text}. "
        "Press Ctrl+C to stop."
    )
    time.sleep(run_interval_hours * 3600)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update WADMP GPS fields for all online routers using LTE cell data and a configurable location lookup provider."
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

    try:
        config = load_config(Path(args.config))
        log_cleanup_message = cleanup_old_log_file(config.log_retention_days)
        logger = setup_logging(verbose=args.verbose)
        if log_cleanup_message:
            logger.info("%s", log_cleanup_message)
        cleanup_old_output_files(config.output_retention_days, logger)

        with requests.Session() as session:
            while True:
                try:
                    stats = execute_batch_cycle(session, config, logger)

                    print_status("Batch update completed successfully.")
                    if args.verbose:
                        print_status(
                            "Summary: "
                            f"online={stats.total_online}, "
                            f"processed={stats.processed}, "
                            f"prepared={stats.ready_for_upload}, "
                            f"unique_cells={stats.unique_cell_keys}, "
                            f"cache_hits={stats.cache_hits}, "
                            f"cache_misses={stats.cache_misses}, "
                            f"missing_or_invalid={stats.skipped_missing + stats.skipped_invalid}, "
                            f"lookup_failed={stats.lookup_failed}."
                        )

                    if config.run_interval_hours == 0:
                        break

                    wait_for_next_run(config.run_interval_hours, logger, "successful cycle")
                except AppError as exc:
                    if config.run_interval_hours == 0:
                        raise
                    logger.error("Scheduled batch cycle failed: %s", exc)
                    print_status(f"Batch cycle failed: {exc}")
                    wait_for_next_run(config.run_interval_hours, logger, "failed cycle")
        return 0
    except AppError as exc:
        if "logger" in locals():
            logger.error("Application error: %s", exc)
        print(f"Application error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if "logger" in locals():
            logger.warning("Execution interrupted by user.")
        print("Operation cancelled by user.", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover
        if "logger" in locals():
            logger.exception("Unexpected application failure.")
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
