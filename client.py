from __future__ import annotations

# Query protocol and field aliases adapted from ChrisKimZHT/CQU-Electricity-Bill.
# Copyright (c) 2026 Haotian Zou; MIT license, see LICENSE.
import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from dataclasses import replace
from html import unescape, escape
from pathlib import Path
from urllib.parse import quote
import httpx
from .models import Room, Reading, QueryError, number, building_name, shapingba_building

BEIJING = timezone(timedelta(hours=8))
HOST = "payment.cqu.edu.cn"
NH_HOST = "nh.cqu.edu.cn:26989"
SENSITIVE = {
    "token", "accesstoken", "refreshtoken", "authorization", "synjonesauth", "cookie", "setcookie", "password", "secret", "appsecret",
    "fusername", "username", "faddr", "addr", "fidno", "idno", "fcellphone", "cellphone", "phone", "mobile",
    "fsalaryacct", "salaryacct", "fhomephone", "homephone", "fidname", "idname", "idcard", "realname",
    "fdept", "dept", "fdeptname", "deptname", "fmemo", "memo",
}

NH_ACCOUNT_ALLOWED = {
    "f_acctid", "f_buildingid", "pointername", "f_opendate", "f_opendatestr", "f_closedate",
    "f_openstate", "f_initbalance", "f_initallowance", "f_notifytimes", "f_order", "f_prepay",
    "f_acctname", "f_acctpy", "b_f_totalace", "b_f_totaldegree", "b_f_initallowance",
    "b_f_initbalance", "b_f_balancedegree", "eprid", "eprname", "f_accttype", "f_accttypent",
}


def redact_body(body, token, stage=None):
    # Preserve response bytes when no secret is present. Never store request headers.
    target_values = {token, quote(token, safe=""), escape(token)} - {""}
    for value in target_values:
        body = body.replace(value.encode(), b"[REDACTED]")
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return re.sub(rb'(?i)((?:access_token|refresh_token|password|authorization|synjones-auth|f_username|f_addr|f_idno|f_cellphone|f_salaryacct|f_homephone)[\s"\']*[:=][\s"\']*)([^"\'<>\s,}]+)', rb'\1[REDACTED]', body)
    changed = False
    def clean(item, is_account_stage=False):
        nonlocal changed
        if isinstance(item, dict):
            result = {}
            for key, val in item.items():
                norm = re.sub(r"[^a-z0-9_]", "", key.lower())
                norm_az = re.sub(r"[^a-z]", "", key.lower())
                if is_account_stage and norm not in NH_ACCOUNT_ALLOWED:
                    result[key] = "[REDACTED]"
                    changed = True
                elif norm_az in SENSITIVE:
                    result[key] = "[REDACTED]"
                    changed = True
                else:
                    result[key] = clean(val, is_account_stage)
            return result
        if isinstance(item, list):
            return [clean(v, is_account_stage) for v in item]
        if isinstance(item, str):
            res_str = item
            for tv in target_values:
                if tv in res_str:
                    res_str = res_str.replace(tv, "[REDACTED]")
                    changed = True
            return res_str
        return item
    cleaned = clean(value, is_account_stage=(stage == "nh_account"))
    return json.dumps(cleaned, ensure_ascii=False).encode() if changed else body


def load_token(path, session=""):
    try:
        token = ""
        if isinstance(session, str):
            token = session.strip()
        elif session is not None:
            raise ValueError("session must be a string")
        if not token:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            token = data.get("access_token", "") if isinstance(data, dict) else ""
        if not isinstance(token, str):
            raise ValueError()
        token = token.strip()
        pairs = {("'", "'"), ('"', '"'), ("‘", "’"), ("“", "”")}
        while len(token) >= 2 and (token[0], token[-1]) in pairs:
            token = token[1:-1].strip()
        token = re.sub(r"^bearer\s+", "", token, flags=re.I).strip()
        while len(token) >= 2 and (token[0], token[-1]) in pairs:
            token = token[1:-1].strip()
        if not token:
            raise QueryError("NO_AUTH")
        if not token.isascii():
            raise ValueError("Non-ASCII character in token")
        if any(c in token for c in "'\"‘’“”"):
            raise ValueError("Unmatched quotes in token")
        if ";" in token:
            raise ValueError("Semicolon in token")
        if any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise ValueError("Control character in token")
        if len(token) > 16384 or any(c.isspace() for c in token):
            raise ValueError("Whitespace or excessive length in token")
        return token
    except FileNotFoundError:
        raise QueryError("NO_AUTH") from None
    except QueryError:
        raise
    except (OSError, ValueError):
        raise QueryError("AUTH_INVALID") from None


