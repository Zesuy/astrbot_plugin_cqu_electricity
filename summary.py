from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from .models import ERRORS

BEIJING = timezone(timedelta(hours=8))
PUBLIC_FIELDS = ("finished_at","cash_balance_yuan","subsidy_kwh","subsidy_balance_yuan","total_balance_yuan","meter_reading_kwh","unit_price_yuan","today_kwh","today_cost_yuan","today_point","monthly_kwh","monthly_cost_yuan","monthly_name")


def local_time(value):
    return datetime.fromisoformat(value).astimezone(BEIJING).strftime("%m月%d日 %H:%M")


def usage(current, previous):
    if not previous: return None
    hours = (datetime.fromisoformat(current["finished_at"])-datetime.fromisoformat(previous["finished_at"])).total_seconds()/3600
    if hours <= 0: return None
    c,p = current.get("meter_reading_kwh"),previous.get("meter_reading_kwh")
    if c is not None and p is not None:
        delta = Decimal(c)-Decimal(p)
        if delta < 0: return {"available":False,"reason":"电表读数回退，无法计算用量"}
        return {"available":True,"kwh":str(delta),"interval_hours":round(hours,2),"estimated":False,"method":"meter_difference"}
    # Balance changes alone cannot distinguish usage from recharge/subsidy changes.
    return {"available":False,"reason":"接口未提供累计电表读数", "balance_change_yuan":str(Decimal(current["total_balance_yuan"])-Decimal(previous["total_balance_yuan"])),"interval_hours":round(hours,2)}


def public_record(row):
    return {k:row.get(k) for k in PUBLIC_FIELDS}


def public_result(room, current, previous=None, *, cached=False, error=None):
    return {"ok":bool(current) and not error,"room":{"campus":room["campus"],"building":room["building"],"room_number":room["room_number"]},"reading":public_record(current) if current else None,"usage":usage(current,previous) if current else None,"cached":cached,"stale":bool(error and current),"error_code":error,"message":ERRORS.get(error) if error else None,"timezone":"Asia/Shanghai","currency":"CNY"}


def energy_stats_lines(reading, now=None):
    if reading.get("today_kwh") is None and reading.get("monthly_kwh") is None:
        return []
    now_bj = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    lines = ["", "📊 用电统计："]
    if reading.get("today_kwh") is not None:
        finished_at = reading.get("finished_at")
        is_today = True
        finished_dt = None
        if finished_at:
            try:
                finished_dt = datetime.fromisoformat(finished_at).astimezone(BEIJING)
                is_today = (finished_dt.date() == now_bj.date())
            except Exception:
                is_today = True

        point_str = reading.get("today_point", "当前")
        cost_str = f" · 约 {Decimal(reading['today_cost_yuan']):.2f} 元" if reading.get("today_cost_yuan") is not None else ""
        if is_today:
            lines += [f"今日用量：约 {Decimal(reading['today_kwh']):.2f} 度（今日零点至 {point_str}）{cost_str}"]
        else:
            day_str = finished_dt.strftime("%m月%d日") if finished_dt else "记录日"
            lines += [f"{day_str}用量：约 {Decimal(reading['today_kwh']):.2f} 度（{day_str}零点至 {point_str}）{cost_str}"]

    if reading.get("monthly_kwh") is not None:
        m_name = str(reading.get("monthly_name") or "").strip()
        curr_m1 = now_bj.strftime("%Y年%m月")
        curr_m2 = f"{now_bj.year}年{now_bj.month}月"
        is_curr_m = bool(m_name and (m_name == curr_m1 or m_name == curr_m2))
        month_prefix = "本月累计" if is_curr_m else "最近账单"
        month_label = f"（{m_name}）" if m_name else ""
        cost_str = f" · 电费 {Decimal(reading['monthly_cost_yuan']):.2f} 元" if reading.get("monthly_cost_yuan") is not None else ""
        lines += [f"{month_prefix}：{Decimal(reading['monthly_kwh']):.2f} 度{cost_str}{month_label}"]
    return lines


