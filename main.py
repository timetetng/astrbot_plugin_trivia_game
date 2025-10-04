# 导入必要的模块和类
import asyncio
import json
import random
import difflib
from datetime import datetime # <--- 新增导入，用于处理日期
from typing import Dict, Any

from astrbot.api.event import MessageChain
from astrbot.api import logger  # 使用 astrbot 提供的 logger 接口
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
# --- 新增导入，用于获取经济API ---
try:
    from ..common.services import shared_services
except ImportError:
    logger.warning("无法导入 shared_services，经济功能将不可用。")
    shared_services = {}

# 定义游戏状态的数据结构，用于存储每个群聊的游戏信息
class GameState:
    def __init__(self, question_data: Dict[str, Any], timeout_task: asyncio.Task):
        self.question_data = question_data
        self.hints_given = 0
        self.timeout_task = timeout_task
        self.is_active = True

@register(
    "TriviaGame",                   # 1. 插件名 (name)
    "Gemini",                       # 2. 作者 (author)
    "一个调用LLM出题的趣味猜题插件",   # 3. 描述 (description)
    "2.0.0",                        # 4. 版本 (version)
    ""                              # 5. 仓库地址 (repo_url)
)
class TriviaGamePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.game_states: Dict[str, GameState] = {}
        self.GAME_TIMEOUT_SECONDS = 60.0
        self.TOPICS = [
            "历史", "地理", "文学", "艺术", "音乐", "电影", "数学", "物理",
            "化学", "生物", "天文学", "计算机科学", "编程", "体育", "动漫",
            "游戏", "生活常识", "冷知识", "语言学", "神话传说"
        ]
        
        # --- 新增/修改点：API初始化与每日奖励追踪 ---
        self.economy_api = None
        # 用于追踪用户每日在本插件获得的奖励: {"user_id": {"date": "2025-10-04", "total": 500}}
        self.daily_rewards: Dict[str, Dict[str, Any]] = {}
        
        # 创建一个异步任务来安全地初始化API
        if shared_services:
            asyncio.create_task(self.initialize_apis())

    # --- 新增/修改点：安全获取经济API ---
    async def wait_for_api(self, api_name: str, timeout: int = 30):
        """通用API等待函数"""
        logger.info(f"TriviaGame 正在等待 {api_name} 加载...")
        start_time = asyncio.get_event_loop().time()
        while True:
            api_instance = shared_services.get(api_name)
            if api_instance:
                logger.info(f"TriviaGame 已成功加载 {api_name}。")
                return api_instance
            if asyncio.get_event_loop().time() - start_time > timeout:
                logger.warning(f"TriviaGame 等待 {api_name} 超时，相关功能将受限！")
                return None
            await asyncio.sleep(1)

    async def initialize_apis(self):
        """异步初始化所有依赖的API。"""
        self.economy_api = await self.wait_for_api("economy_api")
        if self.economy_api:
            logger.info("TriviaGame 经济系统接口已就绪，奖励功能已启用。")
        else:
            logger.error("TriviaGame 未能加载经济系统接口，奖励功能将无法使用！")
            
    async def terminate(self):
        """插件被卸载/停用时，清理所有正在进行的游戏任务"""
        for group_id, state in list(self.game_states.items()):
            if state.timeout_task and not state.timeout_task.done():
                state.timeout_task.cancel()
                logger.info(f"猜题游戏(群:{group_id}) 的计时任务已被终止。")
        self.game_states.clear()
        logger.info("所有猜题游戏状态已清理。")

    @filter.on_llm_request()
    async def check_answer_hook(self, event: AstrMessageEvent, req: ProviderRequest):
        """在消息发送给LLM前，检查是否为猜题答案"""
        group_id = event.get_group_id()

        if not group_id or group_id not in self.game_states or not self.game_states[group_id].is_active:
            return

        state = self.game_states[group_id]
        user_answer_text = event.message_str.strip()
        
        if not user_answer_text:
            return

        correct_answers = [str(a).lower().strip() for a in state.question_data["题目可能的答案"]]
        
        is_correct = False
        user_answer_lower = user_answer_text.lower()
        
        if user_answer_lower in correct_answers:
            is_correct = True
        else:
            SIMILARITY_THRESHOLD = 0.85
            for correct_answer in correct_answers:
                if difflib.SequenceMatcher(None, user_answer_lower, correct_answer).ratio() >= SIMILARITY_THRESHOLD:
                    is_correct = True
                    break

        if is_correct:
            # --- 新增/修改点：奖励计算与发放 ---
            winner_id = event.get_sender_id()
            winner_name = event.get_sender_name()
            
            reward_message = ""
            if self.economy_api:
                # 1. 计算基础奖励
                base_reward = 100
                final_reward = int(base_reward * (0.5 ** state.hints_given))

                # 2. 检查每日上限
                today = datetime.now().strftime("%Y-%m-%d")
                user_daily_data = self.daily_rewards.get(winner_id, {"date": "", "total": 0})
                
                if user_daily_data["date"] != today:
                    user_daily_data["date"] = today
                    user_daily_data["total"] = 0
                
                remaining_limit = 1000 - user_daily_data["total"]
                actual_reward = min(final_reward, remaining_limit)
                
                # 3. 发放奖励
                if actual_reward > 0:
                    await self.economy_api.add_coins(winner_id, actual_reward, "猜题游戏胜利")
                    user_daily_data["total"] += actual_reward
                    self.daily_rewards[winner_id] = user_daily_data
                    reward_message = f"恭喜获得 {actual_reward} 金币！"
                else:
                    reward_message = "今日奖励已达上限啦！"

            # ---- 奖励逻辑结束 ----
            
            logger.info(f"群组 {group_id} 的用户 {winner_name} 猜中了答案！")
            
            if state.timeout_task and not state.timeout_task.done():
                state.timeout_task.cancel()

            matched_answer = ""
            highest_sim = 0.0
            for ans in state.question_data["题目可能的答案"]:
                sim = difflib.SequenceMatcher(None, user_answer_lower, str(ans).lower().strip()).ratio()
                if sim > highest_sim:
                    highest_sim = sim
                    matched_answer = ans
            
            success_message = event.plain_result(
                f"🎉 恭喜 @{winner_name} 回答正确！\n"
                f"💡 正确答案就是：【{matched_answer}】\n"
                f"😎 {reward_message}"
            )

            await event.send(success_message)
            del self.game_states[group_id]
            event.stop_event()
        else:
            error_message = event.plain_result(f"🤔 “{user_answer_text}”似乎不是正确答案哦，再想想吧！")
            await event.send(error_message)
            event.stop_event()

    @filter.command("猜题", alias={'出题'})
    async def start_game(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        # ... (此函数内容与上一版完全相同，此处省略以节省篇幅)
        if not group_id:
            yield event.plain_result("这个游戏只能在群聊里玩哦～")
            return

        if group_id in self.game_states and self.game_states[group_id].is_active:
            yield event.plain_result("当前群里已经有一个猜题游戏正在进行啦！")
            return

        yield event.plain_result("正在随机挑选领域和难度，请稍等...")
        provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            yield event.plain_result("哎呀，获取大语言模型失败了，暂时无法出题。")
            return
        selected_topic = random.choice(self.TOPICS)
        difficulties = ["简单", "普通", "困难"]
        weights = [0.3, 0.5, 0.2]
        selected_difficulty = random.choices(difficulties, weights, k=1)[0]
        logger.info(f"为群组 {group_id} 生成题目，随机领域: {selected_topic}，随机难度: {selected_difficulty}")
        prompt = f"""
请你扮演一个知识渊博的出题人，为我设计一个猜题题目。
# 核心要求
1.  题目领域必须是关于：【{selected_topic}】。
2.  题目难度必须是：【{selected_difficulty}】。
3.  “题目描述”字段的内容，最后必须以一个明确的疑问句结尾。
# JSON格式定义
{{
  "题目描述": "请用一段生动的描述引出问题，并确保描述的最后是一个明确的疑问句（例如：‘这是什么现象？’、‘这位人物是谁？’）。",
  "题目可能的答案": ["答案1", "答案2", "..."],
  "题目难度": "这里必须填写我为你指定的难度：【{selected_difficulty}】。",
  "答案提示": ["关于答案的第一个提示", "第二个更明显的提示", "最后一个决定性的提示"]
}}
# “题目可能的答案”字段填写指南
请在这个字段中，尽可能全面地列出所有可能的正确答案，包括但不限于：
- 官方全称
- 常用简称或缩写（例如 "AI"）
- 别名或昵称
- 不同语言的常见翻译（例如 "Artificial Intelligence"）
- 包含或不包含空格/标点的形式
例如，对于“人工智能”，此字段应为 ["人工智能", "AI", "Artificial Intelligence"]。
对于“中华人民共和国”，此字段应为 ["中华人民共和国", "中国", "People's Republic of China", "PRC"]。
现在，请严格按照以上所有要求出题。
"""
        raw_llm_text = ""
        try:
            llm_resp = await provider.text_chat(prompt=prompt)
            raw_llm_text = llm_resp.completion_text
            cleaned_text = raw_llm_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            question_data = json.loads(cleaned_text)
            required_keys = ["题目描述", "题目可能的答案", "题目难度", "答案提示"]
            if not all(key in question_data for key in required_keys):
                raise ValueError("LLM返回的JSON缺少必要的字段。")
        except Exception as e:
            logger.error(f"处理LLM题目时出错: {e}\n原始返回: {raw_llm_text}")
            yield event.plain_result("糟糕，我想题目的时候走神了，没想好。再试一次吧！")
            return
        timeout_task = asyncio.create_task(self._game_timeout(group_id, event))
        self.game_states[group_id] = GameState(question_data, timeout_task)
        difficulty = question_data.get('题目难度', selected_difficulty)
        description = question_data.get('题目描述', '糟糕，题目描述丢了！')
        announcement = (
            f"🎉 猜题游戏开始啦！(领域: {selected_topic} | 难度: {difficulty})\n"
            f"--------------------\n"
            f"题目：\n{description}\n"
            f"--------------------\n"
            f"⏱️ 你有 {int(self.GAME_TIMEOUT_SECONDS)} 秒的时间回答！\n"
            f"👉 直接在群里说出你的答案即可！\n"
            f"💡 仍然可以使用 `/提示` 或 `/结束答题`。"
        )
        yield event.plain_result(announcement)
    
    # --- 新增/修改点：添加结束答题命令 ---
    @filter.command("结束答题", alias={'结束'})
    async def end_game(self, event: AstrMessageEvent):
        """处理 /结束答题 指令，提前结束游戏"""
        group_id = event.get_group_id()
        if not group_id or group_id not in self.game_states or not self.game_states[group_id].is_active:
            yield event.plain_result("当前没有正在进行的猜题游戏哦。")
            return

        state = self.game_states[group_id]
        
        if state.timeout_task and not state.timeout_task.done():
            state.timeout_task.cancel()

        ender_name = event.get_sender_name()
        answers_str = "、".join(map(str, state.question_data["题目可能的答案"]))
        
        yield event.plain_result(
            f"应 @{ender_name} 的要求，本轮猜题已提前结束。\n"
            f"正确答案是：【{answers_str}】"
        )
        
        del self.game_states[group_id]

    @filter.command("提示")
    async def get_hint(self, event: AstrMessageEvent):
        # ... (此函数内容与上一版完全相同)
        group_id = event.get_group_id()
        if not group_id or group_id not in self.game_states or not self.game_states[group_id].is_active:
            return
        state = self.game_states[group_id]
        hints_list = state.question_data["答案提示"]
        if state.hints_given < len(hints_list):
            hint = hints_list[state.hints_given]
            state.hints_given += 1
            yield event.plain_result(
                f"🤫 提示来啦 (第{state.hints_given}条)：\n"
                f"{hint}"
            )
        else:
            yield event.plain_result("🤔 所有的提示都已经给完啦，靠你自己咯！")

    async def _game_timeout(self, group_id: str, event: AstrMessageEvent):
        # ... (此函数内容与上一版完全相同)
        try:
            await asyncio.sleep(self.GAME_TIMEOUT_SECONDS)
            if group_id in self.game_states and self.game_states[group_id].is_active:
                logger.info(f"群组 {group_id} 的猜题游戏时间到。")
                state = self.game_states[group_id]
                answers_str = "、".join(map(str, state.question_data["题目可能的答案"]))
                timeout_message = MessageChain().message(
                    f"⌛️ 时间到！很遗憾没有人答出来呢。\n"
                    f"公布答案：【{answers_str}】\n"
                    f"下次继续努力哦！"
                )
                await self.context.send_message(
                    event.unified_msg_origin,
                    timeout_message
                )
                del self.game_states[group_id]
        except asyncio.CancelledError:
            logger.info(f"群组 {group_id} 的猜题游戏计时器被正常取消。")
        except Exception as e:
            logger.error(f"游戏计时器发生异常: {e}")
            if group_id in self.game_states:
                del self.game_states[group_id]