def parse_reading(room, response_map, price):
    data, display = response_map.get("data") or {}, response_map.get("showData") or {}
    if not isinstance(data, dict) or not isinstance(display, (dict,list)):
        raise QueryError("PARSE_ERROR")
    try:
        if room.campus == "shapingba":
            if str(data.get("roomID", "")).strip().upper() != room.room_number:
                raise QueryError("ROOM_MISMATCH")
            unit = number(data.get("price", display.get("电价")))
            if unit <= 0:
                raise ValueError()
            return Reading(number(data.get("cashBalance",display.get("现金余额"))),
                           subsidy_balance_yuan=number(data.get("subsidiesBalance",display.get("补贴余额"))), unit_price_yuan=unit)
        for key in ("room", "roomname", "roomID", "fjmc", "accountname"):
            if data.get(key) and str(data[key]).strip().upper() != room.room_number:
                raise QueryError("ROOM_MISMATCH")
        # The upstream API can provide direct fields or labelled entries.
        values = {}
        def visit(item):
            if isinstance(item, dict):
                label = item.get("name") or item.get("label") or item.get("title") or item.get("text")
                val = item.get("value", item.get("data"))
                if label and val is not None and not isinstance(val,(list,dict)):
                    values[str(label).strip().lower()] = val
                for key, val in item.items():
                    if not isinstance(val,(dict,list)) and val not in (None,""):
                        values[str(key).strip().lower()] = val
                    else:
                        visit(val)
            elif isinstance(item,list):
                for child in item: visit(child)
        visit(data); visit(display)
        def pick(names, required=False):
            for name in names:
                if name in values: return number(unescape(str(values[name])))
            if required: raise ValueError()
            return None
        cash = pick(("balance","money","surplusmoney","remainingamount","剩余金额","余额"), True)
        meter = pick(("meterreading","reading","electricreading","usedegree","quantity","电表读数","电量读数"))
        subsidy = pick(("subsidy","electricallowance","surplusdegree","电剩余补助（度）","电剩余补助"))
        if price <= 0 or (meter is not None and meter < 0) or (subsidy is not None and subsidy < 0):
            raise ValueError()
        return Reading(cash, subsidy_kwh=subsidy, meter_reading_kwh=meter, unit_price_yuan=price)
    except QueryError:
        raise
    except (ValueError,TypeError,AttributeError):
        raise QueryError("PARSE_ERROR") from None


