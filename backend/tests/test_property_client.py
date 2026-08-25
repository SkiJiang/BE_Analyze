from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import ssl
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from electricity_app.domain import ElectricityRecord
from electricity_app.property_client import (
    PropertyAuthenticationError,
    PropertyClient,
    PropertyProtocolError,
    PropertyUnavailableError,
)

BASE_URL = "https://zf.zhongkeqizhi.cn:9000"
LOGIN_URL = f"{BASE_URL}/xboot/auth/login"
DETAILS_URL = f"{BASE_URL}/xboot/goodits/room/pageBalanceDetails"
BALANCE_URL = f"{BASE_URL}/xboot/goodits/count/getBalance"


@respx.mock
def test_fetch_day_logs_in_and_parses_records(client, fixture_json):
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "result": "token-value"})
    )
    respx.post(DETAILS_URL).mock(return_value=httpx.Response(200, json=fixture_json))

    records = client.fetch_day(date(2026, 7, 29))

    assert len(records) == 2
    assert str(records[0].energy) == "0.1"
    assert records[0].occurred_at.tzinfo == ZoneInfo("Asia/Shanghai")


@respx.mock
def test_fetch_balance_reads_power_money_for_configured_room(client, settings):
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "result": "token-value"})
    )
    balance = respx.get(BALANCE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "total": 1,
                    "records": [
                        {
                            "roomName": "麒麟科创园-7号楼-8F-805",
                            "powerMoney": 213.03,
                            "waterMoney": -127.75,
                        }
                    ],
                },
            },
        )
    )

    assert client.fetch_balance() == Decimal("213.03")
    assert balance.calls[0].request.headers["Accesstoken"] == "token-value"
    assert dict(balance.calls[0].request.url.params) == {
        "pageNumber": "1",
        "pageSize": "100",
    }


@respx.mock
def test_fetch_day_accepts_live_room_no_schema_without_upstream_id(
    client, fixture_json
):
    payload = deepcopy(fixture_json)
    for item in payload["result"]["records"]:
        item["roomNo"] = item.pop("roomName")
        item.pop("id")
        item["energy"] = float(item["energy"])
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": "token-value"}
        )
    )
    respx.post(DETAILS_URL).mock(
        return_value=httpx.Response(200, json=payload)
    )

    records = client.fetch_day(date(2026, 7, 29))

    assert len(records) == 2
    assert records[0].room_name == "麒麟科创园-7号楼-805"
    assert records[0].upstream_id is None
    assert len(records[0].unique_key) == 64


def test_parse_record_rejects_conflicting_room_aliases(client):
    with pytest.raises(PropertyProtocolError):
        client._parse_record(
            {
                "roomName": "configured-room",
                "roomNo": "different-room",
                "deviceName": "meter",
                "time": "2026-07-29 10:07:07",
                "energy": "0.1",
                "money": "0.06",
            }
        )


@respx.mock
def test_login_and_details_use_exact_form_parameters(
    client, fixture_json, settings
):
    login = respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": "token-value"}
        )
    )
    details = respx.post(DETAILS_URL).mock(
        return_value=httpx.Response(200, json=fixture_json)
    )

    client.fetch_day(date(2026, 7, 29))

    assert parse_qs(login.calls[0].request.content.decode("utf-8")) == {
        "username": [settings.property_username],
        "password": [settings.property_password.get_secret_value()],
    }
    assert parse_qs(details.calls[0].request.content.decode("utf-8")) == {
        "pageNumber": ["1"],
        "pageSize": ["100"],
        "type": ["2"],
        "startDate": ["2026-07-29"],
        "endDate": ["2026-07-29"],
    }
    assert details.calls[0].request.headers["Accesstoken"] == "token-value"


@respx.mock
def test_fetch_day_reauthenticates_once_after_auth_failure(client, fixture_json):
    login = respx.post(LOGIN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"success": True, "result": "token-1"}),
            httpx.Response(200, json={"success": True, "result": "token-2"}),
        ]
    )
    details = respx.post(DETAILS_URL).mock(
        side_effect=[
            httpx.Response(401, json={"success": False}),
            httpx.Response(200, json=fixture_json),
        ]
    )

    assert len(client.fetch_day(date(2026, 7, 29))) == 2
    assert login.call_count == 2
    assert details.call_count == 2


@respx.mock
def test_application_auth_failure_reauthenticates_once_then_fails(client):
    login = respx.post(LOGIN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"success": True, "result": "token-1"}),
            httpx.Response(200, json={"success": True, "result": "token-2"}),
        ]
    )
    details = respx.post(DETAILS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": False,
                "code": 401,
                "message": "authentication required",
            },
        )
    )

    with pytest.raises(PropertyAuthenticationError) as error:
        client.fetch_day(date(2026, 7, 29))

    assert error.value.code == "authentication"
    assert login.call_count == 2
    assert details.call_count == 2


