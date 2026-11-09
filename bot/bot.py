from aiogram import Bot, Dispatcher
import os
from bot.routes.medicine import include_medicine
from bot.routes.reminders import include_reminders
from bot.routes.profile import include_profile
from aiogram.client.session.aiohttp import AiohttpSession

TOKEN: str = os.getenv('BOT_TOKEN', "-1")

bot = Bot(TOKEN)
dp = Dispatcher()


async def start_polling():
    include_medicine(dp)
    include_reminders(dp)
    include_profile(dp)
    print("Routers initialized")
    await dp.start_polling(bot, close_bot_session=False)
    print("bot has stopped")

