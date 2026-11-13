from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shlex
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Plain, At, Image

from .client import ElectricityClient
from .models import ERRORS, QueryError
from .repository import Repository
from .service import ElectricityService
from .summary import summary, history_text
from .scheduler import Scheduler
from .dashboard import DashboardRenderer, dashboard_input
from .calendar_model import calendar_input, make_calendar_model, calendar_summary, report_month
from .actions import ElectricityActions, parse_reminder
from . import messages

HELP="""⚡ 宿舍电费
/电费 绑定 虎溪 梅园1栋 101
/电费 绑定 A1101
/电费 绑定 B10S306
/电费 绑定 沙坪坝 A区12舍 3单元112
/电费：电费看板（读取已采集数据）
/电费 刷新：立即查询
/电费 详情：读数与电价
/电费 历史 [1～30天] [页码]
/电费 历史 明细 [1～30天] [页码]
/电费 月报 [本月、上月或 YYYY-MM]：默认本月
/电费 本轮：从最近一次余额增加或首次记录至今
/电费 提醒 10元
/电费 提醒 2天
/电费 提醒：查看当前设置
/电费 提醒 关闭
/电费 解绑
每个 QQ 号全局绑定一个宿舍，群聊和私聊共用；提醒默认关闭。
提醒发送到最后一次开启提醒的会话。"""