@respx.mock
def test_fetch_day_reads_multiple_pages_until_total_is_reached(client, fixture_json):
    first_page = fixture_json["result"]["records"] * 50
    second_page = fixture_json["result"]["records"][:1]
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "result": "token"})
    )
    details = respx.post(DETAILS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"success": True, "result": {"total": 101, "records": first_page}}),
            httpx.Response(200, json={"success": True, "result": {"total": 101, "records": second_page}}),
        ]
    )

    records = client.fetch_day(date(2026, 7, 29))

    assert len(records) == 101
    assert details.call_count == 2


@respx.mock
def test_page_limit_stops_after_exactly_one_hundred_pages(
    client, fixture_json
):
    page = fixture_json["result"]["records"] * 50
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": "token"}
        )
    )
    details = respx.post(DETAILS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": {"total": 10_001, "records": page},
            },
        )
    )

    with pytest.raises(PropertyProtocolError) as error:
        client.fetch_day(date(2026, 7, 29))

    assert error.value.code == "pagination_overflow"
    assert details.call_count == 100


@respx.mock
def test_fetch_day_rejects_mixed_room_records_before_returning(
    client, fixture_json
):
    payload = deepcopy(fixture_json)
    payload["result"]["records"][1]["roomName"] = "different-room"
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": "token"}
        )
    )
    respx.post(DETAILS_URL).mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(PropertyProtocolError) as error:
        client.fetch_day(date(2026, 7, 29))

    assert error.value.code == "scope_mismatch"


@respx.mock
def test_fetch_day_rejects_record_from_another_shanghai_day(
    client, fixture_json
):
    payload = deepcopy(fixture_json)
    payload["result"]["records"][0]["time"] = "2026-07-28 23:59:59"
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": "token"}
        )
    )
    respx.post(DETAILS_URL).mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(PropertyProtocolError) as error:
        client.fetch_day(date(2026, 7, 29))

    assert error.value.code == "wrong_day"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("energy", "NaN"),
        ("money", "Infinity"),
        ("rate", "-Infinity"),
        ("balance", "NaN"),
    ],
)
@respx.mock
def test_non_finite_decimal_values_are_protocol_errors(
    client, fixture_json, field, value
):
    payload = deepcopy(fixture_json)
    payload["result"]["records"][0][field] = value
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": "token"}
        )
    )
    respx.post(DETAILS_URL).mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(PropertyProtocolError) as error:
        client.fetch_day(date(2026, 7, 29))

    assert error.value.code == "invalid_decimal"


def test_http_5xx_retries_are_bounded_with_exponential_delays(
    settings, fixture_json
):
    details_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal details_attempts
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(
                200, json={"success": True, "result": "token"}
            )
        details_attempts += 1
        if details_attempts < 3:
            return httpx.Response(503, json={"success": False})
        return httpx.Response(200, json=fixture_json)

    delays: list[float] = []
    with httpx.Client(
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
    ) as http:
        retrying_client = PropertyClient(
            settings,
            http=http,
            sleep=delays.append,
        )
        records = retrying_client.fetch_day(date(2026, 7, 29))

    assert len(records) == 2
    assert details_attempts == 3
    assert delays == [0.25, 0.5]


def test_network_retries_stop_after_three_attempts(settings):
    details_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal details_attempts
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(
                200, json={"success": True, "result": "token"}
            )
        details_attempts += 1
        raise httpx.ConnectError("network unavailable", request=request)

    delays: list[float] = []
    with httpx.Client(
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
    ) as http:
        retrying_client = PropertyClient(
            settings,
            http=http,
            sleep=delays.append,
        )
        with pytest.raises(PropertyUnavailableError) as error:
            retrying_client.fetch_day(date(2026, 7, 29))

    assert error.value.code == "network"
    assert details_attempts == 3
    assert delays == [0.25, 0.5]


def test_tls_failure_has_distinct_safe_diagnostic_code(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        error = httpx.ConnectError("TLS handshake failed", request=request)
        error.__cause__ = ssl.SSLError("certificate verify failed")
        raise error

    with httpx.Client(
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
    ) as http:
        retrying_client = PropertyClient(
            settings,
            http=http,
            sleep=lambda _: None,
        )
        with pytest.raises(PropertyUnavailableError) as error:
            retrying_client.fetch_day(date(2026, 7, 29))

    assert error.value.code == "tls"


def test_details_4xx_is_not_retried(settings):
    details_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal details_attempts
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(
                200, json={"success": True, "result": "token"}
            )
        details_attempts += 1
        return httpx.Response(422, json={"success": False})

    with httpx.Client(
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
    ) as http:
        retrying_client = PropertyClient(
            settings,
            http=http,
            sleep=lambda _: None,
        )
        with pytest.raises(PropertyProtocolError):
            retrying_client.fetch_day(date(2026, 7, 29))

    assert details_attempts == 1


@respx.mock
def test_invalid_json_has_distinct_safe_diagnostic_code(client):
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": "token"}
        )
    )
    respx.post(DETAILS_URL).mock(
        return_value=httpx.Response(200, content=b"not-json")
    )

    with pytest.raises(PropertyProtocolError) as error:
        client.fetch_day(date(2026, 7, 29))

    assert error.value.code == "invalid_json"


