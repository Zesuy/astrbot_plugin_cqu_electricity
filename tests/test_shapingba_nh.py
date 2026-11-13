import asyncio
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import httpx
import pytest

from cqu_plugin.models import Room, Reading, QueryError
from cqu_plugin.client import ElectricityClient, parse_nh_reading
from cqu_plugin.repository import Repository
from cqu_plugin.service import ElectricityService
from cqu_plugin.scheduler import Scheduler
from cqu_plugin.summary import summary, daily_brief_text

ROOM_B10 = Room("shapingba", "B区10舍", "B10S306")
ROOM_HUXI = Room("huxi", "竹园5栋", "B5344")
IDENTITY = ("qq-test", "123", "1001")
ORIGIN = "qq-test:GroupMessage:123"
BEIJING = timezone(timedelta(hours=8))

LOGIN_SUCCESS = {"code": "0", "msg": "", "SysLoginAcctID": 2054, "SysLoginAcctNT": "B10S306", "username": "B10S306"}
LOGIN_NOT_FOUND = {"code": "-3", "msg": "抱歉,查询不到当前房间号或手机号!"}
ACCT_DATA = [{"F_AcctID": 2054, "PointerName": "B区10号学生宿舍(271)Z", "F_AcctName": "B10S306", "B_F_InitBalance": 80.86, "B_F_InitAllowance": 2.50, "B_F_Totalace": 83.36}]
SACCT_DATA = {"iTotalRecords": 1, "iTotalDisplayRecords": 1, "aaData": [["正在运行", "已通电", "77275", "62000", "16-4", "B10S306(16-4)", "瑞德单相电表(10000)", "1", "B区10舍306", "家属区、学生电价0.54", "0.5400", "2024/5/26 0:00:00", "2026/11/11 12:00:00", "0", "7061", "0", "B区10号学生宿舍(271)Z"]]}
DEBILS_DATA = {
    "dbs": [
        {"index": 0, "F_Month": "2026年10月", "F_UsedDegree": "163", "F_TotalPrice": 88.02},
        {"index": 1, "F_Month": "2026年11月", "F_UsedDegree": "49", "F_TotalPrice": 26.46},
    ]
}
GOUR_DATA = {
    "iTotalRecords": 2,
    "aaData": [
        ["2026/11/11 00:00", "B区10号学生宿舍(271)Z", "瑞德单相电表(10000)", "0", "1.0000", "0.5400", "0.5400"],
        ["2026/11/11 06:00", "B区10号学生宿舍(271)Z", "瑞德单相电表(10000)", "0", "2.0000", "0.5400", "1.0800"],
    ]
}


@pytest.fixture
def repo(tmp_path):
    value = Repository(tmp_path / "nh_test.db")
    yield value
    value.close()


def make_nh_transport(status=200, login_resp=None, acct_resp=None, sacct_resp=None, debils_resp=None, gour_resp=None):
    login = login_resp if login_resp is not None else LOGIN_SUCCESS
    acct = acct_resp if acct_resp is not None else ACCT_DATA
    sacct = sacct_resp if sacct_resp is not None else SACCT_DATA
    debils = debils_resp if debils_resp is not None else DEBILS_DATA
    gour = gour_resp if gour_resp is not None else GOUR_DATA

    def handle(request):
        url = str(request.url)
        if "/df/Admin/Login" in url:
            return httpx.Response(status, json=login)
        elif "/df/Home/GetAcctDs" in url:
            return httpx.Response(status, json=acct)
        elif "/df/Home/GetSAcctDs" in url:
            return httpx.Response(status, json=sacct)
        elif "/df/Home/GetDeBils" in url:
            return httpx.Response(status, json=debils)
        elif "/df/Home/Get48GourDs" in url:
            return httpx.Response(status, json=gour)
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def test_parse_nh_reading_valid():
    reading = parse_nh_reading(ROOM_B10, ACCT_DATA, SACCT_DATA, Decimal("0.54"))
    assert reading.cash_balance_yuan == Decimal("80.86")
    assert reading.subsidy_balance_yuan == Decimal("2.50")
    assert reading.meter_reading_kwh == Decimal("7061")
    assert reading.unit_price_yuan == Decimal("0.5400")
    assert reading.resolved_building == "B区10舍"
    fields = reading.fields()
    assert Decimal(fields["total_balance_yuan"]) == Decimal("83.36")


