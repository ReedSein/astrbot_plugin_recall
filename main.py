import asyncio
from typing import Dict, List, Optional

from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.api import logger
from astrbot.core.message.components import (
    At,
    AtAll,
    BaseMessageComponent,
    Face,
    Forward,
    Image,
    Plain,
    Reply,
    Video,
)
from astrbot.core.message.message_event_result import MessageChain
# 仅用于类型提示，运行时会做兼容性检查
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

@register(
    "astrbot_plugin_recall_beta",
    "Zhalslar&ReedSein",
    "智能撤回插件，可自动判断各场景下消息是否需要撤回",
    "v1.0.3",
    "https://github.com/ReedSein/astrbot_plugin_recall",
)
class RecallPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        # 使用 set 管理 task，避免内存泄漏
        self.recall_tasks: set[asyncio.Task] = set()
        # 使用字典隔离不同会话的上一条消息: {session_id: message_chain}
        self.last_msgs: Dict[str, List[BaseMessageComponent]] = {}

    def _is_recall(self, chain: list[BaseMessageComponent], session_id: str) -> bool:
        """判断消息是否需撤回"""
        # 1. 判断复读 (基于 Session 隔离)
        last_msg = self.last_msgs.get(session_id)
        # 将 chain 转为字符串进行比较，避免对象引用问题
        if last_msg and str(chain) == str(last_msg):
            logger.debug(f"检测到复读消息 (Session: {session_id})，触发撤回")
            return True
        
        # 更新该会话的最后一条消息
        self.last_msgs[session_id] = chain

        # 2. 遍历检查内容
        for seg in chain:
            if isinstance(seg, Plain):
                text = seg.text
                # 判断长文本
                if len(text) > self.conf["max_plain_len"]:
                    logger.debug(f"文本长度({len(text)})超过阈值({self.conf['max_plain_len']})，触发撤回")
                    return True
                
                # 判断关键词
                for word in self.conf["recall_words"]:
                    if word in text:
                        logger.debug(f"检测到敏感词[{word}]，触发撤回")
                        return True
            
            # 图片鉴黄等逻辑可在此处扩展 (Image 类型)
            
        return False

    async def _recall_msg(self, client, message_id: int):
        """延迟撤回消息"""
        if not message_id:
            return
            
        try:
            await asyncio.sleep(self.conf["recall_time"])
            # 调用 OneBot V11 的 delete_msg API
            await client.api.call_action('delete_msg', message_id=message_id)
            logger.info(f"已自动撤回消息 ID: {message_id}")
        except Exception as e:
            logger.warning(f"撤回消息失败 (可能是权限不足或消息已消失): {e}")

    async def _notify_admin(self, client, content_str: str, source_info: str):
        """通知管理员 (异步执行)"""
        admin_id = self.conf.get("admin_id")
        if not admin_id:
            return
            
        try:
            # 构造通知消息
            msg = f"【自动撤回通知】\n检测到触发策略的消息并已撤回。\n\n来源: {source_info}\n内容: {content_str}"
            # 确保 admin_id 转为 int
            await client.send_private_msg(user_id=int(admin_id), message=msg)
        except Exception as e:
            logger.error(f"发送管理员通知失败: {e}")

    @filter.on_decorating_result(priority=10)
    async def on_recall(self, event: AiocqhttpMessageEvent):
        """
        在消息发送前拦截。
        如果符合撤回条件，则手动发送 -> 启动撤回任务 -> 启动通知任务 -> 阻止原消息发送。
        """
        
        # 1. 平台兼容性检查：目前逻辑强依赖 OneBot V11 的 message_id
        if event.get_platform_name() != "aiocqhttp":
            return

        # 2. 白名单检查
        group_id = event.get_group_id()
        if self.conf["group_whitelist"] and group_id:
            if group_id not in self.conf["group_whitelist"]:
                return

        # 3. 获取消息链
        result = event.get_result()
        chain = result.chain
        
        # 过滤空消息或纯控制消息
        if not chain or not any(isinstance(seg, (Plain, Image, Video, Face, At, AtAll, Forward, Reply)) for seg in chain):
            return

        # 4. 判断是否需要撤回
        # 使用 unified_msg_origin 作为 session 唯一标识，防止跨群串台
        session_id = event.unified_msg_origin
        if not self._is_recall(chain, session_id):
            return

        # === 获取纯文本内容用于日志和通知 ===
        # AstrBot 提供了 message_str 属性方便获取人类可读文本
        msg_content = MessageChain(chain=chain).message_str
        source_info = f"群 {group_id}" if group_id else f"私聊 {event.get_sender_id()}"
        
        # 5. 后台日志显示
        # 截取前100字符避免日志过长
        logger.info(f"[Recall Plugin] 触发撤回！来源: {source_info} | 内容: {msg_content[:100]}...")

        # 6. 执行 "手动发送 -> 记录 ID -> 调度任务" 流程
        client = event.bot
        try:
            # 将 Component 转为 OneBot 协议格式
            obmsg = await event._parse_onebot_json(MessageChain(chain=chain))
            
            send_result = None
            if group_id:
                send_result = await client.send_group_msg(group_id=int(group_id), message=obmsg)
            elif user_id := event.get_sender_id():
                send_result = await client.send_private_msg(user_id=int(user_id), message=obmsg)

            if send_result and (message_id := send_result.get("message_id")):
                # 6.1 启动撤回任务
                task_recall = asyncio.create_task(self._recall_msg(client, int(message_id)))
                self.recall_tasks.add(task_recall)
                task_recall.add_done_callback(self.recall_tasks.discard)
                
                # 6.2 启动管理员通知任务 (非阻塞)
                task_notify = asyncio.create_task(self._notify_admin(client, msg_content, source_info))
                self.recall_tasks.add(task_notify)
                task_notify.add_done_callback(self.recall_tasks.discard)
            
            # 关键：清空原消息链，阻止 AstrBot 再次发送
            chain.clear() 
            
        except Exception as e:
            logger.error(f"处理撤回逻辑时发生错误: {e}")
            # 发生错误时保留 chain，让 AstrBot 尝试兜底发送，避免消息彻底丢失

    async def terminate(self):
        """插件卸载时清理资源"""
        if self.recall_tasks:
            for task in self.recall_tasks:
                task.cancel()
            logger.info(f"已取消 {len(self.recall_tasks)} 个挂起的任务")
        self.recall_tasks.clear()