@respx.mock
def test_missing_records_is_a_protocol_error(client):
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "result": "token"})
    )
    respx.post(DETAILS_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )

    with pytest.raises(PropertyProtocolError):
        client.fetch_day(date(2026, 7, 29))


@respx.mock
def test_login_rejects_missing_token(client):
    respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json={"success": True}))

    with pytest.raises(PropertyAuthenticationError):
        client.login()


@respx.mock
def test_network_error_is_unavailable(client):
    respx.post(LOGIN_URL).mock(side_effect=httpx.ConnectError("connection failed"))

    with pytest.raises(PropertyUnavailableError):
        client.fetch_day(date(2026, 7, 29))


def test_parse_record_hashes_stable_fields_when_upstream_id_is_absent(client):
    record = client._parse_record(
        {
            "roomName": "room",
            "deviceName": "meter",
            "time": "2026-07-29 10:07:07",
            "energy": "0.1",
            "money": "0.06",
            "rate": "0.55",
            "balance": "182.66",
        }
    )

    assert record.upstream_id is None
    assert len(record.unique_key) == 64
    assert record.energy == Decimal("0.1")


def test_parse_record_preserves_whitespace_and_uses_it_in_fallback_identity(client):
    plain = client._parse_record(
        {
            "roomName": "room",
            "deviceName": "meter",
            "time": "2026-07-29 10:07:07",
            "energy": "0.1",
            "money": "0.06",
            "rate": "0.55",
        }
    )
    spaced = client._parse_record(
        {
            "roomName": " room ",
            "deviceName": " meter ",
            "time": " 2026-07-29 10:07:07 ",
            "energy": " 0.1 ",
            "money": " 0.06 ",
            "rate": " 0.55 ",
        }
    )

    assert spaced.room_name == " room "
    assert spaced.device_name == " meter "
    assert spaced.unique_key != plain.unique_key


def test_parse_record_rejects_a_structured_upstream_id(client):
    with pytest.raises(PropertyProtocolError):
        client._parse_record(
            {
                "id": {"unexpected": "object"},
                "roomName": "room",
                "deviceName": "meter",
                "time": "2026-07-29 10:07:07",
                "energy": "0.1",
                "money": "0.06",
            }
        )


def test_rejects_an_injected_client_that_follows_redirects(settings):
    with httpx.Client(base_url=BASE_URL, follow_redirects=True) as http:
        with pytest.raises(ValueError, match="redirect"):
            PropertyClient(settings, http=http)


def test_rejects_an_injected_client_without_a_verified_tls_transport(settings):
    class NonTlsTransport(httpx.BaseTransport):
        def handle_request(self, request):
            raise AssertionError("transport must not be used")

    with httpx.Client(base_url=BASE_URL, transport=NonTlsTransport()) as http:
        with pytest.raises(ValueError, match="TLS"):
            PropertyClient(settings, http=http)


def test_rejects_an_injected_client_with_a_mounted_transport(settings):
    class NonTlsTransport(httpx.BaseTransport):
        def handle_request(self, request):
            raise AssertionError("transport must not be used")

    with httpx.Client(base_url=BASE_URL, mounts={BASE_URL: NonTlsTransport()}) as http:
        with pytest.raises(ValueError, match="mount"):
            PropertyClient(settings, http=http)


def test_probe_property_schema_prints_only_schema_summary(monkeypatch, capsys, settings):
    from electricity_app import cli

    record = ElectricityRecord(
        unique_key="detail-1",
        upstream_id="detail-1",
        room_name="sensitive room",
        device_name="sensitive device",
        occurred_at=datetime(2026, 7, 29, 10, 7, 7, tzinfo=ZoneInfo("Asia/Shanghai")),
        energy=Decimal("0.1"),
        money=Decimal("0.06"),
        rate=Decimal("0.55"),
        balance=Decimal("182.66"),
    )

    class FakePropertyClient:
        last_field_names = ("balance", "deviceName", "energy", "id", "money", "rate", "roomName", "time")

        def __init__(self, received_settings):
            assert received_settings is settings

        def fetch_day(self, day):
            return [record]

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "PropertyClient", FakePropertyClient)
    monkeypatch.setattr("sys.argv", ["electricity-admin", "probe-property-schema"])

    cli.main()

    output = capsys.readouterr().out
    assert "field_names=balance,deviceName,energy,id,money,rate,roomName,time" in output
    assert "record_count=1" in output
    assert "sensitive room" not in output
    assert "182.66" not in output
