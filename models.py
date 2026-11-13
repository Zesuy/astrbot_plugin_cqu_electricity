from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

GARDENS = {"梅园":"A", "竹园":"B", "松园":"C", "兰园":"D", "荷园":"F"}


def building_name(value):
    value=str(value).strip()
    digits={"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9}
    match=re.fullmatch(r"(.+?)([0-9]+|[一二两三四五六七八九十]+)(?:栋|幢)",value)
    if not match: return value
    prefix,number=match.groups()
    if number.isdigit():
        parsed=int(number)
    elif "十" in number:
        parts=number.split("十")
        if len(parts)!=2 or len(parts[0])>1 or len(parts[1])>1: return value
        parsed=(digits.get(parts[0],1) if parts[0] else 1)*10+(digits.get(parts[1],0) if parts[1] else 0)
    elif number in digits:
        parsed=digits[number]
    else: return value
    return f"{prefix}{parsed}栋"


def shapingba_building(value):
    match=re.match(r"^([ABC])区([1-9][0-9]?)(?:号|舍|栋)",re.sub(r"\s+","",str(value)).upper())
    return (match[1],int(match[2])) if match else None


def shapingba_account(account):
    value=str(account).strip().upper().replace("-","F").replace("－","F").replace("−","F").replace("负","F")
    match=re.fullmatch(r"([ABC])([1-9][0-9]?)S(F?[0-9]+(?:F[0-9]+)*)",value)
    if not match: return None
    area,building,suffix=match[1],int(match[2]),match[3]
    if len(value)>32: raise ValueError("房间账号过长。")
    if area=="A" and building==12 and not re.fullmatch(r"[1-9][0-9F]{3,}",suffix):
        raise ValueError("A区12舍需在房号前加单元号，例如 3单元112 或 A12S3112。")
    return area,building,suffix,value


def shapingba_room(building, room):
    identity=shapingba_building(building)
    if not identity: raise ValueError("沙坪坝楼栋请填写 A区1舍、B区10舍等格式。")
    area,number=identity
    value=str(room).strip().upper()
    encoded=shapingba_account(value)
    if encoded:
        if encoded[:2]!=identity: raise ValueError("房间账号与宿舍区域或楼栋不一致。")
        return encoded[3]
    if re.match(r"^[A-Z]+[0-9]+S",value): raise ValueError("沙坪坝房间账号格式不正确。")
    value=re.sub(r"(?:寝室|室)$","",value).replace("-","F").replace("－","F").replace("−","F").replace("负","F")
    if area=="A" and number==12:
        unit=re.fullmatch(r"([1-9])单元(.+)",value)
        if unit: value=unit[1]+unit[2]
        if not re.fullmatch(r"[1-9][0-9F]{3,}",value):
            raise ValueError("A区12舍需提供单元号，例如 3单元112。")
    elif area=="B" and number==12 and re.fullmatch(r"12[0-9]{3}",value):
        value=value[2:]
    elif area=="B" and number==7 and re.fullmatch(r"7[0-9]{3}",value):
        value=value[1:]
    encoded=shapingba_account(f"{area}{number}S{value}")
    if not encoded: raise ValueError("沙坪坝房号格式不正确；负号或连接号使用 F。")
    return encoded[3]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def decimal_text(value):
    if value is None: return None
    if isinstance(value, Decimal): return format(value, "f")
    return str(value)


def number(value):
    if isinstance(value, bool):
        raise ValueError("invalid number")
    match = re.fullmatch(r"\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:元|度|kWh|千瓦时)?\s*", str(value).replace(",", ""), re.I)
    if not match:
        raise ValueError("invalid number")
    result = Decimal(match.group(1))
    if not result.is_finite():
        raise ValueError("invalid number")
    return result


@dataclass(frozen=True)
class Room:
    campus: str
    building: str
    room_number: str

    @classmethod
    def validated(cls, campus, building, room):
        campus = {"虎溪": "huxi", "沙坪坝": "shapingba"}.get(str(campus), str(campus).lower())
        building, room = building_name(building), str(room).strip().upper()
        if campus=="shapingba":
            room=shapingba_room(building,room)
            area,number=shapingba_building(building)
            building=f"{area}区{number}舍"
        if campus=="huxi":
            regular=re.fullmatch(r"(梅园|竹园|松园|兰园|荷园)([1-9][0-9]?)栋",building)
            chrysanthemum=re.fullmatch(r"菊园([AB])([1-3])栋",building)
            if regular:
                prefix=GARDENS[regular[1]]+regular[2]
                if room.isdigit(): room=prefix+room
                elif not room.startswith(prefix): raise ValueError("房间账号与园区或楼栋不一致。")
            elif chrysanthemum:
                prefix="E"+chrysanthemum[1]
                if room.isdigit(): room=prefix+room
                elif not room.startswith(prefix): raise ValueError("房间账号与菊园区域不一致。")
        if campus not in {"huxi", "shapingba"} or not building or len(building) > 80:
            raise ValueError("校区应为虎溪或沙坪坝，并填写楼栋名称。")
        if not re.fullmatch(r"[A-Z0-9-]{1,32}", room) or any(ord(c) < 32 for c in building):
            raise ValueError("房号格式不正确。")
        return cls(campus, building, room)

    @classmethod
    def from_account(cls, account):
        account=str(account).strip().upper()
        encoded=shapingba_account(account)
        if encoded:
            area,building,suffix,account=encoded
            return cls.validated("shapingba",f"{area}区{building}舍",account)
        match=re.fullmatch(r"([ABCDF])([1-9])([0-9]{3,4})",account)
        if match:
            garden=next(k for k,v in GARDENS.items() if v==match[1])
            return cls.validated("huxi",garden+match[2]+"栋",account)
        match=re.fullmatch(r"E([AB])([0-9]{3,4})",account)
        if match:
            # No building number is encoded in this format: resolve through the API.
            return cls("huxi","菊园"+match[1]+"区",account)
        raise ValueError("请填写完整账号，例如 A1101 或 B10S306；也可填写校区、楼栋和房号。")


@dataclass(frozen=True)
class Reading:
    cash_balance_yuan: Decimal
    subsidy_kwh: Decimal | None = None
    subsidy_balance_yuan: Decimal | None = None
    meter_reading_kwh: Decimal | None = None
    unit_price_yuan: Decimal = Decimal("0.54")
    resolved_building: str | None = None
    today_kwh: Decimal | None = None
    today_cost_yuan: Decimal | None = None
    today_point: str | None = None
    monthly_kwh: Decimal | None = None
    monthly_cost_yuan: Decimal | None = None
    monthly_name: str | None = None

    def fields(self):
        result = {k: decimal_text(v) for k, v in asdict(self).items() if k!="resolved_building"}
        result["total_balance_yuan"] = decimal_text(self.cash_balance_yuan + (self.subsidy_kwh or Decimal(0)) * self.unit_price_yuan + (self.subsidy_balance_yuan or Decimal(0)))
        return result


class QueryError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


ERRORS = {
    "NO_SAVED_READING": "暂无已保存的电费采集记录。",
    "NO_AUTH": "尚未配置缴费平台认证，请联系管理员。",
    "AUTH_EXPIRED": "缴费平台拒绝当前登录，请联系管理员检查或更新认证。",
    "AUTH_INVALID": "认证文件格式不正确，请联系管理员。",
    "BUILDING_NOT_FOUND": "缴费平台找不到指定楼栋，请核对楼栋名称。",
    "ROOM_NOT_FOUND": "指定楼栋中找不到该房号，请核对后重试。",
    "AMBIGUOUS_ROOM": "宿舍定位不唯一，请填写准确楼栋和房号，例如 /电费 绑定 菊园A1栋 EA612。",
    "ROOM_MISMATCH": "接口返回的宿舍不一致，本次数据未采用。",
    "TIMEOUT": "缴费平台响应超时，请稍后重试。",
    "NETWORK": "暂时无法连接缴费平台，请稍后重试。",
    "HTTP_ERROR": "缴费平台暂时无法提供数据，请稍后重试。",
    "INVALID_RESPONSE": "缴费平台响应格式异常，原始数据已记录。",
    "PARSE_ERROR": "暂时无法解析电费数据，原始数据已记录。",
    "RESPONSE_TOO_LARGE": "缴费平台响应超出限制。",
    "INTERNAL": "查询暂时失败，请稍后重试。",
    "RATE_LIMITED": "查询较频繁，请稍后重试。",
}
