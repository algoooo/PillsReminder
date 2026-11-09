from aiogram import F, Router, Dispatcher
from aiogram import types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import crud
from database.models import Medicine

router = Router()


@router.message(Command("me"))
async def handle_me(message: types.Message):
    user = await crud.get_user(message.from_user.id)

    # Unregistered user - register immediately
    if user is None:
        user = await crud.create_user(message.from_user.id, message.from_user.username)

    await message.answer(f"Hi, {user.username or message.from_user.first_name}!")

    medicine: list[Medicine] | None = await crud.list_medicines(message.from_user.id)

    # No medicine yet
    if not medicine:
        mk = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💊 Add Medicine", callback_data="medicine_add")
        ]])
        await message.answer(f"❌ There is no active medicine yet", reply_markup=mk)
        return

    mk = InlineKeyboardMarkup(inline_keyboard=[])

    # paginate existing medicine
    for med in medicine:
        mk.inline_keyboard.append([
            InlineKeyboardButton(text=f"⚙️ Manage {med.name}", callback_data=f"medicine_manage_{med.id}")
        ])

    mk.inline_keyboard.append([
        InlineKeyboardButton(text="💊 Add Medicine", callback_data="medicine_add")
    ])
    await message.answer(f"Select medicine to edit: ", reply_markup=mk)

def include_profile(dp: Dispatcher):
    dp.include_router(router)