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
# 仅用于类型提示
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

@register(
    "astrbot_plugin_recall",
    "Zhalslar",
    "智能撤回插件，可自动判断各场景下消息是否需要撤回",
    "v1.0.4",
    "https://github.com/Zhalslar/astrbot_plugin_recall",
)
class RecallPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.recall_tasks: set[asyncio.Task] = set()
        self.last_msgs: Dict[str, List[BaseMessageComponent]] = {}

    def _is_recall(self, chain: list[BaseMessageComponent], session_id: str) -> bool:
        """判断消息是否需撤回"""
        # 1. 判断复读
        last_msg = self.last_msgs.get(session_id)
        if last_msg and str(chain) == str(last_msg):
            logger.debug(f"检测到复读消息 (Session: {session_id})，触发撤回")
            return True
        
        # 更新该会话的最后一条消息
        self.last_msgs[session_id] = chain

        # 2. 遍历检查内容
        for seg in chain:
            if isinstance(seg, Plain):
                text = seg.text
                if len(text) > self.conf["max_plain_len"]:
                    logger.debug(f"文本长度({len(text)})超过阈值({self.conf['max_plain_len']})，触发撤回")
                    return True
                
                for word in self.conf["recall_words"]:
                    if word in text:
                        logger.debug(f"检测到敏感词[{word}]，触发撤回")
                        return True
        return False

    async def _recall_msg(self, client, message_id: int):
        """延迟撤回消息"""
        if not message_id:
            return
            
        try:
            await asyncio.sleep(self.conf["recall_time"])
            await client.api.call_action('delete_msg', message_id=message_id)
            logger.info(f"已自动撤回消息 ID: {message_id}")
        except Exception as e:
            logger.warning(f"撤回消息失败: {e}")

    async def _notify_admin(self, client, content_str: str, source_info: str):
        """通知管理员 (异步执行)"""
        admin_id = self.conf.get("admin_id")
        if not admin_id:
            return
            
        try:
            msg = f"【自动撤回通知】\n检测到触发策略的消息并已撤回。\n\n来源: {source_info}\n内容: {content_str}"
            await client.send_private_msg(user_id=int(admin_id), message=msg)
        except Exception as e:
            logger.error(f"发送管理员通知失败: {e}")

    @filter.on_decorating_result(priority=10)
    async def on_recall(self, event: AiocqhttpMessageEvent):
        # 1. 平台兼容性检查
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
        
        if not chain or not any(isinstance(seg, (Plain, Image, Video, Face, At, AtAll, Forward, Reply)) for seg in chain):
            return

        # 4. 判断是否需要撤回
        session_id = event.unified_msg_origin
        if not self._is_recall(chain, session_id):
            return

        # === 修复点：手动提取文本内容 ===
        texts = [seg.text for seg in chain if isinstance(seg, Plain)]
        msg_content = "".join(texts)
        if any(isinstance(seg, Image) for seg in chain):
            msg_content += " [包含图片]"
        
        source_info = f"群 {group_id}" if group_id else f"私聊 {event.get_sender_id()}"
        
        # 5. 后台日志显示
        logger.info(f"[Recall Plugin] 触发撤回！来源: {source_info} | 内容: {msg_content[:100]}...")

        # 6. 执行流程
        client = event.bot
        try:
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
                
                # 6.2 启动管理员通知任务
                task_notify = asyncio.create_task(self._notify_admin(client, msg_content, source_info))
                self.recall_tasks.add(task_notify)
                task_notify.add_done_callback(self.recall_tasks.discard)
            
            chain.clear() 
            
        except Exception as e:
            logger.error(f"处理撤回逻辑时发生错误: {e}")

    async def terminate(self):
        if self.recall_tasks:
            for task in self.recall_tasks:
                task.cancel()
        self.recall_tasks.clear()