def test_parse_nh_reading_room_mismatch():
    mismatched = [{"F_AcctID": 2054, "F_AcctName": "B3S624", "B_F_InitBalance": 10.0}]
    with pytest.raises(QueryError, match="ROOM_MISMATCH"):
        parse_nh_reading(ROOM_B10, mismatched, SACCT_DATA, Decimal("0.54"))


def test_parse_nh_reading_invalid_structure():
    with pytest.raises(QueryError, match="INVALID_RESPONSE"):
        parse_nh_reading(ROOM_B10, [], SACCT_DATA, Decimal("0.54"))
    with pytest.raises(QueryError, match="INVALID_RESPONSE"):
        parse_nh_reading(ROOM_B10, "not a list", SACCT_DATA, Decimal("0.54"))


def test_parse_nh_reading_missing_device_falls_back():
    empty_sacct = {"iTotalRecords": 0, "aaData": []}
    reading = parse_nh_reading(ROOM_B10, ACCT_DATA, empty_sacct, Decimal("0.60"))
    assert reading.cash_balance_yuan == Decimal("80.86")
    assert reading.meter_reading_kwh is None
    assert reading.unit_price_yuan == Decimal("0.60")
    assert reading.resolved_building == "B区10舍"


def test_parse_nh_reading_with_debils_and_gour():
    now_bj = datetime(2026, 11, 11, 14, 0, 0, tzinfo=BEIJING)
    reading = parse_nh_reading(ROOM_B10, ACCT_DATA, SACCT_DATA, Decimal("0.54"), debils_data=DEBILS_DATA, gour_data=GOUR_DATA, now_bj=now_bj)
    assert reading.monthly_kwh == Decimal("49")
    assert reading.monthly_cost_yuan == Decimal("26.46")
    assert reading.monthly_name == "2026年11月"
    assert reading.today_kwh == Decimal("3.0000")
    assert reading.today_cost_yuan == Decimal("1.6200")
    assert reading.today_point == "06:00"

    fields = reading.fields()
    assert fields["monthly_kwh"] == "49"
    assert fields["monthly_cost_yuan"] == "26.46"
    assert fields["monthly_name"] == "2026年11月"
    assert fields["today_kwh"] == "3.0000"
    assert fields["today_cost_yuan"] == "1.6200"
    assert fields["today_point"] == "06:00"

    fake_result = {
        "ok": True,
        "room": {"campus": "shapingba", "building": "B区10舍", "room_number": "B10S306"},
        "reading": {**fields, "finished_at": "2026-11-11T06:00:00+00:00"},
    }
    normal_text = summary(fake_result, detail=False)
    assert "今日用量" not in normal_text
    assert "本月累计" not in normal_text

    detail_text = summary(fake_result, detail=True)
    assert "今日用量：约 3.00 度（今日零点至 06:00） · 约 1.62 元" in detail_text
    assert "本月累计：49.00 度 · 电费 26.46 元（2026年11月）" in detail_text

    fake_brief_result = {**fake_result, "daily_brief": {"state": "ready", "estimate": None}}
    brief_text = daily_brief_text(fake_brief_result, detail=True)
    assert "今日用量：约 3.00 度（今日零点至 06:00） · 约 1.62 元" in brief_text
    assert "本月累计：49.00 度 · 电费 26.46 元（2026年11月）" in brief_text


def test_huxi_campus_detail_unaffected():
    fake_huxi = {
        "ok": True,
        "room": {"campus": "huxi", "building": "竹园5栋", "room_number": "B5344"},
        "reading": {
            "finished_at": "2026-11-11T06:00:00+00:00",
            "cash_balance_yuan": "50.00",
            "subsidy_kwh": None,
            "subsidy_balance_yuan": None,
            "total_balance_yuan": "50.00",
            "meter_reading_kwh": "100.00",
            "unit_price_yuan": "0.54",
            "today_kwh": None,
            "today_cost_yuan": None,
            "today_point": None,
            "monthly_kwh": None,
            "monthly_cost_yuan": None,
            "monthly_name": None,
        },
    }
    detail_text = summary(fake_huxi, detail=True)
    assert "今日用量" not in detail_text
    assert "本月累计" not in detail_text
    assert "校区：虎溪" in detail_text


def test_nh_fetch_success_without_auth(repo, tmp_path):
    client = ElectricityClient(tmp_path / "missing", nh_enabled=True, transport=make_nh_transport())
    assert client.configured() is False
    assert client.configured("shapingba") is True

    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    result = asyncio.run(service.query(room, force=True))
    assert result["ok"]
    assert result["reading"]["cash_balance_yuan"] == "80.86"
    assert result["reading"]["meter_reading_kwh"] == "7061"
    assert result["reading"]["monthly_name"] == "2026年11月"
    assert result["reading"]["monthly_kwh"] == "49"

    responses = repo.all("SELECT stage, http_status FROM query_responses ORDER BY sequence_no")
    assert len(responses) == 5
    assert [r["stage"] for r in responses] == ["nh_login", "nh_account", "nh_device", "nh_debils", "nh_48h"]


