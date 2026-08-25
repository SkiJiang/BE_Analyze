"""Authenticated client for the configured property electricity service."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import ssl
import time
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from electricity_app.api_transport import (
    ExternalRequestError,
    is_auth_payload,
    is_tls_error,
    json_object,
    request_with_retry,
)
from electricity_app.config import Settings
from electricity_app.domain import ElectricityRecord


class PropertyError(RuntimeError):
    default_code = "property_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class PropertyAuthenticationError(PropertyError):
    """The property service rejected the configured credentials or token."""

    default_code = "authentication"


class PropertyProtocolError(PropertyError):
    """The property service returned a response outside its expected schema."""

    default_code = "protocol"


class PropertyUnavailableError(PropertyError):
    """The property service could not be reached or returned a server failure."""

    default_code = "network"


class _AuthenticationExpired(RuntimeError):
    """Internal marker for a details request that needs one reauthentication."""


class PropertyClient:
    _LOGIN_PATH = "/xboot/auth/login"
    _DETAILS_PATH = "/xboot/goodits/room/pageBalanceDetails"
    _BALANCE_PATH = "/xboot/goodits/count/getBalance"
    _PAGE_SIZE = 100
    _MAX_PAGES = 100
    _MAX_ATTEMPTS = 3
    _RETRY_BASE_SECONDS = 0.25

    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        *,
        sleep: Any = time.sleep,
    ) -> None:
        self._settings = settings
        self._base_url = str(settings.property_base_url).rstrip("/")
        self._token: str | None = None
        self._sleep = sleep
        self.last_field_names: tuple[str, ...] = ()
        self._http = http or httpx.Client(
            base_url=self._base_url,
            verify=True,
            timeout=httpx.Timeout(10, read=30),
            follow_redirects=False,
        )
        if http is not None and self._origin(http.base_url) != self._origin(httpx.URL(self._base_url)):
            raise ValueError("Injected HTTP client must use the configured property origin")
        if http is not None and http.follow_redirects:
            raise ValueError("Injected HTTP client must disable redirects")
        if http is not None:
            if http._mounts:
                raise ValueError("Injected HTTP client must not use mounts")
            self._require_verified_tls(http)

    def login(self) -> None:
        """Authenticate without retaining credentials or token outside process memory."""
        response = self._request_with_retry(
            self._LOGIN_PATH,
            data={
                "username": self._settings.property_username,
                "password": self._settings.property_password.get_secret_value(),
            },
        )

        if response.status_code in (401, 403) or 400 <= response.status_code < 500:
            raise PropertyAuthenticationError("Property login was rejected")

        payload = self._response_object(response)
        token = payload.get("result")
        if payload.get("success") is not True or not isinstance(token, str) or not token:
            raise PropertyAuthenticationError("Property login did not return a token")
        self._token = token

    def fetch_day(self, day: date) -> list[ElectricityRecord]:
        """Fetch all available electricity details for one local calendar day."""
        if self._token is None:
            self.login()

        records: list[ElectricityRecord] = []
        field_names: set[str] = set()
        reauthenticated = False

        for page_number in range(1, self._MAX_PAGES + 1):
            try:
                result = self._fetch_page(day, page_number)
            except _AuthenticationExpired as exc:
                if reauthenticated:
                    raise PropertyAuthenticationError("Property token was rejected after retry") from exc
                self._token = None
                self.login()
                reauthenticated = True
                try:
                    result = self._fetch_page(day, page_number)
                except _AuthenticationExpired as retry_exc:
                    raise PropertyAuthenticationError("Property token was rejected after retry") from retry_exc

            total, items = self._page_items(result)
            for item in items:
                field_names.update(item.keys())
                record = self._parse_record(item)
                self._validate_scope(record, day)
                records.append(record)
            self.last_field_names = tuple(sorted(field_names))

            if len(records) >= total or len(items) < self._PAGE_SIZE:
                return records

        raise PropertyProtocolError(
            "Property response exceeded the page safety limit",
            code="pagination_overflow",
        )

    def fetch_balance(self) -> Decimal | None:
        """Fetch the account balance for the configured room."""
        if self._token is None:
            self.login()

        reauthenticated = False
        while True:
            response = self._request_with_retry(
                self._BALANCE_PATH,
                method="get",
                params={"pageNumber": "1", "pageSize": "100"},
                headers={"Accesstoken": self._token},
            )
            if response.status_code in (401, 403):
                if reauthenticated:
                    raise PropertyAuthenticationError(
                        "Property balance request was rejected after retry"
                    )
                self._token = None
                self.login()
                reauthenticated = True
                continue

            payload = self._response_object(response)
            if self._is_application_auth_failure(payload):
                if reauthenticated:
                    raise PropertyAuthenticationError(
                        "Property balance request was rejected after retry"
                    )
                self._token = None
                self.login()
                reauthenticated = True
                continue
            if payload.get("success") is not True:
                raise PropertyProtocolError(
                    "Property balance response was unsuccessful"
                )

            result = payload.get("result")
            if not isinstance(result, dict):
                raise PropertyProtocolError(
                    "Property balance response has no result object"
                )
            records = result.get("records")
            if not isinstance(records, list) or not all(
                isinstance(item, dict) for item in records
            ):
                raise PropertyProtocolError(
                    "Property balance response has no records list"
                )

            matches = [
                item
                for item in records
                if self._room_names_match(
                    item.get("roomName") or item.get("roomNo")
                )
            ]
            if len(matches) != 1:
                raise PropertyProtocolError(
                    "Property balance response did not identify one configured room",
                    code="balance_scope_mismatch",
                )
            balance_text = self._optional_text(matches[0].get("powerMoney"))
            if balance_text is None:
                return None
            try:
                balance = Decimal(balance_text.strip())
            except InvalidOperation as exc:
                raise PropertyProtocolError(
                    "Property balance is not a valid decimal",
                    code="invalid_balance",
                ) from exc
            if not balance.is_finite():
                raise PropertyProtocolError(
                    "Property balance is not finite",
                    code="invalid_balance",
                )
            return balance

    def _fetch_page(self, day: date, page_number: int) -> dict[str, object]:
        if self._token is None:
            raise _AuthenticationExpired()
        response = self._request_with_retry(
            self._DETAILS_PATH,
            data={
                "pageNumber": str(page_number),
                "pageSize": str(self._PAGE_SIZE),
                "type": "2",
                "startDate": day.isoformat(),
                "endDate": day.isoformat(),
            },
            headers={"Accesstoken": self._token},
        )

        if response.status_code in (401, 403):
            raise _AuthenticationExpired()
        if response.status_code >= 400:
            raise PropertyProtocolError("Property details request was rejected")

        payload = self._response_object(response)
        if self._is_application_auth_failure(payload):
            raise _AuthenticationExpired()
        if payload.get("success") is not True:
            raise PropertyProtocolError("Property details response was unsuccessful")

        result = payload.get("result")
        if not isinstance(result, dict):
            raise PropertyProtocolError("Property details response has no result object")
        return result

    def _page_items(self, result: dict[str, object]) -> tuple[int, list[dict[str, object]]]:
        total = result.get("total")
        items = result.get("records")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise PropertyProtocolError("Property details result has an invalid total")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise PropertyProtocolError("Property details result has no records list")
        return total, items

    def _parse_record(self, item: dict[str, object]) -> ElectricityRecord:
        try:
            room_name = self._required_alias_text(item, "roomName", "roomNo")
            device_name = self._required_text(item, "deviceName")
            occurred_text = self._required_text(item, "time")
            energy_text = self._required_text(item, "energy")
            money_text = self._required_text(item, "money")
            rate_text = self._optional_text(item.get("rate"))
            balance_text = self._optional_text(item.get("balance"))
            occurred_at = datetime.strptime(occurred_text.strip(), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo(self._settings.timezone)
            )
            upstream_id = self._optional_text(item.get("id"))
        except (KeyError, TypeError, ValueError) as exc:
            raise PropertyProtocolError("Property record has invalid required fields") from exc
        try:
            energy = Decimal(energy_text.strip())
            money = Decimal(money_text.strip())
            rate = Decimal(rate_text.strip()) if rate_text is not None else None
            balance = Decimal(balance_text.strip()) if balance_text is not None else None
        except InvalidOperation as exc:
            raise PropertyProtocolError(
                "Property record has an invalid decimal",
                code="invalid_decimal",
            ) from exc
        if not all(
            value is None or value.is_finite()
            for value in (energy, money, rate, balance)
        ):
            raise PropertyProtocolError(
                "Property record has a non-finite decimal",
                code="invalid_decimal",
            )

        unique_key = upstream_id or sha256(
            "\x1f".join(
                (room_name, device_name, occurred_text, energy_text, money_text, rate_text or "")
            ).encode("utf-8")
        ).hexdigest()
        return ElectricityRecord(
            unique_key=unique_key,
            upstream_id=upstream_id,
            room_name=room_name,
            device_name=device_name,
            occurred_at=occurred_at,
            energy=energy,
            money=money,
            rate=rate,
            balance=balance,
        )

    def _validate_scope(self, record: ElectricityRecord, day: date) -> None:
        if (
            record.room_name != self._settings.property_room_name
            or record.device_name != self._settings.property_device_name
        ):
            raise PropertyProtocolError(
                "Property record is outside the configured scope",
                code="scope_mismatch",
            )
        if record.occurred_at.astimezone(
            ZoneInfo(self._settings.timezone)
        ).date() != day:
            raise PropertyProtocolError(
                "Property record is outside the requested day",
                code="wrong_day",
            )

    def _request_with_retry(
        self,
        path: str,
        *,
        method: str = "post",
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            return request_with_retry(
                self._http,
                method,
                path,
                attempts=self._MAX_ATTEMPTS,
                retry_base_seconds=self._RETRY_BASE_SECONDS,
                sleep=self._sleep,
                **kwargs,
            )
        except ExternalRequestError as error:
            raise PropertyUnavailableError(
                "Property request transport failed",
                code=error.code,
            ) from error

    @staticmethod
    def _response_object(response: httpx.Response) -> dict[str, object]:
        try:
            return json_object(
                response,
                error_message="Property service returned invalid JSON",
            )
        except ExternalRequestError as exc:
            raise PropertyProtocolError(
                "Property service returned invalid JSON",
                code="invalid_json",
            ) from exc

    @staticmethod
    def _required_text(item: dict[str, object], field: str) -> str:
        value = item[field]
        if value is None or isinstance(value, (dict, list)):
            raise ValueError(field)
        text = str(value)
        if not text.strip():
            raise ValueError(field)
        return text

    @classmethod
    def _required_alias_text(
        cls,
        item: dict[str, object],
        *fields: str,
    ) -> str:
        values = [
            cls._required_text(item, field)
            for field in fields
            if field in item
        ]
        if not values:
            raise KeyError(fields[0])
        if len(set(values)) != 1:
            raise ValueError("conflicting aliases")
        return values[0]

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            raise ValueError("invalid optional value")
        text = str(value)
        return text if text.strip() else None

    @staticmethod
    def _require_verified_tls(http: httpx.Client) -> None:
        transport = http._transport
        if isinstance(transport, httpx.MockTransport):
            return
        pool = getattr(transport, "_pool", None)
        context = getattr(pool, "_ssl_context", None)
        if (
            not isinstance(context, ssl.SSLContext)
            or context.verify_mode != ssl.CERT_REQUIRED
            or not context.check_hostname
        ):
            raise ValueError("Injected HTTP client must use verified TLS")

    @staticmethod
    def _is_tls_error(error: BaseException) -> bool:
        return is_tls_error(error)

    @staticmethod
    def _is_application_auth_failure(payload: dict[str, object]) -> bool:
        return is_auth_payload(payload)

    @staticmethod
    def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
        return url.scheme, url.host, url.port

    def _room_names_match(self, value: object) -> bool:
        if value is None:
            return False
        return self._normalize_room_name(str(value)) == self._normalize_room_name(
            self._settings.property_room_name
        )

    @staticmethod
    def _normalize_room_name(value: str) -> str:
        normalized = re.sub(r"\s+", "", value).casefold()
        return re.sub(r"-(?:\d+f|\d+层)(?=-|$)", "", normalized)