def parse_nh_reading(room, acct_data, sacct_data, default_price=Decimal("0.54"), debils_data=None, gour_data=None, now_bj=None):
    if not isinstance(acct_data, list) or not acct_data:
        raise QueryError("INVALID_RESPONSE")
    acct = acct_data[0]
    if not isinstance(acct, dict):
        raise QueryError("INVALID_RESPONSE")
    acct_name = str(acct.get("F_AcctName") or "").strip().upper()
    if acct_name and acct_name != room.room_number:
        raise QueryError("ROOM_MISMATCH")
    try:
        cash = number(acct.get("B_F_InitBalance"))
        subsidy = number(acct.get("B_F_InitAllowance", 0))
    except (ValueError, TypeError):
        raise QueryError("PARSE_ERROR") from None

    aa_data = sacct_data.get("aaData") if isinstance(sacct_data, dict) else None
    meter_reading = None
    unit_price = default_price
    building_raw = str(acct.get("PointerName") or "")

    if isinstance(aa_data, list) and aa_data:
        row = aa_data[0]
        if isinstance(row, list):
            try:
                if len(row) > 10 and row[10] not in (None, ""):
                    unit_price = number(row[10])
                if len(row) > 14 and row[14] not in (None, ""):
                    meter_reading = number(row[14])
                if len(row) > 16 and row[16] not in (None, ""):
                    building_raw = str(row[16])
            except (ValueError, TypeError):
                raise QueryError("PARSE_ERROR") from None

    monthly_kwh, monthly_cost_yuan, monthly_name = None, None, None
    if isinstance(debils_data, dict):
        dbs = debils_data.get("dbs")
        if isinstance(dbs, list) and dbs:
            last_db = dbs[-1]
            if isinstance(last_db, dict):
                try:
                    if last_db.get("F_UsedDegree") not in (None, ""):
                        monthly_kwh = number(last_db.get("F_UsedDegree"))
                    if last_db.get("F_TotalPrice") not in (None, ""):
                        monthly_cost_yuan = number(last_db.get("F_TotalPrice"))
                    if last_db.get("F_Month") not in (None, ""):
                        monthly_name = str(last_db.get("F_Month")).strip()
                except (ValueError, TypeError):
                    monthly_kwh, monthly_cost_yuan, monthly_name = None, None, None

    today_kwh, today_cost_yuan, today_point = None, None, None
    if isinstance(gour_data, dict):
        gour_rows = gour_data.get("aaData")
        if isinstance(gour_rows, list):
            if now_bj is None:
                now_bj = datetime.now(BEIJING)
            today_slash = now_bj.strftime("%Y/%m/%d")
            today_dash = now_bj.strftime("%Y-%m-%d")
            today_rows = [
                row for row in gour_rows
                if isinstance(row, list) and len(row) > 6 and (str(row[0]).startswith(today_slash) or str(row[0]).startswith(today_dash))
            ]
            if today_rows:
                try:
                    today_kwh = sum((number(r[4]) for r in today_rows if r[4] not in (None, "")), Decimal(0))
                    today_cost_yuan = sum((number(r[6]) for r in today_rows if r[6] not in (None, "")), Decimal(0))
                    last_t = str(today_rows[-1][0]).strip()
                    today_point = last_t.split()[-1] if " " in last_t else last_t
                except (ValueError, TypeError):
                    today_kwh, today_cost_yuan, today_point = None, None, None

    id_info = shapingba_building(building_raw) or shapingba_building(room.building)
    resolved = f"{id_info[0]}区{id_info[1]}舍" if id_info else room.building
    return Reading(
        cash_balance_yuan=cash,
        subsidy_balance_yuan=subsidy,
        meter_reading_kwh=meter_reading,
        unit_price_yuan=unit_price,
        resolved_building=resolved,
        today_kwh=today_kwh,
        today_cost_yuan=today_cost_yuan,
        today_point=today_point,
        monthly_kwh=monthly_kwh,
        monthly_cost_yuan=monthly_cost_yuan,
        monthly_name=monthly_name,
    )