def test_nh_fetch_room_not_found(repo, tmp_path):
    client = ElectricityClient(tmp_path / "missing", nh_enabled=True, transport=make_nh_transport(login_resp=LOGIN_NOT_FOUND))
    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    result = asyncio.run(service.query(room, force=True))
    assert not result["ok"]
    assert result["error_code"] == "ROOM_NOT_FOUND"


def test_nh_fetch_http_error(repo, tmp_path):
    client = ElectricityClient(tmp_path / "missing", nh_enabled=True, transport=make_nh_transport(status=500))
    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    result = asyncio.run(service.query(room, force=True))
    assert not result["ok"]
    assert result["error_code"] == "HTTP_ERROR"


def test_nh_fallback_to_payment_when_configured(repo, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"access_token": "valid-token"}))

    def hybrid_handler(request):
        url = str(request.url)
        if "nh.cqu.edu.cn" in url:
            return httpx.Response(500)
        form = dict(httpx.QueryParams(request.content.decode()))
        if form["level"] == "0":
            data = {"total": [{"code": "b"}, {"code": "r"}], "data": [{"name": "B区10号学生宿舍", "value": "b10"}]}
        elif form["type"] == "select":
            data = {"data": [{"name": "B10S306", "value": "r306"}]}
        else:
            data = {"data": {"roomID": "B10S306", "cashBalance": "12.34", "subsidiesBalance": "0", "price": "0.54"}, "showData": {"现金余额": "12.34"}}
        return httpx.Response(200, json={"code": 200, "map": data})

    client = ElectricityClient(auth_file, nh_enabled=True, transport=httpx.MockTransport(hybrid_handler))
    assert client.configured() is True
    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    result = asyncio.run(service.query(room, force=True))
    print("FALLBACK RESULT:", result)
    assert result["ok"]
    assert result["reading"]["cash_balance_yuan"] == "12.34"
    assert result["reading"]["meter_reading_kwh"] is None


def test_scheduler_runs_for_shapingba_without_token(repo, tmp_path):
    client = ElectricityClient(tmp_path / "missing", nh_enabled=True, transport=make_nh_transport())
    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    repo.bind(IDENTITY, room["id"], ORIGIN)

    async def send(*args):
        pass

    scheduler = Scheduler(service, lambda *x: True, send, capture_times=("09:00",), reminder_enabled=False)
    rooms = scheduler._eligible_rooms()
    assert room["id"] in rooms


def test_nh_account_response_sensitive_data_redacted(repo, tmp_path):
    sensitive_acct = [{
        "F_AcctID": 2054,
        "PointerName": "B区10号学生宿舍(271)Z",
        "F_AcctName": "B10S306",
        "B_F_InitBalance": 80.86,
        "B_F_InitAllowance": 2.50,
        "B_F_Totalace": 83.36,
        "F_UserName": "张三",
        "F_Addr": "沙坪坝区沙正街174号重大B区",
        "F_IdNo": "500106199901011234",
        "F_CellPhone": "13800138000",
        "F_SalaryAcct": "6222021234567890",
        "F_HomePhone": "02365100000",
        "F_DeptName": "大数据与软件学院",
    }]
    client = ElectricityClient(tmp_path / "missing", nh_enabled=True, transport=make_nh_transport(acct_resp=sensitive_acct))
    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    result = asyncio.run(service.query(room, force=True))
    assert result["ok"]

    acct_resp = repo.one("SELECT raw_body FROM query_responses WHERE stage='nh_account'")
    assert acct_resp is not None
    raw = acct_resp["raw_body"]
    raw_text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
    # Sensitive personal information must NEVER be persisted in plaintext
    assert "张三" not in raw_text
    assert "沙正街174号" not in raw_text
    assert "500106199901011234" not in raw_text
    assert "13800138000" not in raw_text
    assert "6222021234567890" not in raw_text
    assert "02365100000" not in raw_text
    assert "大数据与软件学院" not in raw_text
    assert "[REDACTED]" in raw_text

    # Non-sensitive essential billing data must be preserved
    assert "80.86" in raw_text
    assert "B10S306" in raw_text


