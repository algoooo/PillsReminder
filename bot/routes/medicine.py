from aiogram import dispatcher, Router, F, Dispatcher
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import crud
from database.models import Medicine, Reminder, User


class Adding_medicine(StatesGroup):
    get_name = State()
    get_amount = State()

router = Router()


@router.callback_query(F.data.regexp(r"^medicine_manage_[0-9]+$"))
async def handle_medicine_manage(callback: types.CallbackQuery):
    medicine_id = int(callback.data.split("_")[-1])

    medicine: Medicine | None = await crud.get_medicine(medicine_id=medicine_id, user_id=callback.from_user.id)

    if medicine is None:
        await callback.answer("Incorrect option")
        return

    reminders: list[Reminder] = [
        reminder for reminder in await crud.list_reminders(callback.from_user.id)
        if reminder.medicine_id == medicine_id
    ]

    reminders_str = "Reminders are set at: \n"
    reminders_Id_time = dict()
    if not reminders:
        reminders_str = ""
    else:
        for reminder in reminders:
            formatted_time = reminder.remind_time
            reminders_str += formatted_time + "\n"
            reminders_Id_time.update({
                reminder.id: formatted_time
            })

    message = f"Your medicine {medicine.name}: \nAmount: {medicine.amount} \n{reminders_str}"
    mk = InlineKeyboardMarkup(inline_keyboard=[])
    for r_id, r_time in reminders_Id_time.items():
        mk.inline_keyboard.append([
            InlineKeyboardButton(text=f"{r_time}", callback_data=f"reminder_manage_{r_id}")
        ])
    mk.inline_keyboard.append([InlineKeyboardButton(text='Add reminder', callback_data=f"reminder_add_{medicine_id}")])
    mk.inline_keyboard.append([InlineKeyboardButton(
            text="DELETE MEDICINE",
            callback_data=f"medicine_delete_{medicine.id}")]
        )
    await callback.answer()
    await callback.message.answer(message, reply_markup=mk)
    return

@router.callback_query(F.data.regexp(r"^medicine_delete_[0-9]+$"))
async def handle_medicine_delete(callback: types.CallbackQuery):
    medicine_id = int(callback.data.split("_")[-1])
    medicine: Medicine | None = await crud.get_medicine(medicine_id=medicine_id, user_id=callback.from_user.id)
    if medicine is None:
        await callback.answer("Incorrect option")
        return
    await crud.delete_medicine(medicine_id=medicine_id, user_id=callback.from_user.id)
    await callback.answer()
    await callback.message.answer("Deleted.")
    return

@router.callback_query(F.data=="medicine_add")
async def handle_medicine_add(callback: types.CallbackQuery, state: FSMContext):
    user: User | None = await crud.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Incorrect option")
        return
    await state.clear()
    await state.set_state(Adding_medicine.get_name)
    await callback.answer()
    await callback.message.answer("Enter the medicine name:")

@router.message(Adding_medicine.get_name)
async def handle_medicine_add_get_name(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip() or len(message.text.strip()) > 255:
        await message.answer("Enter a medicine name between 1 and 255 characters.")
        return
    await state.update_data({
        "name": message.text
    })
    await state.set_state(Adding_medicine.get_amount)
    await message.answer("Enter the amount:")


@router.message(Adding_medicine.get_amount)
async def handle_medicine_add_get_amount(message: types.Message, state: FSMContext):
    try:
        amount = int(message.text)
        if amount < 0 or amount > 2_147_483_647:
            raise ValueError
    except (TypeError, ValueError):
        await message.answer("Enter a whole number between 0 and 2147483647.")
        return
    await state.update_data({
        "amount": amount
    })
    data = await state.get_data()
    await crud.create_medicine(message.from_user.id, data["name"], data["amount"])
    await message.answer(f"Added {data['name']}")
    await state.clear()
    return

def include_medicine(dp: Dispatcher):
    dp.include_router(router)