class ElectricityClient:
    def __init__(self, token_path, *, session="", scheme="https", timeout=40, price="0.54", transport=None,
                 nh_enabled=False, nh_host=NH_HOST, nh_scheme="http"):
        if scheme not in {"http", "https"}: raise ValueError("Invalid scheme")
        if nh_scheme not in {"http", "https"}: raise ValueError("Invalid scheme")
        self.token_path, self.scheme = token_path, scheme
        self.session = session
        self.timeout, self.price = max(1,min(float(timeout),90)), number(price)
        if self.price <= 0: raise ValueError("Invalid price")
        self.transport = transport
        self.nh_enabled = bool(nh_enabled)
        self.nh_host = nh_host.strip()
        self.nh_scheme = nh_scheme.strip()

    def configured(self, campus=None):
        if campus == "shapingba" and self.nh_enabled:
            return True
        try:
            load_token(self.token_path, self.session)
            return True
        except QueryError:
            return False

    async def _fetch_shapingba_nh(self, room, record):
        url_base = f"{self.nh_scheme}://{self.nh_host}"
        headers = {"User-Agent": "AstrBot-CQU-Electricity/0.1.7"}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=min(15, self.timeout), follow_redirects=False, transport=self.transport) as http:
                async def request(stage, method, path, *, data=None, params=None, req_timeout=None):
                    t = req_timeout or min(15.0, self.timeout)
                    async with http.stream(method, f"{url_base}{path}", data=data, params=params, timeout=t) as response:
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > 2 * 1024 * 1024:
                                record(stage, response.status_code, response.headers.get("content-type", ""), redact_body(b"".join(chunks), "", stage=stage))
                                raise QueryError("RESPONSE_TOO_LARGE")
                        body = b"".join(chunks)
                        record(stage, response.status_code, response.headers.get("content-type", ""), redact_body(body, "", stage=stage))
                        if response.status_code >= 400:
                            raise QueryError("HTTP_ERROR")
                        try:
                            return json.loads(body)
                        except (ValueError, UnicodeError):
                            raise QueryError("INVALID_RESPONSE") from None

                # Core data: login, account balance, and device meter reading.
                # Strictly guarded by self.timeout.
                async with asyncio.timeout(self.timeout):
                    login_data = await request("nh_login", "POST", "/df/Admin/Login", data={"username": room.room_number})
                    if not isinstance(login_data, dict):
                        raise QueryError("INVALID_RESPONSE")
                    if str(login_data.get("code")) == "-3":
                        raise QueryError("ROOM_NOT_FOUND")
                    if str(login_data.get("code")) != "0":
                        raise QueryError("INVALID_RESPONSE")
                    acct_id = str(login_data.get("SysLoginAcctID") or "").strip()
                    if not acct_id:
                        raise QueryError("INVALID_RESPONSE")

                    acct_data = await request("nh_account", "GET", "/df/Home/GetAcctDs", params={"SysLoginAcctID": acct_id})
                    sacct_data = await request("nh_device", "GET", "/df/Home/GetSAcctDs", params={"sEcho": 1, "iDisplayStart": 0, "iDisplayLength": 30, "sSearch": acct_id})

                # Core data is now secured. Optional stats (debils, 48h) run with an isolated,
                # short timeout budget (3.0s each). Any timeout or error in optional endpoints
                # must never drop or fail the already-successful base reading.
                debils_data, gour_data = None, None
                try:
                    async with asyncio.timeout(3.0):
                        debils_data = await request("nh_debils", "GET", "/df/Home/GetDeBils", params={"SysLoginAcctID": acct_id}, req_timeout=3.0)
                except (TimeoutError, httpx.TimeoutException, QueryError, Exception):
                    debils_data = None

                try:
                    async with asyncio.timeout(3.0):
                        gour_data = await request("nh_48h", "GET", "/df/Home/Get48GourDs", params={"sEcho": 1, "iDisplayStart": 0, "iDisplayLength": 200, "sSearch": acct_id}, req_timeout=3.0)
                except (TimeoutError, httpx.TimeoutException, QueryError, Exception):
                    gour_data = None

                return parse_nh_reading(room, acct_data, sacct_data, self.price, debils_data=debils_data, gour_data=gour_data)
        except QueryError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise QueryError("TIMEOUT") from None
        except httpx.HTTPError:
            raise QueryError("NETWORK") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise QueryError("INVALID_RESPONSE") from None

    async def fetch(self, room, save_response):
        seq = 0
        def record(stage, status, content_type, body):
            nonlocal seq
            seq += 1
            save_response(seq, stage, status, content_type, body)

        if room.campus == "shapingba" and self.nh_enabled:
            try:
                return await self._fetch_shapingba_nh(room, record)
            except QueryError as exc:
                if self.configured() and exc.code not in {"ROOM_NOT_FOUND", "RESPONSE_TOO_LARGE", "ROOM_MISMATCH"}:
                    pass
                else:
                    raise
            except (TimeoutError, httpx.TimeoutException):
                if self.configured():
                    pass
                else:
                    raise QueryError("TIMEOUT") from None
            except httpx.HTTPError:
                if self.configured():
                    pass
                else:
                    raise QueryError("NETWORK") from None
            except Exception:
                if self.configured():
                    pass
                else:
                    raise QueryError("INVALID_RESPONSE") from None

        token = load_token(self.token_path, self.session)
        authorization = "bearer " + token
        headers = {"synjones-auth":authorization,"Authorization":"Basic Y2hhcmdlOmNoYXJnZV9zZWNyZXQ=", "User-Agent":"AstrBot-CQU-Electricity/0.1.7"}
        cookies = httpx.Cookies()
        cookies.set("synjones-auth",authorization,domain=HOST,path="/charge")
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(headers=headers,cookies=cookies,timeout=min(15,self.timeout),follow_redirects=False,transport=self.transport) as http:
                    async def request(stage, form):
                        async with http.stream("POST",f"{self.scheme}://{HOST}/charge/feeitem/getThirdData",data=form) as response:
                            chunks, size = [], 0
                            async for chunk in response.aiter_bytes():
                                chunks.append(chunk); size += len(chunk)
                                if size > 2 * 1024 * 1024:
                                    record(stage,response.status_code,response.headers.get("content-type",""),redact_body(b"".join(chunks),token))
                                    raise QueryError("RESPONSE_TOO_LARGE")
                            body = b"".join(chunks)
                            record(stage,response.status_code,response.headers.get("content-type",""),redact_body(body,token))
                            if response.status_code in (401,403) or 300 <= response.status_code < 400:
                                raise QueryError("AUTH_EXPIRED")
                            if response.status_code >= 400: raise QueryError("HTTP_ERROR")
                            try: result = json.loads(body)
                            except (ValueError,UnicodeError):
                                if (re.search(rb'(?i)(login|password)',body) or "统一认证".encode() in body): raise QueryError("AUTH_EXPIRED")
                                raise QueryError("INVALID_RESPONSE") from None
                            if not isinstance(result,dict): raise QueryError("INVALID_RESPONSE")
                            if str(result.get("code")) in ("401","403"): raise QueryError("AUTH_EXPIRED")
                            if str(result.get("code")) != "200": raise QueryError("INVALID_RESPONSE")
                            return result.get("map") or {}
                    base = {"feeitemid":{"huxi":"448","shapingba":"449"}[room.campus]}
                    initial = await request("buildings",{**base,"type":"select","level":0})
                    levels, buildings = initial.get("total") or [], initial.get("data") or []
                    if not isinstance(levels,list) or len(levels)<2 or not isinstance(buildings,list):
                        raise QueryError("INVALID_RESPONSE")
                    area=re.fullmatch(r"菊园([AB])区",room.building)
                    if room.campus=="shapingba":
                        wanted=shapingba_building(room.building)
                        candidates=[b for b in buildings if isinstance(b,dict) and wanted and shapingba_building(b.get("name",""))==wanted]
                    else:
                        candidates = [b for b in buildings if isinstance(b,dict) and (building_name(b.get("name",""))==building_name(room.building) or (area and re.fullmatch(r"菊园"+area[1]+r"[1-3]栋",building_name(b.get("name","")))))]
                    if not candidates: raise QueryError("BUILDING_NOT_FOUND")
                    if room.campus!="shapingba" and not area and len(candidates)!=1: raise QueryError("AMBIGUOUS_ROOM")
                    bkey, rkey = levels[0].get("code") or "building", levels[-1].get("code") or "room"
                    matched=[]
                    for building in candidates:
                        result = await request("rooms",{**base,"type":"select","level":1,bkey:building["value"]})
                        choices = [r for r in result.get("data",[]) if isinstance(r,dict) and room.room_number in {str(r.get("name","")).strip().upper(),str(r.get("value","")).strip().upper()}]
                        matched.extend((building,r) for r in choices)
                    if not matched: raise QueryError("ROOM_NOT_FOUND")
                    if len(matched)!=1: raise QueryError("AMBIGUOUS_ROOM")
                    building,matched_room=matched[0]
                    final = await request("reading",{**base,"type":"IEC","level":len(levels),bkey:building["value"],rkey:matched_room["value"]})
                    if not final.get("showData"): raise QueryError("INVALID_RESPONSE")
                    resolved=building_name(building["name"])
                    # Preserve an existing equivalent binding key and its history.
                    if room.campus=="huxi" and resolved==building_name(room.building):
                        resolved=room.building
                    return replace(parse_reading(room,final,self.price),resolved_building=resolved)
        except QueryError:
            raise
        except (TimeoutError,httpx.TimeoutException): raise QueryError("TIMEOUT") from None
        except httpx.HTTPError: raise QueryError("NETWORK") from None
        except (ValueError,TypeError,KeyError,AttributeError): raise QueryError("INVALID_RESPONSE") from None