def test_nh_optional_stats_timeout_preserves_core_reading(repo, tmp_path):
    def slow_stats_handler(request):
        url = str(request.url)
        if "/df/Admin/Login" in url:
            return httpx.Response(200, json=LOGIN_SUCCESS)
        elif "/df/Home/GetAcctDs" in url:
            return httpx.Response(200, json=ACCT_DATA)
        elif "/df/Home/GetSAcctDs" in url:
            return httpx.Response(200, json=SACCT_DATA)
        elif "/df/Home/GetDeBils" in url or "/df/Home/Get48GourDs" in url:
            # Simulate endpoint timeout by returning 504 / timeout
            raise httpx.ReadTimeout("Server too slow")
        return httpx.Response(404)

    client = ElectricityClient(tmp_path / "missing", timeout=5, nh_enabled=True, transport=httpx.MockTransport(slow_stats_handler))
    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    result = asyncio.run(service.query(room, force=True))
    # Core balance and meter reading must be returned successfully rather than discarded!
    assert result["ok"]
    assert result["reading"]["cash_balance_yuan"] == "80.86"
    assert result["reading"]["meter_reading_kwh"] == "7061"
    assert result["reading"]["today_kwh"] is None
    assert result["reading"]["monthly_kwh"] is None


def test_parse_nh_reading_no_data_does_not_fill_zero():
    # Empty aaData (e.g. 02:00 morning before first point or network empty)
    empty_gour = {"iTotalRecords": 0, "aaData": []}
    now_bj = datetime(2026, 11, 11, 14, 0, 0, tzinfo=BEIJING)
    reading = parse_nh_reading(ROOM_B10, ACCT_DATA, SACCT_DATA, Decimal("0.54"), gour_data=empty_gour, now_bj=now_bj)
    # Must remain None, never fabricated as Decimal("0")
    assert reading.today_kwh is None
    assert reading.today_cost_yuan is None
    assert reading.today_point is None

    fake_result = {
        "ok": True,
        "room": {"campus": "shapingba", "building": "B区10舍", "room_number": "B10S306"},
        "reading": {**reading.fields(), "finished_at": "2026-11-11T06:00:00+00:00"},
    }
    detail_text = summary(fake_result, detail=True)
    assert "今日用量" not in detail_text


def test_summary_cross_day_cache_and_month_label():
    # 1. Yesterday snapshot: should display "11月10日用量" instead of "今日用量"
    yesterday_reading = {
        "finished_at": "2026-11-10T14:00:00+00:00",
        "cash_balance_yuan": "80.86",
        "subsidy_kwh": None,
        "subsidy_balance_yuan": "2.50",
        "total_balance_yuan": "83.36",
        "meter_reading_kwh": "7061",
        "unit_price_yuan": "0.54",
        "today_kwh": "2.0000",
        "today_cost_yuan": "1.0800",
        "today_point": "12:00",
        "monthly_kwh": "163",
        "monthly_cost_yuan": "88.02",
        "monthly_name": "2026年10月",
    }
    fake_result = {
        "ok": True,
        "room": {"campus": "shapingba", "building": "B区10舍", "room_number": "B10S306"},
        "reading": yesterday_reading,
    }
    detail_text = summary(fake_result, detail=True)
    # Since finished_at is 11-10 and current date is 11-11, it should show 11月10日用量
    assert "今日用量" not in detail_text
    assert "11月10日用量：约 2.00 度（11月10日零点至 12:00） · 约 1.08 元" in detail_text
    # Since monthly_name is 2026年10月 (not current November), it should show 最近账单
    assert "本月累计" not in detail_text
    assert "最近账单：163.00 度 · 电费 88.02 元（2026年10月）" in detail_text


def test_scheduler_mixed_campus_without_token_filters_huxi(repo, tmp_path):
    client = ElectricityClient(tmp_path / "missing", nh_enabled=True, transport=make_nh_transport())
    service = ElectricityService(repo, client)

    room_shapingba = repo.room(ROOM_B10)
    room_huxi = repo.room(ROOM_HUXI)
    repo.bind(IDENTITY, room_shapingba["id"], ORIGIN)
    repo.bind(("qq-test", "123", "1002"), room_huxi["id"], "qq-test:GroupMessage:123")

    async def send(*args):
        pass

    scheduler = Scheduler(service, lambda *x: True, send, capture_times=("09:00",), reminder_enabled=False)
    rooms = scheduler._eligible_rooms()
    # Shapingba room is eligible without token
    assert room_shapingba["id"] in rooms
    # Huxi room is NOT eligible without token and MUST be filtered out
    assert room_huxi["id"] not in rooms
