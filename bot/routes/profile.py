import os
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest

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

@router.callback_query(F.data.regexp(r"^profile_logged(?:_[0-9]+)?$"))
async def intake_logged(callback: types.CallbackQuery):
    if callback.data == "profile_logged":
        await callback.answer("Use Check on the latest reminder to confirm this dose.", show_alert=True)
        return
    notification_id = int(callback.data.rsplit("_", 1)[-1])
    notification = await crud.confirm_notification(notification_id, user_id=callback.from_user.id)
    if notification is None:
        await callback.answer("Notification not found.", show_alert=True)
        return
    await callback.answer("Intake confirmed. Reminders for this dose are stopped.")
    if not isinstance(callback.message, types.Message):
        return
    local_time = notification.checked_at.astimezone(
        ZoneInfo(os.getenv("REMINDER_TIMEZONE", "Europe/Moscow"))
    ).strftime("%H:%M:%S")
    text = (callback.message.text or "Medicine reminder").split("\n✅ Checked at ", 1)[0]
    try:
        await callback.message.edit_text(
            text=f"{text}\n✅ Checked at {local_time}", reply_markup=None, parse_mode=None,
        )
    except TelegramBadRequest:
        # Confirmation is already saved even if an old message cannot be edited.
        pass


def include_profile(dp: Dispatcher):
    dp.include_router(router)