def summary(result, *, detail=False):
    if "daily_brief" in result: return daily_brief_text(result,detail=detail)
    room=result["room"]
    lines=["⚡ "+room["building"]+" · "+room["room_number"]]
    if result.get("error_code"):
        lines += ["", "⚠️ "+result.get("message", "查询失败")]
        if result.get("reading"): lines += ["以下为上次成功记录："]
    reading=result.get("reading")
    if not reading: return "\n".join(lines)
    fmt=lambda v:f"{Decimal(v):.2f}"
    lines += ["", "💰 余额合计："+fmt(reading["total_balance_yuan"])+" 元", "现金："+fmt(reading["cash_balance_yuan"])+" 元"]
    if reading.get("subsidy_kwh") is not None:
        kwh=Decimal(reading["subsidy_kwh"]); price=Decimal(reading["unit_price_yuan"])
        lines += [f"补助：{kwh:.2f} 度，折合 {kwh*price:.2f} 元", f"补助按 {price:f} 元/度折算"]
    if reading.get("subsidy_balance_yuan") is not None:
        lines += ["补贴："+fmt(reading["subsidy_balance_yuan"])+" 元"]
    delta=result.get("usage")
    if delta and delta.get("available"):
        lines += ["", "📉 距上次采集读数增加："+fmt(delta["kwh"])+" 度", f"记录间隔：{delta['interval_hours']:g} 小时"]
    elif delta and delta.get("balance_change_yuan"):
        lines += ["", "余额变化："+fmt(delta["balance_change_yuan"])+" 元（不等于用电量）"]
    if detail:
        if reading.get("meter_reading_kwh") is not None: lines += ["累计电表读数："+fmt(reading["meter_reading_kwh"])+" 度"]
        lines += ["电价："+fmt(reading["unit_price_yuan"])+" 元/度", "校区："+{"huxi":"虎溪","shapingba":"沙坪坝"}[room["campus"]]]
        lines += energy_stats_lines(reading)
        if delta and not delta.get("available"): lines += [delta.get("reason", "用量暂不可用")]
    label="上次成功" if result.get("stale") else "缓存记录" if result.get("cached") else "更新"
    lines += ["", "🕒 "+label+"于 "+local_time(reading["finished_at"]), "查看历史：/电费 历史", "立即更新：/电费 刷新"]
    return "\n".join(lines)


def daily_brief_text(result, *, detail=False):
    room=result["room"];brief=result["daily_brief"]
    lines=["⚡ "+room["building"]+" · "+room["room_number"]]
    reading=result.get("reading")
    if not reading:return "\n".join(lines+[result.get("message") or "暂无保存的采集记录。", "每日09:00、14:00自动采集；手动采集：/电费 刷新"])
    settlement=result.get("settlement")
    if settlement and settlement["estimated_balance_yuan"] is not None:
        lines += [f"💰 预计扣费后余额：{settlement['estimated_balance_yuan']} 元",
                  f"平台余额 {Decimal(reading['total_balance_yuan']):.2f} 元 · 本次电费约 {settlement['pending_cost_yuan']} 元",
                  "用量已更新，平台余额可能尚未结算。"]
    else:lines += [f"💰 已采集余额：{Decimal(reading['total_balance_yuan']):.2f} 元"]
    estimate=brief.get("estimate")
    if estimate:
        day=datetime.fromisoformat(estimate["reference_day"]).date()
        today=datetime.fromisoformat(brief["today"]).date()
        label="昨天" if day==today-timedelta(days=1) else "前天" if day==today-timedelta(days=2) else "历史"
        cost=f" · 电费约 {Decimal(estimate['cost_yuan']):.2f} 元" if estimate.get("cost_yuan") is not None and not settlement else ""
        lines += [f"📉 {label}（{day:%m月%d日}）参考：约 {Decimal(estimate['kwh']):.2f} 度"+cost]
    else:lines += ["日用电参考：暂无，缺少连续日记录或电表读数。"]
    lines += ["🕒 采集于 "+local_time(reading["finished_at"])]
    state=brief["state"]
    if state=="before_refresh":lines += ["昨天的数据将在今天14:00刷新。"]
    elif settlement and not settlement["fresh"]:lines += ["数据暂未更新，以上为上次记录。"]
    elif state=="refresh_failed":lines += ["⚠️ 今天14:00采集尚未成功，以上为已保存记录。"]
    elif state=="awaiting_refresh":lines += ["今天14:00的采集尚未完成，以上为已保存记录。"]
    if detail:
        lines += [f"现金：{Decimal(reading['cash_balance_yuan']):.2f} 元"]
        if reading.get("subsidy_kwh") is not None:lines += [f"补助：{Decimal(reading['subsidy_kwh']):.2f} 度"]
        if reading.get("subsidy_balance_yuan") is not None:lines += [f"补贴：{Decimal(reading['subsidy_balance_yuan']):.2f} 元"]
        if reading.get("meter_reading_kwh") is not None:lines += [f"累计电表读数：{Decimal(reading['meter_reading_kwh']):.2f} 度"]
        lines += [f"电价：{Decimal(reading['unit_price_yuan']):f} 元/度"]
        brief_now = datetime.fromisoformat(brief["today"]).astimezone(BEIJING) if brief.get("today") else None
        lines += energy_stats_lines(reading, now=brief_now)
    if estimate:lines += ["用电及电费为日读数差估算，不代表实际扣费。"]
    lines += ["查看历史：/电费 历史"]
    return "\n".join(lines)


