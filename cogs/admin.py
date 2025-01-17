import asyncio
import random
import typing
from datetime import datetime, time, timedelta
from pathlib import Path

import discord
import enkanetwork
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands, tasks

from genshin_py import auto_task
from utility import SlashCommandLogger, config


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot: commands.Bot = bot
        self.presence_string: list[str] = ["原神"]
        self.change_presence.start()
        self.refresh_genshin_db.start()

    async def cog_unload(self) -> None:
        self.change_presence.cancel()
        self.refresh_genshin_db.cancel()

    # /sync指令：同步 Slash commands 到全域或是當前伺服器
    @app_commands.command(name="sync", description="同步Slash commands到全域或是當前伺服器")
    @app_commands.rename(area="範圍")
    @app_commands.choices(area=[Choice(name="當前伺服器", value=0), Choice(name="全域伺服器", value=1)])
    @SlashCommandLogger
    async def slash_sync(self, interaction: discord.Interaction, area: int = 0):
        await interaction.response.defer()
        if area == 0 and interaction.guild:  # 複製全域指令，同步到當前伺服器，不需等待
            self.bot.tree.copy_global_to(guild=interaction.guild)
            result = await self.bot.tree.sync(guild=interaction.guild)
        else:  # 同步到全域，需等待一小時
            result = await self.bot.tree.sync()

        msg = f'已同步以下指令到{"全部" if area == 1 else "當前"}伺服器：{"、".join(cmd.name for cmd in result)}'
        await interaction.edit_original_response(content=msg)

    # /status指令：顯示機器人相關狀態
    @app_commands.command(name="status", description="顯示小幫手狀態")
    @app_commands.choices(
        option=[
            Choice(name="機器人連線延遲", value="BOT_LATENCY"),
            Choice(name="已連接伺服器數量", value="SERVER_COUNT"),
            Choice(name="已連接伺服器名稱", value="SERVER_NAMES"),
        ]
    )
    @SlashCommandLogger
    async def slash_status(self, interaction: discord.Interaction, option: str):
        match option:
            case "BOT_LATENCY":
                await interaction.response.send_message(f"延遲：{round(self.bot.latency*1000)} 毫秒")
            case "SERVER_COUNT":
                await interaction.response.send_message(f"已連接 {len(self.bot.guilds)} 個伺服器")
            case "SERVER_NAMES":
                await interaction.response.defer()
                names = [guild.name for guild in self.bot.guilds]
                for i in range(0, len(self.bot.guilds), 100):
                    msg = "、".join(names[i : i + 100])
                    embed = discord.Embed(title=f"已連接伺服器名稱({i + 1})", description=msg)
                    await interaction.followup.send(embed=embed)

    # /system指令：操作cog、更改機器人狀態...
    @app_commands.command(name="system", description="使用系統命令(操作cog、更改機器人狀態)")
    @app_commands.rename(option="選項", param="參數")
    @app_commands.choices(
        option=[
            Choice(name="載入 cog", value="LOAD_COG"),
            Choice(name="卸載 cog", value="UNLOAD_COG"),
            Choice(name="重新載入 cog", value="RELOAD_COG"),
            Choice(name="自訂機器人狀態", value="CHANGE_PRESENCE"),
            Choice(name="立即執行領取每日獎勵", value="CLAIM_DAILY_REWARD"),
            Choice(name="更新 Enka 新版本資料", value="UPDATE_ENKA_ASSETS"),
        ]
    )
    @SlashCommandLogger
    async def slash_system(
        self, interaction: discord.Interaction, option: str, param: typing.Optional[str] = None
    ):
        await interaction.response.defer()
        match option:
            case "LOAD_COG":
                await self._operate_cogs(self.bot.load_extension, param, pass_self=True)
                await interaction.edit_original_response(content=f"{param or '全部'}指令集載入完成")
            case "UNLOAD_COG":
                await self._operate_cogs(self.bot.unload_extension, param, pass_self=True)
                await interaction.edit_original_response(content=f"{param or '全部'}指令集卸載完成")
            case "RELOAD_COG":
                await self._operate_cogs(self.bot.reload_extension, param)
                await interaction.edit_original_response(content=f"{param or '全部'}指令集重新載入完成")
            case "CHANGE_PRESENCE":  # 更改機器人狀態
                if param is not None:
                    self.presence_string = param.split(",")
                    await interaction.edit_original_response(
                        content=f"Presence list已變更為：{self.presence_string}"
                    )
            case "CLAIM_DAILY_REWARD":  # 立即執行領取每日獎勵
                await interaction.edit_original_response(content="開始執行每日自動簽到")
                asyncio.create_task(auto_task.DailyReward.execute(self.bot))
            case "UPDATE_ENKA_ASSETS":  # 更新 Enka 新版本素材資料
                client = enkanetwork.EnkaNetworkAPI()
                async with client:
                    await client.update_assets()
                enkanetwork.Assets(lang=enkanetwork.Language.CHT)
                await interaction.edit_original_response(content="Enka 資料更新完成")

    # /config指令：設定config配置檔案的參數值
    @app_commands.command(name="config", description="更改config配置內容")
    @app_commands.rename(option="選項", value="值")
    @app_commands.choices(
        option=[
            Choice(name="schedule_daily_reward_time", value="schedule_daily_reward_time"),
            Choice(
                name="schedule_check_resin_interval",
                value="schedule_check_resin_interval",
            ),
            Choice(name="schedule_loop_delay", value="schedule_loop_delay"),
            Choice(name="notification_channel_id", value="notification_channel_id"),
        ]
    )
    @SlashCommandLogger
    async def slash_config(self, interaction: discord.Interaction, option: str, value: str):
        if option in [
            "schedule_daily_reward_time",
            "schedule_check_resin_interval",
            "notification_channel_id",
        ]:
            setattr(config, option, int(value))
        elif option in ["schedule_loop_delay"]:
            setattr(config, option, float(value))
        await interaction.response.send_message(f"已將{option}的值設為: {value}")

    # /maintenance指令：設定遊戲維護時間
    @app_commands.command(name="maintenance", description="設定遊戲維護時間，輸入0表示將維護時間設定為關閉")
    @app_commands.rename(month="月", day="日", hour="點", duration="維護幾小時")
    @SlashCommandLogger
    async def slash_maintenance(
        self,
        interaction: discord.Interaction,
        month: int,
        day: int,
        hour: int = 6,
        duration: int = 5,
    ):
        if month == 0 or day == 0:
            config.game_maintenance_time = None
            await interaction.response.send_message("已將維護時間設定為：關閉")
        else:
            now = datetime.now()
            start_time = datetime(
                (now.year if month >= now.month else now.year + 1), month, day, hour
            )
            end_time = start_time + timedelta(hours=duration)
            config.game_maintenance_time = (start_time, end_time)
            await interaction.response.send_message(
                f"已將維護時間設定為：{start_time} ~ {end_time}\n"
                + "若每日自動簽到時間在此範圍內，請使用 /config 指令更改每日自動簽到時間"
            )

    # 每一定時間更改機器人狀態
    @tasks.loop(minutes=1)
    async def change_presence(self):
        length = len(self.presence_string)
        n = random.randint(0, length)
        if n < length:
            await self.bot.change_presence(activity=discord.Game(self.presence_string[n]))
        elif n == length:
            await self.bot.change_presence(activity=discord.Game(f"{len(self.bot.guilds)} 個伺服器"))

    @change_presence.before_loop
    async def before_change_presence(self):
        await self.bot.wait_until_ready()

    # 每天定時重整 genshin_db API 資料
    @tasks.loop(time=time(hour=20, minute=00))
    async def refresh_genshin_db(self):
        await self._operate_cogs(self.bot.reload_extension, "search")

    @refresh_genshin_db.before_loop
    async def before_refresh_genshin_db(self):
        await self.bot.wait_until_ready()

    async def _operate_cogs(
        self,
        func: typing.Callable[[str], typing.Awaitable[None]],
        param: typing.Optional[str] = None,
        *,
        pass_self: bool = False,
    ):
        """操作 cog，func 為操作函式，param 為操作的 cog 名稱，pass_self 為是否跳過 admin cog"""
        if param is None:  # 操作全部cog
            for filepath in Path("./cogs").glob("**/*.py"):
                cog_name = Path(filepath).stem
                if pass_self and cog_name == "admin":
                    continue
                await func(f"cogs.{cog_name}")
        else:  # 操作單一cog
            await func(f"cogs.{param}")


async def setup(client: commands.Bot):
    await client.add_cog(Admin(client), guild=discord.Object(id=config.test_server_id))
