from aiogram import dispatcher, Router, F, Dispatcher
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.routes.profile import handle_me
from database import crud
from database.models import Medicine

router = Router()

class Remind_date(StatesGroup):
    get_date = State()


@router.callback_query(F.data.regexp(r"reminder_add_[0-9]+$"))
async def handle_add_reminder(callback: types.CallbackQuery, state: FSMContext):
    user = await crud.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Incorrect user")
        return
    await callback.answer()
    medicine_id = int(callback.data.split("_")[-1])
    await state.set_state(Remind_date.get_date)
    await callback.message.answer("🕒 Enter time in format HH:MM")
    await state.update_data({"medicine_id": medicine_id})

@router.message(Remind_date.get_date)
async def handle_add_reminder_get_date(message: types.Message, state: FSMContext):
    user = await crud.get_user(message.from_user.id)
    data = await state.get_data()
    medicine_id = int(data.get("medicine_id", "-100000"))
    if user is None:
        await state.clear()
        await message.answer("Send /me to register first.")
        return
    try:
        await crud.create_reminder(user.id, medicine_id, "Medicine reminder", message.text)
    except ValueError:
        await message.answer("Enter a valid time in HH:MM format for an existing medicine.")
        return
    await state.clear()
    await message.answer("✅ Reminder added")
    return await handle_me(message)




@router.callback_query(F.data.regexp(r"^reminder_manage_[0-9]+$"))
async def handle_reminder_manage(callback: types.CallbackQuery):
    user = await crud.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Incorrect user")
        return
    reminder_id = int(callback.data.split("_")[-1])
    reminder = await crud.get_reminder(reminder_id=reminder_id, user_id=user.id)
    if reminder is None:
        await callback.answer("Incorrect reminder")
        return
    await callback.answer()
    time_remind = reminder.remind_time
    message = f"Reminder set at {time_remind}"
    mk = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❌ Delete reminder", callback_data=f"reminder_delete_{reminder.id}")
        ]
    ])
    await callback.answer()
    await callback.message.answer(message, reply_markup=mk)

@router.callback_query(F.data.regexp(r"^reminder_delete_[0-9]+$"))
async def handle_reminder_delete(callback: types.CallbackQuery):
    user = await crud.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Incorrect user")
        return
    reminder_id = int(callback.data.split("_")[-1])
    reminder = await crud.get_reminder(reminder_id=reminder_id, user_id=user.id)
    if reminder is None:
        await callback.answer("Incorrect reminder")
        return
    await crud.delete_reminder(reminder_id=reminder_id, user_id=user.id)
    await callback.answer()
    await callback.message.answer("Reminder deleted")
    return

def include_reminders(dp: Dispatcher):
    dp.include_router(router)