def history_text(result, *, page=1, page_size=20, detail=False):
    if result.get("error_code"): return result["message"]
    if isinstance(page,bool) or not isinstance(page,int) or page<1: raise ValueError("历史页码应为正整数。")
    records=result["records"] if detail else result["daily_records"]
    pages=max(1,(len(records)+page_size-1)//page_size)
    command="/电费 历史"+(" 明细" if detail else "")
    if page>pages: raise ValueError(f"历史只有 {pages} 页，请使用 {command} {result['days']} {pages}。")
    room=result["room"]
    lines=["📊 "+room["building"]+" · "+room["room_number"]+f" 最近 {result['days']} 天"]
    fmt=lambda value:f"{Decimal(value):.2f}"
    if detail:
        lines += [f"共 {len(records)} 次成功采集 · 第 {page}/{pages} 页", "采集时间 · 余额合计 · 累计电表读数"]
    else:
        stats=result["overview"]
        lines += [f"已有 {len(records)} 天记录 · 第 {page}/{pages} 页"]
        if stats.get("latest_reading"):
            latest=stats["latest_reading"]
            lines += ["💰 最新余额："+fmt(latest["total_balance_yuan"])+" 元（"+local_time(latest["finished_at"])+"）"]
            totals=[]
            if stats.get("meter_increase_kwh") is not None:totals.append("累计读数增加 "+fmt(stats["meter_increase_kwh"])+" 度")
            change=stats.get("balance_change_yuan")
            if change is not None:
                value=Decimal(change)
                totals.append(("余额净增加 " if value>0 else "余额净减少 ")+f"{abs(value):.2f} 元")
            if totals:lines += ["已记录期间："+"；".join(totals), "统计范围："+local_time(stats["start_at"])+"～"+local_time(stats["end_at"])]
            avg=stats.get("average")
            if avg:lines += ["📈 日均读数增量："+fmt(avg["kwh_per_day"])+f" 度/日（{avg['start_day']}～{avg['end_day']}，{avg['intervals']} 个连续日间隔）"]
            if stats.get("balance_increase_events"):lines += ["↗️ 检测到余额增加，可能涉及充值或补助；净变化不代表实际扣费。"]
        lines += ["日期 · 余额 · 读数增加 · 余额变化"]
    visible=list(reversed(records))[(page-1)*page_size:page*page_size]
    for row in visible:
        reading=row["reading"]
        if detail:
            captured=datetime.fromisoformat(reading['finished_at']).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
            meter=fmt(reading["meter_reading_kwh"])+"度" if reading.get("meter_reading_kwh") is not None else "无读数"
            lines += [f"{captured} · {fmt(reading['total_balance_yuan'])}元 · {meter}"]
        else:
            date=row["day"][5:]
            if row["sample_kind"]=="fallback":date+=" "+datetime.fromisoformat(reading['finished_at']).astimezone(BEIJING).strftime("%H:%M")+"*"
            delta=row.get("usage")
            meter=fmt(delta["kwh"])+"度" if delta and delta.get("available") else "—"
            change=row.get("balance_change_yuan")
            money="—" if change is None else ("↑" if Decimal(change)>0 else "↓" if Decimal(change)<0 else "")+f"{abs(Decimal(change)):.2f}元"
            notes=[]
            if row.get("span_days",0) and row["span_days"]>1:notes.append(f"跨 {row['span_days']} 天")
            if row.get("balance_increase_observed"):notes.append("期间余额曾增加")
            if delta and delta.get("reason")=="电表读数回退，无法计算用量":notes.append("读数回退")
            lines += [f"{date} · {fmt(reading['total_balance_yuan'])}元 · {meter} · {money}"+("（"+"；".join(notes)+"）" if notes else "")]
    if not records: lines += ["暂无成功采集记录。"]
    if page<pages: lines += [f"下一页：{command} {result['days']} {page+1}"]
    if detail:
        lines += ["时间为北京时间；展示每次成功采集，读取缓存不会新增记录。",f"每日概览：/电费 历史 {result['days']}"]
    else:
        lines += ["优先取 14:00 采集；*为当天最后记录。读数增量不等于当天实际用电，缺失日期不补零。",f"逐次明细：/电费 历史 明细 {result['days']}"]
    return "\n".join(lines)
