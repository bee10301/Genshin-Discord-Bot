import asyncio
from datetime import date, datetime
from typing import ClassVar, Final

import aiohttp
import discord
import sentry_sdk
from discord.ext import commands

from data.database import ScheduleDaily, db
from utility import LOG, EmbedTemplate, config

from .. import genshin_app


class DailyReward:
    """自動排程的類別

    Methods
    -----
    execute(bot: `commands.Bot`)
        執行自動排程
    """

    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    # 統計簽到人數
    _total: ClassVar[dict[str, int]] = {}
    """簽到的總人數 dict[host, count]"""
    _honkai_count: ClassVar[dict[str, int]] = {}
    """簽到崩壞3的人數 dict[host, count]"""

    @classmethod
    async def execute(cls, bot: commands.Bot):
        """執行自動排程，簽到使用者並統計簽到數據

        Parameters
        -----
        bot: `commands.Bot`
            Discord 機器人客戶端
        """
        if cls._lock.locked():
            return
        await cls._lock.acquire()
        try:
            LOG.System("每日自動簽到開始")

            # 初始化
            queue: asyncio.Queue[ScheduleDaily] = asyncio.Queue()
            cls._total = {}
            cls._honkai_count = {}
            daily_users = await db.schedule_daily.getAll()

            # 將所有需要簽到的使用者放入佇列 (Producer)
            for user in daily_users:
                if user.last_checkin_date != date.today():
                    await queue.put(user)

            # 建立本地簽到任務 (Consumer)
            tasks = [asyncio.create_task(cls._claim_daily_reward_task(queue, "LOCAL", bot))]
            # 建立遠端簽到任務 (Consumer)
            for host in config.daily_reward_api_list:
                tasks.append(asyncio.create_task(cls._claim_daily_reward_task(queue, host, bot)))

            start_time = datetime.now()  # 簽到開始時間
            await queue.join()  # 等待所有使用者簽到完成
            for task in tasks:  # 關閉簽到任務
                task.cancel()

            _log_message = f"每日自動簽到結束：總共 {sum(cls._total.values())} 人簽到，其中 {sum(cls._honkai_count.values())} 人也簽到崩壞3\n"
            for host in cls._total.keys():
                _log_message += f"- {host}：{cls._total.get(host)}、{cls._honkai_count.get(host)}\n"
            LOG.System(_log_message)
            await cls._update_statistics(bot, start_time)
        except Exception as e:
            sentry_sdk.capture_exception(e)
            LOG.Error(f"自動排程 DailyReward 發生錯誤：{e}")
        finally:
            cls._lock.release()

    @classmethod
    async def _claim_daily_reward_task(
        cls, queue: asyncio.Queue[ScheduleDaily], host: str, bot: commands.Bot
    ):
        """從傳入的 asyncio.Queue 裡面取得使用者，然後進行每日簽到，並根據簽到結果發送訊息給使用者

        Parameters
        -----
        queue: `asyncio.Queue[ScheduleDaily]`
            存放需要簽到的使用者的佇列
        host: `str`
            簽到的主機
            - 本地：固定為字串 "LOCAL"
            - 遠端：簽到 API 網址

        bot: `commands.Bot`
            Discord 機器人客戶端
        """
        LOG.Info(f"自動排程簽到任務開始：{host}")
        if host != "LOCAL":
            # 先測試 API 是否正常
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(host) as resp:
                        if resp.status != 200:
                            raise Exception(f"Http 狀態碼 {resp.status}")
                except Exception as e:
                    sentry_sdk.capture_exception(e)
                    LOG.Error(f"自動排程 DailyReward 測試 API {host} 時發生錯誤：{e}")
                    return

        cls._total[host] = 0  # 初始化簽到人數
        cls._honkai_count[host] = 0  # 初始化簽到崩壞3的人數
        MAX_API_ERROR_COUNT: Final[int] = 20  # 遠端 API 發生錯誤的最大次數
        api_error_count = 0  # 遠端 API 發生錯誤的次數

        while True:
            user = await queue.get()
            try:
                message = await cls._claim_daily_reward(host, user)
            except Exception as e:
                await queue.put(user)  # 簽到發生異常，將使用者放回佇列
                api_error_count += 1
                LOG.Error(f"遠端 API：{host} 發生錯誤 ({api_error_count}/{MAX_API_ERROR_COUNT})")
                # 如果發生錯誤超過 MAX_API_ERROR_COUNT 次，則停止簽到任務
                if api_error_count >= MAX_API_ERROR_COUNT:
                    sentry_sdk.capture_exception(e)
                    return
            else:
                # 簽到成功後，更新資料庫中的簽到日期、發送訊息給使用者、更新計數器
                await db.schedule_daily.update(user.id, last_checkin_date=True)
                if message is not None:
                    await cls._send_message(bot, user, message)
                    cls._total[host] += 1
                    cls._honkai_count[host] += int(user.has_honkai)
                    await asyncio.sleep(config.schedule_loop_delay)
            finally:
                queue.task_done()

    @classmethod
    async def _claim_daily_reward(cls, host: str, user: ScheduleDaily) -> str | None:
        """
        為使用者進行每日簽到。

        Parameters
        ----------
        host: `str`
            簽到的主機
            - 本地：固定為字串 "LOCAL"
            - 遠端：簽到 API 網址
        user: `ScheduleDaily`
            需要簽到的使用者

        Returns
        -------
        str | None
            簽到結果訊息； None 表示跳過此使用者。

        Raises
        ------
        Exception
            如果簽到失敗，會拋出一個 Exception。
        """
        if host == "LOCAL":  # 本地簽到
            message = await genshin_app.claim_daily_reward(
                user.id, honkai=user.has_honkai, schedule=True
            )
            return message
        else:  # 遠端 API 簽到
            user_data = await db.users.get(user.id)
            if user_data is None:
                return None
            payload = {
                "discord_id": user.id,
                "uid": user_data.uid or 0,
                "cookie": user_data.cookie,
                "has_honkai": "true" if user.has_honkai else "false",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url=host + "/daily-reward", json=payload) as resp:
                    if resp.status == 200:
                        result: dict[str, str] = await resp.json()
                        message = result.get("message", "遠端 API 簽到失敗")
                        return message
                    else:
                        raise Exception(f"{host} 簽到失敗，HTTP 狀態碼：{resp.status}")

    @classmethod
    async def _send_message(cls, bot: commands.Bot, user: ScheduleDaily, result: str):
        """向使用者發送簽到結果的訊息"""
        try:
            channel = bot.get_channel(user.channel_id) or await bot.fetch_channel(user.channel_id)
            # 若不用@提及使用者，則先取得此使用者的暱稱然後發送訊息
            if user.is_mention is False:
                _user = await bot.fetch_user(user.id)
                await channel.send(f"[自動簽到] {_user.display_name}：{result}")  # type: ignore
            else:
                await channel.send(f"[自動簽到] <@{user.id}> {result}")  # type: ignore
        except (
            discord.Forbidden,
            discord.NotFound,
            discord.InvalidData,
        ) as e:  # 發送訊息失敗，移除此使用者
            LOG.Except(f"自動簽到發送訊息失敗，移除此使用者 {LOG.User(user.id)}：{e}")
            await db.schedule_daily.remove(user.id)
        except Exception as e:
            sentry_sdk.capture_exception(e)

    @classmethod
    async def _update_statistics(cls, bot: commands.Bot, start_time: datetime):
        """
        計算自動簽到的統計數據，包括總簽到人數、簽到崩壞3的人數、平均簽到時間，
        並將結果儲存到 schedule cog，同時將結果發送到通知頻道。
        """
        total = sum(cls._total.values())
        honkai_count = sum(cls._honkai_count.values())
        # 計算平均簽到時間
        end_time = datetime.now()
        avg_user_daily_time = (end_time - start_time).total_seconds() / (total if total > 0 else 1)

        # 將平均簽到時間儲存到 schedule cog
        schedule_cog = bot.get_cog("自動化")
        if schedule_cog is not None:
            setattr(schedule_cog, "avg_user_daily_time", avg_user_daily_time)

        # 發送統計結果到通知頻道
        if config.notification_channel_id:
            embed = EmbedTemplate.normal(
                f"總共 {total} 人簽到，其中 {honkai_count} 人也簽到崩壞3\n"
                f"簽到時間：{start_time.strftime('%H:%M:%S')} ~ {end_time.strftime('%H:%M:%S')}\n"
                f"平均時間：{avg_user_daily_time:.2f} 秒/人",
                title="每日自動簽到結果",
            )
            _id = config.notification_channel_id
            _channel = bot.get_channel(_id) or await bot.fetch_channel(_id)
            if isinstance(_channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                await _channel.send(embed=embed)