@register("astrbot_plugin_cqu_electricity","Zesuy","宿舍电费看板、金额/天数提醒与本人管理工具","0.1.17")
class CquElectricityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config=config
        self.data=Path(get_astrbot_data_path())/"plugin_data"/"astrbot_plugin_cqu_electricity"
        self.service=None
        self._closing=False
        self.actions=None
        self.scheduler=None
        self.task=None
        self.seen={}
        self.limits={}
        self._whitelist_patch=None
        self.dashboard=DashboardRenderer(self.data/"dashboard")

    async def initialize(self):
        repo=Repository(self.data/"electricity.db")
        token_path=str(self.config.get("token_file","")).strip() or str(self.data/"auth.json")
        try:
            client=ElectricityClient(token_path,session=self.config.get("session",""),scheme=self.config.get("query_scheme","https"),timeout=self.config.get("request_timeout",40),price=self.config.get("electricity_price","0.54"),nh_enabled=self.config.get("shapingba_nh_enabled",True),nh_host=self.config.get("shapingba_nh_host","nh.cqu.edu.cn:26989"),nh_scheme=self.config.get("shapingba_nh_scheme","http"))
            self.service=ElectricityService(repo,client,cache_seconds=self.config.get("cache_seconds",60))
            self.actions=ElectricityActions(self.service,self.config)
            self.scheduler=Scheduler(self.service,self._allowed,self._send,capture_times=self.config.get("capture_times",["09:00","14:00"]),capture_window_minutes=self.config.get("capture_window_minutes",15),reminder_enabled=self.config.get("reminder_enabled",True))
            self._install_private_commands()
            if self.config.get("auto_capture_enabled",True):
                self.task=asyncio.create_task(self._loop(),name="cqu_electricity_capture")
            logger.info("CQU electricity loaded; authentication configured=%s",client.configured())
        except Exception:
            self._remove_private_commands()
            repo.close()
            self.service=None
            raise

    async def terminate(self):
        self._closing=True
        service=self.service
        self._remove_private_commands()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self.task
        if service: await service.close()
        await self.dashboard.close()
        self.service=None
        self.actions=None
        self.scheduler=None

    def _install_private_commands(self):
        # WakingCheck has already matched the command and applied session plugin
        # settings. Exempt only this handler, leaving every other message under
        # the original whitelist. No persistent AstrBot/core config is changed.
        from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
        original=WhitelistCheckStage.process
        async def process(stage,event):
            if self.service and self.config.get("enable_private",True) and event.is_private_chat() and self._identity(event):
                handlers=event.get_extra("activated_handlers",[]) or []
                own=[h for h in handlers if h.handler_module_path==__name__ and h.handler_name=="electricity"]
                if own:
                    event.set_extra("activated_handlers",own)
                    return
            return await original(stage,event)
        WhitelistCheckStage.process=process
        self._whitelist_patch=(WhitelistCheckStage,original,process)

    def _remove_private_commands(self):
        if self._whitelist_patch:
            stage,original,process=self._whitelist_patch
            if stage.process is process: stage.process=original
            self._whitelist_patch=None

    def _allowed(self, platform, group, origin):
        adapter=self.context.get_platform_inst(platform)
        if adapter is None or adapter.meta().name!="aiocqhttp": return False
        if not group:
            return bool(self.config.get("enable_private",True) and re.fullmatch(re.escape(str(platform))+r":FriendMessage:[0-9]+",str(origin)))
        if not re.fullmatch(r"[0-9]+",str(group)): return False
        restricted=[str(x) for x in self.config.get("allowed_groups",[])]
        if restricted and str(group) not in restricted: return False
        cfg=self.context.get_config(umo=origin)
        settings=cfg.get("platform_settings",{})
        whitelist=[str(x).strip() for x in settings.get("id_whitelist",[]) if str(x).strip()]
        if settings.get("enable_id_white_list") and whitelist:
            if str(group) not in whitelist and origin not in whitelist: return False
        return True

    def _identity(self,event):
        # No caller-supplied identity is accepted by any LLM tool.
        platform,group,user=str(event.get_platform_id()),str(event.get_group_id() or ""),str(event.get_sender_id())
        if event.get_platform_name()!="aiocqhttp" or not re.fullmatch(r"[0-9]+",user) or not self._allowed(platform,group,str(event.unified_msg_origin)):
            return None
        if not group and str(event.unified_msg_origin)!=Repository.route(platform,"",user): return None
        return platform,group,user

    async def _send(self, origin, users, text):
        components=([At(qq=user) for user in users]+[Plain("\n"+text)]) if ":GroupMessage:" in origin else [Plain(text)]
        return await self.context.send_message(origin,MessageChain(components))

    async def _loop(self):
        while True:
            try: await self.scheduler.tick()
            except asyncio.CancelledError: raise
            except Exception as exc: logger.warning("CQU electricity scheduler deferred (%s)",type(exc).__name__)
            await asyncio.sleep(self.scheduler.next_delay)

    def _rate_limit(self,identity):
        now=time.monotonic()
        self.limits={k:v for k,v in self.limits.items() if now-v<2}
        user=str(identity[-1])
        if user in self.limits: return True
        self.limits[user]=now
        return False

    async def _send_panel(self,event,identity):
        result=await self.service.mine(identity)
        if not result.get("room"):
            return {"ok":False,"error_code":"NOT_BOUND","message":messages.NOT_BOUND}
        try:
            data=dashboard_input(self.service.repo,identity,window_minutes=self.config.get("capture_window_minutes",15))
            path=await self.dashboard.render(data)
        except Exception as exc:
            logger.warning("CQU electricity dashboard unavailable; using text (%s)",type(exc).__name__)
            await event.send(event.plain_result("图片暂时没能生成，先看文字摘要：\n"+summary(result)))
            return {"ok":True,"image_sent":False,"text_sent":True,"message":"图片暂时没能生成，已发送文字摘要。"}
        await event.send(event.chain_result([Image.fromFileSystem(path)]))
        return {"ok":True,"image_sent":True,"message":"电费面板已发送，不需要重复发送或描述为新采集。"}

    async def _operation_tool(self,event,operation,*args):
        identity=self._identity(event)
        if identity is None:
            return json.dumps({"ok":False,"error_code":"FORBIDDEN","message":"当前会话不允许使用电费功能。"},ensure_ascii=False)
        if self._closing or not self.service or not self.actions:
            return json.dumps({"ok":False,"error_code":"NOT_READY","message":"电费功能还没准备好，请稍后再试。"},ensure_ascii=False)
        async with self.service.identity_lock(identity):
            if self._closing:
                return json.dumps({"ok":False,"error_code":"NOT_READY","message":"电费功能还没准备好，请稍后再试。"},ensure_ascii=False)
            # A repeated model tool call in the same event must not bind/query
            # twice or send the same image twice. Separate user messages can.
            cache=event.get_extra("cqu_electricity_operations",{}) or {}
            key=json.dumps([operation,args],ensure_ascii=False,default=str)
            if operation!="get_reminder" and key in cache: return cache[key]
            try:
                origin=str(event.unified_msg_origin)
                if operation=="panel":result=await self._send_panel(event,identity)
                elif operation=="bind":result=await self.actions.bind(identity,origin,*args)
                else:result=getattr(self.actions,operation)(identity,origin,*args)
            except (ValueError,TypeError) as exc:
                result={"ok":False,"error_code":"INVALID_ARGUMENT","message":str(exc) or "参数格式不正确。"}
            except Exception as exc:
                logger.warning("CQU electricity tool unavailable (%s)",type(exc).__name__)
                result={"ok":False,"error_code":"INTERNAL","message":"这次操作未能确认完成，请先查询当前状态，不要声称操作成功。"}
            encoded=json.dumps(result,ensure_ascii=False,allow_nan=False)
            cache[key]=encoded
            event.set_extra("cqu_electricity_operations",cache)
            return encoded

    @filter.command("电费",alias={"electricity"})
    async def electricity(self,event: AstrMessageEvent):
        event.stop_event()
        identity=self._identity(event)
        if not identity:
            await event.send(event.plain_result("🔒 电费插件仅支持已开启的 QQ 私聊和允许的 QQ 群。")); return
        if self._closing or not self.service:
            await event.send(event.plain_result("电费插件尚未就绪。")); return
        now=time.monotonic()
        self.seen={k:v for k,v in self.seen.items() if now-v<3600}
        message_id=str(getattr(event.message_obj,"message_id",""))
        key=(*identity,message_id)
        if message_id and key in self.seen: return
        if self._rate_limit(identity):
            await event.send(event.plain_result("⏳ 请稍后再试。")); return
        if message_id: self.seen[key]=now
        if len(self.seen)>2000: self.seen.pop(next(iter(self.seen)))
        async with self.service.identity_lock(identity):
            if self._closing:
                await event.send(event.plain_result("电费插件尚未就绪。")); return
            try:
                args=shlex.split(event.message_str.strip())[1:]
                command=args[0].lower() if args else "查询"
                if command in {"绑定","bind"}:
                    text=(await self.actions.bind(identity,str(event.unified_msg_origin),args[1:]))["message"]
                elif command in {"查询","余额","status","刷新","refresh","详情","detail"}:
                    if len(args)>1: raise ValueError("此命令不接受用户号或房号，请先绑定自己的宿舍。")
                    if command in {"查询","余额","status"}:
                        result=await self._send_panel(event,identity)
                        if result["ok"]: return
                        text=result["message"]
                    else:
                        result=await self.service.mine(identity,force=command in {"刷新","refresh"})
                        text=summary(result,detail=command in {"详情","detail"}) if result.get("room") else result["message"]
                elif command in {"月报","本轮"}:
                    if command == "本轮" and len(args) != 1:
                        raise ValueError("用法：/电费 本轮；只展示自己绑定的宿舍。")
                    if command == "月报" and len(args) > 2:
                        raise ValueError("用法：/电费 月报 [本月、上月或 YYYY-MM]")
                    month = report_month(args[1] if len(args) == 2 else None) if command == "月报" else None
                    data = calendar_input(self.service.repo, identity,
                        scope="month" if command == "月报" else "cycle", month_key=month,
                        window_minutes=self.config.get("capture_window_minutes", 15))
                    if data is None:
                        raise ValueError("请先绑定自己的宿舍：/电费 绑定 A1101")
                    model = make_calendar_model(data)
                    text = calendar_summary(model)
                    try:
                        paths = await self.dashboard.render_calendar(model)
                    except Exception as exc:
                        logger.warning("CQU electricity calendar unavailable; using text (%s)", type(exc).__name__)
                    else:
                        for path in paths:
                            await event.send(event.chain_result([Image.fromFileSystem(path)]))
                        return
                elif command in {"历史","history"}:
                    rest=args[1:]
                    detail=bool(rest and rest[0].lower() in {"明细","detail","details"})
                    if detail: rest=rest[1:]
                    if len(rest)>2: raise ValueError("用法：/电费 历史 [明细] [1～30天] [页码]")
                    days=int(rest[0]) if rest else 7
                    page=int(rest[1]) if len(rest)==2 else 1
                    text=history_text(self.service.history(identity,days),page=page,detail=detail)
                elif command in {"解绑","unbind"}:
                    if len(args)!=1: raise ValueError("用法：/电费 解绑")
                    text=self.actions.unbind(identity,str(event.unified_msg_origin))["message"]
                elif command in {"提醒","reminder"}:
                    operation,value,unit=parse_reminder(args[1:],self.config.get("low_balance_threshold","10"))
                    origin=str(event.unified_msg_origin)
                    if operation=="get":result=self.actions.get_reminder(identity,origin)
                    elif operation=="off":result=self.actions.disable_reminder(identity,origin)
                    else:result=self.actions.set_reminder(identity,origin,value,unit)
                    text=result["message"]
                elif command in {"管理","admin"} and getattr(event,"role",None)=="admin":
                    text="认证："+("已配置" if self.service.client.configured() else "未配置")+"\n自动采集："+("开启" if self.task else "关闭")+"\n采集时间："+"、".join(f"{h:02d}:{m:02d}" for h,m in self.scheduler.capture_times)+"（北京时间）"
                else: text=HELP
            except QueryError as exc: text="⚠️ "+ERRORS.get(exc.code,"查询失败。")
            except (ValueError,TypeError) as exc:
                text=str(exc) if str(exc) and not str(exc).startswith("invalid literal") else "参数格式不正确。\n"+HELP
            except Exception as exc:
                logger.warning("CQU electricity command failed (%s)",type(exc).__name__)
                text="暂时未能处理电费请求，请稍后重试。"
        await event.send(event.plain_result(text))

    @filter.llm_tool(name="set_my_electricity_reminder")
    async def set_my_electricity_reminder(self,event: AstrMessageEvent,value: float,unit: str) -> str:
        """仅当当前用户明确要求设置或修改自己的电费提醒时调用。金额与天数互斥，新设置覆盖旧条件，提醒发到当前会话；不要因普通电费查询自动开启提醒。不得代他人设置。以返回的 ok 和 message 为准，失败不得声称成功。

        Args:
            value(number): 提醒阈值，元为0～10000，天为正数，支持小数。
            unit(string): 必须为“元”或“天”。
        """
        return await self._operation_tool(event,"set_reminder",value,unit)

    @filter.llm_tool(name="get_my_electricity_reminder")
    async def get_my_electricity_reminder(self,event: AstrMessageEvent) -> str:
        """查看当前用户本人的电费提醒条件和发送位置，不改变设置，不采集。只返回本会话、私聊或其他群聊，不获取他人设置。"""
        return await self._operation_tool(event,"get_reminder")

    @filter.llm_tool(name="disable_my_electricity_reminder")
    async def disable_my_electricity_reminder(self,event: AstrMessageEvent) -> str:
        """仅当当前用户明确要求关闭自己的电费提醒时调用；从任意允许会话关闭本人全局提醒，不影响室友。必须根据返回结果回答，不因普通查询擅自关闭。"""
        return await self._operation_tool(event,"disable_reminder")

    @filter.llm_tool(name="bind_my_electricity")
    async def bind_my_electricity(self,event: AstrMessageEvent,room: str) -> str:
        """仅当当前用户明确要求绑定或更换自己的宿舍时调用，不因普通电费查询自动绑定。查询校方验证成功才更换；换绑成功关闭原提醒，同宿舍重绑保留提醒，验证失败保留原设置。不能传QQ号或代他人绑定。只按返回结果说明是否成功。

        Args:
            room(string): 宿舍完整账号如A1101、B10S306，或“虎溪 梅园1栋 101”“沙坪坝 A区12舍 3单元112”。
        """
        return await self._operation_tool(event,"bind",room)

    @filter.llm_tool(name="unbind_my_electricity")
    async def unbind_my_electricity(self,event: AstrMessageEvent) -> str:
        """仅当当前用户明确要求解除自己的宿舍绑定时调用，同时关闭本人关联提醒，保留历史。不操作他人，不因查询或换绑失败自动解绑，只按返回结果回答。"""
        return await self._operation_tool(event,"unbind")

    @filter.llm_tool(name="send_my_electricity_panel")
    async def send_my_electricity_panel(self,event: AstrMessageEvent) -> str:
        """用户要看自己的电费、余额面板或用电图片时使用。直接向当前会话发送与/电费相同的看板，仅读取已采集数据，不触发刷新。成功已发送图片则不要重复发送；生成失败会发送文字摘要。未绑定或发送失败时按结果说明，不编造成功。"""
        return await self._operation_tool(event,"panel")

    @filter.llm_tool(name="get_my_electricity")
    async def get_my_electricity(self,event: AstrMessageEvent) -> str:
        """只读取当前 QQ 用户绑定宿舍的已保存余额与用量，不触发采集。09点至14点如有今早记录，返回实际平台余额；仅当读数增加且从昨天14点起观察到的余额、补助和电价均未变化时，另给settlement中的假设扣费后余额，不代表已结算。14点后恢复固定日记录。必须区分实际余额、预计余额、参考日及旧数据。身份来自事件，不能查询其他用户、指定房号或修改绑定和提醒。"""
        identity=self._identity(event)
        if identity is None: return json.dumps({"ok":False,"error_code":"FORBIDDEN","message":"当前会话不允许电费查询。"},ensure_ascii=False)
        if self._closing or not self.service: return json.dumps({"ok":False,"error_code":"NOT_READY"})
        result=await self.service.mine(identity,trigger="llm")
        return json.dumps(result,ensure_ascii=False)

    @filter.llm_tool(name="get_my_electricity_history")
    async def get_my_electricity_history(self,event: AstrMessageEvent,days: int=7) -> str:
        """读取当前 QQ 用户全局绑定宿舍每次成功采集的电费和用量历史，同时返回每日概览、范围汇总、连续14:00采集的日均读数增量，以及按时间顺序排列的每次成功采集明细。读数增加不等于采集间隔内实际用电，余额净变化不等于扣费；缺失、读数回退和余额增加须按标记说明。不改变绑定、提醒或采集设置。缺失日期没有记录；余额变化不等于用电量。

        Args:
            days(number): 查询最近几天，1～30 的整数，通常使用 7。
        """
        identity=self._identity(event)
        if identity is None: return json.dumps({"ok":False,"error_code":"FORBIDDEN","message":"当前会话不允许电费查询。"},ensure_ascii=False)
        if self._closing or not self.service: return json.dumps({"ok":False,"error_code":"NOT_READY"})
        return json.dumps(self.service.history(identity,days),ensure_ascii=False)
