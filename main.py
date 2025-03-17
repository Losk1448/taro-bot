# main.py

"""
Основной модуль бота ArcanaLens.

Данный модуль:
- Инициализирует бота, базу данных и диспетчер очереди.
- Обрабатывает команды и callback-запросы с использованием библиотеки aiogram.
- Предоставляет функционал для получения ежедневной карты, раскладов, вопросов и администрирования.
"""

import asyncio
import logging
import datetime
import html
import os
import sys
from dataclasses import dataclass
from typing import Optional, Dict

from aiogram.exceptions import TelegramBadRequest
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.client.bot import DefaultBotProperties
from aiogram.types import (
    InlineKeyboardMarkup,
    ContentType,
    LabeledPrice,
    FSInputFile,
)
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Импортируем настройки из config.py
from config import (
    BOT_TOKEN,
    MAX_SYNCHRONOUS_TASKS,
    MAX_REQUESTS_PER_SECOND,
    MAX_REQUESTS_PER_HOUR,
    SAMPLE_CARDS,
    PAY_LINK,
)

import database

# Импортируем функции из database.py
from database import (
    init_db,
    register_user,
    get_random_card,
    mark_daily_card,
    can_ask_question,
    can_choose_daily_card,
    can_do_spread,
    subscription_info_text,
    list_users_paginated,
    set_subscription_status,
    extend_subscription,
    update_transaction_status,
    save_transaction,
)

# Импортируем модуль с запросами к GPT
from gpt_requests import (
    RequestDispatcher,
    ask_gpt_in_queue,
    get_card_description,
    get_shaman_oracle_description,
    get_goddess_union_description,
    ask_yandex_gpt,
    ask_cbt,
    get_spread_interpretation,
)

from utils import safe_edit_or_send, admin_required, markdown_to_telegram_html


# Импорт системы обработки ошибок (улучшённый декоратор и глобальный обработчик)
from errors import error_handler, handle_unhandled_exception, request_context


# ============================================================================
# Настройка логирования и глобального обработчика ошибок
# ============================================================================


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

loop = asyncio.get_event_loop()
loop.set_exception_handler(handle_unhandled_exception)


# ============================================================================
# ИНИЦИАЛИЗАЦИЯ БОТА И GPT
# ============================================================================


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)


@dataclass
class PendingSubAction:
    """
    Класс для хранения ожидаемых действий администратора по управлению подпиской.

    Атрибуты:
        action (str): Тип действия ("extend" для продления или "cancel_partial" для частичного отключения).
        target_tid (int): ID пользователя, для которого производится операция.
        timestamp (datetime.datetime): Время создания записи.
    """

    action: str
    target_tid: int
    timestamp: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)


# ============================================================================
# ФУНКЦИИ ВСПОМОГАТЕЛЬНОГО ХОДА
# =============================================================================


def approximate_tokens_count(text: str) -> int:
    """
    Упрощённый подсчёт "токенов" по количеству слов.

    Args:
        text (str): Исходный текст.

    Returns:
        int: Количество "токенов" (слов) в тексте.
    """
    return len(text.split())


# Глобальный экземпляр диспетчера очереди для GPT-запросов
request_dispatcher: RequestDispatcher = None

# Глобальное хранилище для ожидаемых вариантов вопроса (ключ: user_id, значение: строка)
pending_question_variants: dict[int, str] = {}

# Множество администраторов, находящихся в режиме рассылки
broadcast_mode_admins: set[int] = set()

# Глобальное хранилище состояния раскладов (ключ: user_id, значение: словарь с состоянием)
pending_spreads: dict[int, dict] = {}

# Хранилище ожидаемых действий для администраторов (ключ: admin_id)
pending_sub_actions: Dict[int, PendingSubAction] = {}

USERS_PER_PAGE = 4


# ============================================================================
# ФУНКЦИИ УПРАВЛЕНИЯ ПОДПИСКОЙ И УВЕДОМЛЕНИЯ
# ============================================================================


@error_handler(default_return=False)
async def check_subscription_notifications(telegram_id: int, chat_id: int) -> None:
    """
    Проверяет дату окончания подписки и, при необходимости, отправляет уведомление.

    Для пользователей с активной подпиской (premium) уведомление отправляется за 3 дня до окончания.

    Args:
        telegram_id (int): Идентификатор пользователя Telegram.
        chat_id (int): Идентификатор чата.
    """
    try:
        async with database.db.execute(
            "SELECT subscription_status, subscription_end FROM users WHERE telegram_id=?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
    except Exception as e:
        logger.exception("Ошибка проверки уведомлений по подписке: %s", e)
        return
    if not row:
        return

    sub_status, sub_end = row
    if not sub_end:
        return
    try:
        end_date = datetime.date.fromisoformat(sub_end)
    except Exception as e:
        logger.exception("Ошибка преобразования даты подписки: %s", e)
        return

    today = datetime.date.today()
    days_left = (end_date - today).days

    # Для пользователей с активной подпиской (premium) уведомляем о скором окончании
    if sub_status == "premium" and days_left == 3:
        try:
            await bot.send_message(
                chat_id,
                "Напоминаем, что ваша платная подписка истекает через 3 дня. Продлите подписку, чтобы не прерывать использование сервиса.",
            )
        except Exception as e:
            logger.exception("Ошибка отправки уведомления о продлении подписки: %s", e)


async def get_start_menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """
    Формирует текст и клавиатуру главного меню для команды /start.

    Если подписка не активна или просрочена, пользователю предлагается оформить подписку.
    При активной подписке отображается основное меню.

    Args:
        user_id (int): Идентификатор пользователя Telegram.

    Returns:
        tuple[str, InlineKeyboardMarkup]: Текст меню и разметка клавиатуры.
    """
    sub_status = None
    try:
        async with database.db.execute(
            "SELECT subscription_status FROM users WHERE telegram_id=?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                sub_status = row[0]
    except Exception as e:
        logger.exception("Ошибка получения статуса подписки: %s", e)

    if sub_status != "premium":
        text = (
            "Здравствуйте! Ваша подписка не активна или просрочена. "
            "Чтобы пользоваться ботом, оформите платную подписку.\n\n"
            "Выберите нужный пункт:"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="ℹ️ Справка и советы", callback_data="help")
        builder.button(text="💳 Купить подписку", callback_data="buy_subscription")
        keyboard = builder.as_markup()
    else:
        text = (
            "Здравствуйте! Добро пожаловать в бот ArcanaLens.\n\n"
            "Здесь вы можете:\n"
            "• Получить «карту дня» из колоды Таро\n"
            "• Задать вопрос профессиональному психологу\n"
            "• Узнать трактовку вашего расклада\n\n"
            "Выберите нужный пункт в меню ниже:"
        )
        keyboard = get_main_menu_keyboard()

    return text, keyboard


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Формирует главное меню в виде Inline-клавиатуры.

    Returns:
        InlineKeyboardMarkup: Разметка главного меню.
    """
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔮 Моя карта дня", callback_data="get_card")
        builder.button(text="❓ Задать вопрос психологу", callback_data="ask_question")
        builder.button(text="📜 Расклад", callback_data="spread")
        builder.button(text="💌 Мой статус", callback_data="info_sub")
        builder.button(text="ℹ️ Справка и советы", callback_data="help")
        builder.button(text="💳 Купить подписку", callback_data="buy_subscription")
        builder.adjust(1)
        return builder.as_markup()
    except Exception as e:
        logger.exception("Ошибка формирования главного меню: %s", e)
        return InlineKeyboardMarkup(inline_keyboard=[])


# ============================================================================
# ХЕНДЛЕРЫ Aiogram
# ============================================================================


@router.callback_query(F.data == "buy_subscription")
@error_handler(default_return=None)
async def choose_payment_method_handler(callback_query: types.CallbackQuery) -> None:
    """
    Обрабатывает выбор способа оплаты подписки и предлагает варианты.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Stars", callback_data="pay_stars")
    kb.button(text="🏦 Банковская карта", callback_data="pay_bank")
    kb.adjust(1)
    await callback_query.message.edit_text(
        "Выберите способ оплаты:", reply_markup=kb.as_markup()
    )
    await callback_query.answer()


# ---------- Оплата через Telegram Stars ----------
@router.callback_query(F.data == "pay_stars")
@error_handler(default_return=None)
async def buy_subscription_handler(callback_query: types.CallbackQuery) -> None:
    """
    Обрабатывает оплату подписки через Telegram Stars.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    title = "Полная подписка"
    description = (
        "• Расширенный функционал: ежедневные карты, возможность задавать вопросы психологу и получать трактовку расклада.\n"
        "Подписка действует 1 месяц.\n\n"
        "Стоимость: 500 ⭐️."
    )
    payload = "full_subscription_500"
    provider_token = ""  # Для Telegram Stars оставляем пустую строку
    currency = "XTR"  # Валюта для Stars
    prices = [LabeledPrice(label="Полная подписка", amount=500)]

    kb = InlineKeyboardBuilder()
    kb.button(text="Заплатить 500 ⭐️", pay=True)
    kb.button(text="🔰 Вернуться в меню", callback_data="main_menu")
    kb.adjust(1)

    await bot.send_invoice(
        chat_id=callback_query.from_user.id,
        title=title,
        description=description,
        payload=payload,
        provider_token=provider_token,
        currency=currency,
        prices=prices,
        start_parameter="full-subscription",
        reply_markup=kb.as_markup(),
    )
    await callback_query.answer()


@router.pre_checkout_query()
@error_handler(default_return=None)
async def pre_checkout_query_handler(
    pre_checkout_query: types.PreCheckoutQuery,
) -> None:
    """
    Обрабатывает pre-checkout запрос, подтверждая его выполнение.

    Args:
        pre_checkout_query (types.PreCheckoutQuery): Запрос на оплату.
    """
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
@error_handler(default_return=None)
async def successful_payment_handler(message: types.Message) -> None:
    """
    Обрабатывает успешную оплату подписки.

    Обновляет статус подписки пользователя, продлевает подписку на 1 месяц,
    сохраняет транзакцию и отправляет подтверждение пользователю.

    Args:
        message (types.Message): Сообщение с успешной оплатой.
    """
    telegram_id = message.from_user.id
    today = datetime.date.today()
    current_end_date = None

    try:
        async with database.db.execute(
            "SELECT subscription_end FROM users WHERE telegram_id=?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                current_end_date = datetime.date.fromisoformat(row[0])
        logger.info(f"Текущая дата окончания подписки: {current_end_date}")
    except Exception as e:
        logger.exception("Ошибка получения текущей даты подписки: %s", e)

    if current_end_date and current_end_date > today:
        new_end_date = current_end_date + datetime.timedelta(days=30)
    else:
        new_end_date = today + datetime.timedelta(days=30)
    subscription_end_date = new_end_date.isoformat()

    logger.info(f"Новая дата окончания подписки будет: {subscription_end_date}")

    await set_subscription_status(telegram_id, "premium")
    await extend_subscription(telegram_id, subscription_end_date)

    transaction_id = message.successful_payment.telegram_payment_charge_id
    amount = message.successful_payment.total_amount
    logger.info(f"Получен transaction_id: {transaction_id}, amount: {amount}")

    await save_transaction(telegram_id, transaction_id, amount, status="paid")

    try:
        async with database.db.execute(
            "SELECT transaction_id, status, date FROM transactions WHERE transaction_id=?",
            (transaction_id,),
        ) as cursor:
            row = await cursor.fetchone()
            logger.info(f"После обновления транзакции: {row}")
    except Exception as e:
        logger.exception("Ошибка выборки транзакции: %s", e)

    await message.answer(
        "Спасибо за покупку! Ваша полная подписка активирована на 1 месяц."
    )


# ---------- Оплата через банковскую карту ----------
@router.callback_query(F.data == "pay_bank")
@error_handler(default_return=None)
async def pay_bank_handler(callback_query: types.CallbackQuery) -> None:
    """
    Обрабатывает оплату подписки через банковскую карту.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    description = (
        "• Расширенный функционал: ежедневные карты, возможность задавать вопросы психологу и получать трактовку расклада.\n"
        "Подписка действует 1 месяц.\n\n"
        "Стоимость: 1000 руб."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="Купить", url=PAY_LINK)
    kb.button(text="🔰 Вернуться в меню", callback_data="main_menu")
    kb.adjust(1)

    await callback_query.message.edit_text(description, reply_markup=kb.as_markup())
    await callback_query.answer()


# ---------- Команда /start и возврат в главное меню ----------
@router.message(Command("start"))
@error_handler(default_return=None)
async def cmd_start(message: types.Message) -> None:
    """
    Обрабатывает команду /start:
    - Регистрирует пользователя.
    - Отправляет главное меню.
    - Проверяет уведомления о подписке.

    Args:
        message (types.Message): Сообщение пользователя.
    """
    request_context.set({"user_id": message.from_user.id, "command": "start"})
    user_id = message.from_user.id
    username = message.from_user.username or "Нет username"

    await register_user(user_id, username)

    menu_text, menu_keyboard = await get_start_menu(user_id)

    await check_subscription_notifications(user_id, message.chat.id)

    await message.answer(menu_text, reply_markup=menu_keyboard)


@router.callback_query(F.data == "main_menu")
@safe_edit_or_send
@error_handler(default_return=None)
async def process_main_menu(
    callback_query: types.CallbackQuery, **kwargs
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Обрабатывает запрос на возврат в главное меню.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
        **kwargs: Дополнительные параметры.

    Returns:
        tuple[str, InlineKeyboardMarkup]: Текст меню и клавиатура.
    """
    user_id = callback_query.from_user.id
    menu_text, menu_keyboard = await get_start_menu(user_id)
    return menu_text, menu_keyboard


# ---------- Обработка расклада ----------
@router.callback_query(F.data == "spread")
@error_handler(default_return=None)
async def process_spread_start(callback_query: types.CallbackQuery) -> None:
    """
    Инициирует процесс расклада, предлагая выбрать количество карт.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    text = "Выберите, сколько карт будет в вашем раскладе (от 1 до 3):"
    builder = InlineKeyboardBuilder()
    builder.button(text="1", callback_data="spread_num_1")
    builder.button(text="2", callback_data="spread_num_2")
    builder.button(text="3", callback_data="spread_num_3")
    builder.button(text="🔰 Вернуться в меню", callback_data="main_menu")
    builder.adjust(3)
    await callback_query.message.edit_text(text, reply_markup=builder.as_markup())
    await callback_query.answer()


@router.callback_query(F.data.startswith("spread_num_"))
@error_handler(default_return=None)
async def process_spread_num(callback_query: types.CallbackQuery) -> None:
    """
    Обрабатывает выбор количества карт для расклада и запрашивает ввод названий карт.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    user_id = callback_query.from_user.id
    num_str = callback_query.data.split("_")[-1]
    try:
        expected = int(num_str)
    except ValueError:
        expected = 1
    pending_spreads[user_id] = {"expected": expected, "cards": []}
    await callback_query.message.edit_text(
        f"Введите название карты номер 1 из {expected} (например, Маг):"
    )
    await callback_query.answer()


@router.message(
    lambda message: message.from_user.id in pending_spreads and message.text is not None
)
@error_handler(default_return=None)
async def spread_input_handler(message: types.Message) -> None:
    """
    Обрабатывает ввод названий карт для расклада.

    После ввода всех карт отправляет запрос для получения трактовки расклада.

    Args:
        message (types.Message): Сообщение пользователя с названием карты.
    """
    user_id = message.from_user.id
    state = pending_spreads[user_id]
    state["cards"].append(message.text.strip())
    expected = state["expected"]
    current = len(state["cards"])
    if current < expected:
        await message.answer(
            f"Введите название карты номер {current + 1} из {expected} (например, Маг):"
        )
    else:
        if not await can_do_spread(user_id):
            await message.answer("Вы уже сделали расклад сегодня. Попробуйте завтра.")
            del pending_spreads[user_id]
            return
        cards = state["cards"]
        del pending_spreads[user_id]
        await message.answer("Ваш расклад принят. Ожидайте трактовку...")
        interpretation = await ask_gpt_in_queue(
            dispatcher=request_dispatcher,
            chat_id=message.chat.id,
            user_id=user_id,
            func_to_call=get_spread_interpretation,
            cards=cards,
        )
        interpretation = markdown_to_telegram_html(interpretation)
        await message.answer(interpretation, parse_mode="HTML")

        builder = InlineKeyboardBuilder()
        builder.button(text="🔰 Вернуться в меню", callback_data="main_menu")
        await message.answer(
            "Что бы вы хотели сделать дальше?", reply_markup=builder.as_markup()
        )


# ---------- Обработка "Карты дня" ----------
@router.callback_query(F.data == "get_card")
@error_handler(default_return=None)
async def process_get_card(callback_query: types.CallbackQuery) -> None:
    """
    Предлагает пользователю выбрать колоду для получения карты дня.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    text = (
        "Выберите колоду для вашей карты дня:\n\n"
        "1. Оракул шамана мистика\n"
        "2. Союз богинь\n"
        "3. Классическое таро"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="Оракул шамана мистика", callback_data="daily_card_deck_1")
    builder.button(text="Союз богинь", callback_data="daily_card_deck_2")
    builder.button(text="Классическое таро", callback_data="daily_card_deck_3")
    builder.button(text="🔰 Вернуться в меню", callback_data="main_menu")
    builder.adjust(1)
    kb = builder.as_markup()
    await callback_query.message.edit_text(text, reply_markup=kb)
    await callback_query.answer()


@error_handler(default_return=None)
async def send_tarot_card_message(
    chat_id: int, card_name: str, description: str
) -> None:
    """
    Отправляет сообщение с изображением и описанием выбранной Таро-карты.

    Args:
        chat_id (int): Идентификатор чата.
        card_name (str): Название карты.
        description (str): Описание карты, полученное от GPT.
    """
    try:
        try:
            card_index = SAMPLE_CARDS.index(card_name)
        except ValueError:
            card_index = 0

        parts = card_name.split(" (")
        if len(parts) == 2:
            russian_name = parts[0].strip()
            english_name = parts[1].rstrip(")").strip()
            file_name = f"{russian_name}_{english_name}_{card_index}.webp"
        else:
            file_name = f"{card_name}_{card_index}.webp"

        photo_path = os.path.join("img", "classic", file_name)

        if not os.path.exists(photo_path):
            raise FileNotFoundError(f"Файл не найден: {photo_path}")

        safe_name = html.escape(card_name)
        safe_desc = html.escape(description)
        safe_desc = markdown_to_telegram_html(safe_desc)

        caption = f"✨ <b>Ваша карта дня</b>\n\n🎴 <b>{safe_name}</b>"

        photo = FSInputFile(photo_path)

        if len(caption) + len(safe_desc) <= 1024:
            caption += f"\n\n{safe_desc}"
            await bot.send_photo(
                chat_id, photo=photo, caption=caption, parse_mode="HTML"
            )
        else:
            await bot.send_photo(
                chat_id, photo=photo, caption=caption, parse_mode="HTML"
            )

            await bot.send_message(chat_id, safe_desc, parse_mode="HTML")

    except FileNotFoundError as e:
        await bot.send_message(chat_id, f"Ошибка: изображение не найдено.\n{str(e)}")
    except Exception as e:
        await bot.send_message(
            chat_id, f"Произошла ошибка при отправке карты:\n{str(e)}"
        )
        logger.exception("Ошибка в send_tarot_card_message: %s", e)


@router.callback_query(F.data.startswith("daily_card_deck_"))
@error_handler(default_return=None)
async def process_daily_card_deck(callback_query: types.CallbackQuery) -> None:
    """
    Обрабатывает выбор колоды для получения карты дня и запрашивает подтверждение.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    await callback_query.answer()
    user_id = callback_query.from_user.id
    chat_id = callback_query.message.chat.id
    deck_number = int(callback_query.data.split("_")[-1])

    text = "Вы уверены, что хотите получить карту дня из выбранной колоды?"
    builder = InlineKeyboardBuilder()
    builder.button(text="Подтвердить", callback_data=f"confirm_card_{deck_number}")
    builder.button(text="Отмена", callback_data="main_menu")
    await callback_query.message.edit_text(text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("confirm_card_"))
@error_handler(default_return=None)
async def confirm_card_generation(callback_query: types.CallbackQuery) -> None:
    """
    Подтверждает получение карты дня, запрашивает трактовку у GPT и отправляет пользователю.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    await callback_query.answer()
    user_id = callback_query.from_user.id
    chat_id = callback_query.message.chat.id

    await bot.delete_message(chat_id, callback_query.message.message_id)

    deck_number = int(callback_query.data.split("_")[-1])

    if not await can_choose_daily_card(user_id, deck_number):
        await callback_query.message.answer(
            "Вы уже использовали карту дня сегодня. Приходите завтра!"
        )
        return

    card_name = await get_random_card()
    if deck_number == 1:
        description = await ask_gpt_in_queue(
            dispatcher=request_dispatcher,
            chat_id=chat_id,
            user_id=user_id,
            func_to_call=get_shaman_oracle_description,
            card_name=card_name,
        )
    elif deck_number == 2:
        description = await ask_gpt_in_queue(
            dispatcher=request_dispatcher,
            chat_id=chat_id,
            user_id=user_id,
            func_to_call=get_goddess_union_description,
            card_name=card_name,
        )
    elif deck_number == 3:
        description = await ask_gpt_in_queue(
            dispatcher=request_dispatcher,
            chat_id=chat_id,
            user_id=user_id,
            func_to_call=get_card_description,
            card_name=card_name,
        )
    else:
        description = "Ошибка: неизвестная колода."

    await mark_daily_card(user_id, deck_number)
    await send_tarot_card_message(chat_id, card_name, description)

    builder = InlineKeyboardBuilder()
    builder.button(text="🔰 Вернуться в меню", callback_data="main_menu")
    await callback_query.message.answer(
        "Что бы вы хотели сделать дальше?", reply_markup=builder.as_markup()
    )


# ---------- Информация о подписке ----------
@router.callback_query(F.data == "info_sub")
@error_handler(default_return=None)
async def process_info_sub(callback_query: types.CallbackQuery) -> None:
    """
    Отправляет пользователю информацию о его подписке.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    user_id = callback_query.from_user.id
    text = await subscription_info_text(user_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="🔰 Вернуться в меню", callback_data="main_menu")
    kb = builder.as_markup()
    await callback_query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback_query.answer()


# ---------- Справка и советы ----------
@router.callback_query(F.data == "help")
@error_handler(default_return=None)
async def process_help(callback_query: types.CallbackQuery) -> None:
    """
    Отправляет справочную информацию и советы пользователю.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    text = (
        "📝 <b>Справка и советы</b>\n\n"
        "Выберите раздел, чтобы узнать подробности:\n\n"
        "1) Моя карта\n"
        "2) Задать вопрос\n"
        "3) Расклад\n"
        "4) Мой статус\n"
        "5) Купить подписку"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="Моя карта", callback_data="help_card")
    builder.button(text="Задать вопрос", callback_data="help_question")
    builder.button(text="Расклад", callback_data="help_spread")
    builder.button(text="Мой статус", callback_data="help_status")
    builder.button(text="Купить подписку", callback_data="help_subscription")
    builder.button(text="🔰 Вернуться в меню", callback_data="main_menu")
    builder.adjust(1)
    await callback_query.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback_query.answer()


@router.callback_query(F.data == "help_card")
@error_handler(default_return=None)
async def help_card(callback_query: types.CallbackQuery) -> None:
    """
    Отправляет справку по разделу "Моя карта".

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    text = (
        "🎴 <b>Моя карта</b>\n\n"
        "С помощью этого раздела вы получаете ежедневную карту дня. \n"
        "1. Нажмите «Моя карта» в главном меню.\n"
        "2. Выберите нужную колоду (например, «Оракул шамана мистика», «Союз богинь» или «Классическое таро»).\n"
        "3. Подтвердите выбор, и бот пришлёт вам карту дня с трактовкой."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🔰 Вернуться в справку", callback_data="help")
    await callback_query.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback_query.answer()


@router.callback_query(F.data == "help_question")
@error_handler(default_return=None)
async def help_question(callback_query: types.CallbackQuery) -> None:
    """
    Отправляет справку по разделу "Задать вопрос".

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    text = (
        "❓ <b>Задать вопрос</b>\n\n"
        "В этом разделе вы можете получить психологическую поддержку от нашего ИИ:\n"
        "1. Выберите «Задать вопрос» в главном меню.\n"
        "2. Затем выберите подход: транзактный анализ или КПТ.\n"
        "3. Введите свой вопрос одним сообщением.\n"
        "4. Получите ответ, сформированный с учётом выбранного метода."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🔰 Вернуться в справку", callback_data="help")
    await callback_query.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback_query.answer()


@router.callback_query(F.data == "help_spread")
@error_handler(default_return=None)
async def help_spread(callback_query: types.CallbackQuery) -> None:
    """
    Отправляет справку по разделу "Расклад".

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    text = (
        "📜 <b>Расклад</b>\n\n"
        "Расклад позволяет получить трактовку комбинации карт:\n"
        "1. Нажмите «Расклад» в главном меню.\n"
        "2. Выберите, сколько карт будет в раскладе (от 1 до 3).\n"
        "3. Введите названия карт по очереди.\n"
        "4. Бот предоставит подробную интерпретацию расклада, описывая значение каждой карты и их взаимосвязь."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🔰 Вернуться в справку", callback_data="help")
    await callback_query.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback_query.answer()


@router.callback_query(F.data == "help_status")
@error_handler(default_return=None)
async def help_status(callback_query: types.CallbackQuery) -> None:
    """
    Отправляет справку по разделу "Мой статус".

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    text = (
        "💌 <b>Мой статус</b>\n\n"
        "В этом разделе вы узнаете актуальную информацию о вашем аккаунте:\n"
        "1. Текущий статус подписки (expired, premium и т.д.).\n"
        "2. Дату окончания подписки.\n"
        "3. Информацию о лимите использования карт, вопросов и раскладов на сегодня."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🔰 Вернуться в справку", callback_data="help")
    await callback_query.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback_query.answer()


@router.callback_query(F.data == "help_subscription")
@error_handler(default_return=None)
async def help_subscription(callback_query: types.CallbackQuery) -> None:
    """
    Отправляет справку по разделу "Купить подписку".

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    text = (
        "💳 <b>Купить подписку</b>\n\n"
        "Приобретение подписки открывает расширенные возможности:\n"
        "1. Нажмите «Купить подписку» в главном меню.\n"
        "2. Ознакомьтесь с условиями (неограниченный доступ, повышенные лимиты и приоритетная обработка запросов).\n"
        "3. Оформите покупку, чтобы активировать платный период."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🔰 Вернуться в справку", callback_data="help")
    await callback_query.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback_query.answer()


# ---------- Задать вопрос ----------
@router.callback_query(F.data == "ask_question")
@error_handler(default_return=None)
async def ask_question_callback(callback_query: types.CallbackQuery) -> None:
    """
    Предлагает пользователю выбрать метод для получения ответа на вопрос.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    text = (
        "Выберите подход для получения ответа на ваш вопрос:\n\n"
        "1. Транзактный психоанализ\n"
        "2. Метод когнитивно-поведенческой терапии"
    )
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Транзактный психоанализ", callback_data="question_variant_transact"
    )
    builder.button(text="КПТ", callback_data="question_variant_cbt")
    builder.button(text="🔰 Вернуться в меню", callback_data="main_menu")
    builder.adjust(1)
    kb = builder.as_markup()
    await callback_query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback_query.answer()


@router.callback_query(
    F.data.in_(["question_variant_transact", "question_variant_cbt"])
)
@error_handler(default_return=None)
async def question_variant_selection(callback_query: types.CallbackQuery) -> None:
    """
    Обрабатывает выбор варианта ответа (транзактный анализ или КПТ) и запрашивает вопрос.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос пользователя.
    """
    user_id = callback_query.from_user.id
    if callback_query.data == "question_variant_transact":
        pending_question_variants[user_id] = "transact"
        chosen_text = "Транзактный психоанализ"
    else:
        pending_question_variants[user_id] = "cbt"
        chosen_text = "Когнитивно-поведенческая терапия"
    text = f"Вы выбрали подход: <b>{chosen_text}</b>.\n\nНапишите ваш вопрос одним сообщением."
    builder = InlineKeyboardBuilder()
    builder.button(text="🔰 Вернуться в меню", callback_data="main_menu")
    kb = builder.as_markup()
    await callback_query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback_query.answer()


# ---------- Админ-панель ----------
def get_admin_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Формирует главное меню администраторской панели.

    Returns:
        InlineKeyboardMarkup: Клавиатура админ-панели.
    """
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="Состояние очереди GPT", callback_data="admin_show_queue")
        builder.button(text="Список пользователей", callback_data="admin_list_users_0")
        builder.button(text="Очистить очередь GPT", callback_data="admin_clear_queue")
        builder.button(text="Рассылка сообщения", callback_data="admin_broadcast")
        builder.button(text="Возврат платежа", callback_data="admin_refund")
        builder.button(text="Запустить тест очереди", callback_data="run_test_queue")
        builder.button(text="Выключить бота", callback_data="admin_shutdown")
        builder.button(text="↩️ Вернуться в меню", callback_data="main_menu")
        builder.adjust(1)
        return builder.as_markup()
    except Exception as e:
        logger.exception("Ошибка формирования меню админа: %s", e)
        return InlineKeyboardMarkup()


@router.message(Command("admin"))
@admin_required
@error_handler(default_return=None)
async def cmd_admin_panel(message: types.Message) -> None:
    """
    Отправляет администратору главное меню админ-панели.

    Args:
        message (types.Message): Сообщение администратора.
    """
    text = "🔒 <b>Админ-панель</b>\n\nВыберите действие:"
    await message.answer(
        text, parse_mode="HTML", reply_markup=get_admin_menu_keyboard()
    )


async def send_long_message(chat_id: int, text: str, chunk_size: int = 4000) -> None:
    """
    Отправляет длинное сообщение по частям для избежания ограничения длины.

    Args:
        chat_id (int): Идентификатор чата.
        text (str): Текст сообщения.
        chunk_size (int): Максимальный размер одного сообщения.
    """
    for i in range(0, len(text), chunk_size):
        chunk = text[i : i + chunk_size]
        await bot.send_message(chat_id, chunk)
        await asyncio.sleep(1)


@router.callback_query(F.data == "run_test_queue")
@error_handler(default_return=None)
async def run_test_queue_handler(callback_query: types.CallbackQuery) -> None:
    """
    Запускает тестовую нагрузку очереди GPT-запросов для отладки.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    await callback_query.answer(
        "Запускается тестовая нагрузка, пожалуйста, подождите..."
    )
    chat_id = callback_query.message.chat.id
    tasks = []
    for i in range(50):
        # Для теста имитируем разных пользователей, прибавляя индекс к текущему id
        user_id = callback_query.from_user.id + i
        question = f"Тестовый запрос номер {i+1}"
        task = asyncio.create_task(
            ask_gpt_in_queue(
                dispatcher=request_dispatcher,
                chat_id=chat_id,
                user_id=user_id,
                func_to_call=ask_yandex_gpt,
                user_question=question,
            )
        )
        tasks.append(task)
    results = await asyncio.gather(*tasks)
    response_text = "\n".join([f"Запрос {i+1}: {res}" for i, res in enumerate(results)])
    if len(response_text) > 4000:
        await send_long_message(chat_id, f"Результаты теста:\n{response_text}")
    else:
        await bot.send_message(chat_id, f"Результаты теста:\n{response_text}")


@error_handler(default_return=None)
async def show_queue(callback_query: types.CallbackQuery) -> None:
    """
    Показывает текущее состояние очереди GPT-запросов.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    queued_count = request_dispatcher.queue.qsize()
    active_count = len(request_dispatcher.active_requests)
    details = ""

    if active_count > 0:
        details += "Активные задачи:\n"
        for i, req in enumerate(list(request_dispatcher.active_requests)[:5]):
            details += (
                f"{i+1}. Пользователь {req.user_id} — {req.func_to_call.__name__}\n"
            )
    else:
        details += "Нет активных задач.\n"

    snapshot = list(request_dispatcher.queue._queue)
    if snapshot:
        details += "\nЗадачи в очереди:\n"
        for i, req in enumerate(snapshot[:5]):
            details += (
                f"{i+1}. Пользователь {req.user_id} — {req.func_to_call.__name__}\n"
            )
    else:
        details += "\nНет задач в очереди."

    text = (
        f"📊 <b>Состояние очереди</b>\n\n"
        f"Ожидающих задач: <b>{queued_count}</b>\n"
        f"Активных задач: <b>{active_count}</b>\n"
        f"Всего задач: <b>{queued_count + active_count}</b>\n\n" + details
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="↩️ Назад", callback_data="admin_back")
    kb = builder.as_markup()
    await callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback_query.answer()


@router.callback_query(F.data.startswith("admin_list_users_"))
@admin_required
@error_handler(default_return=None)
async def admin_list_users_handler(callback_query: types.CallbackQuery) -> None:
    """
    Обрабатывает запрос на вывод списка пользователей с постраничной навигацией.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    try:
        page = int(callback_query.data.split("_")[-1])
    except ValueError:
        page = 0

    text, pages_count = await list_users_paginated(page, USERS_PER_PAGE)

    builder = InlineKeyboardBuilder()
    offset = page * USERS_PER_PAGE
    query = (
        "SELECT telegram_id, username FROM users ORDER BY user_id ASC LIMIT ? OFFSET ?"
    )
    async with database.db.execute(query, (USERS_PER_PAGE, offset)) as cursor:
        async for row in cursor:
            tid, username = row
            btn_text = (
                f"{username}" if username and username != "Нет username" else f"{tid}"
            )
            builder.button(text=btn_text, callback_data=f"admin_sub_user_{tid}")

    if page > 0:
        builder.button(text="⬅️", callback_data=f"admin_list_users_{page-1}")
    if page < pages_count - 1:
        builder.button(text="➡️", callback_data=f"admin_list_users_{page+1}")
    builder.button(text="↩️ Назад", callback_data="admin_back")
    builder.adjust(1)
    await callback_query.message.edit_text(
        text, parse_mode="HTML", reply_markup=builder.as_markup()
    )
    await callback_query.answer()


@router.callback_query(F.data.startswith("admin_sub_user_"))
@admin_required
@error_handler(default_return=None)
async def admin_sub_user_handler(callback_query: types.CallbackQuery) -> None:
    """
    Обрабатывает выбор пользователя для управления подпиской.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    try:
        target_tid = int(callback_query.data.split("_")[-1])
    except ValueError:
        await callback_query.answer("Неверные данные пользователя", show_alert=True)
        return

    text = f"Выберите действие для пользователя <b>{target_tid}</b>:"
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Продлить подписку", callback_data=f"admin_sub_extend_{target_tid}"
    )
    builder.button(
        text="Отменить подписку", callback_data=f"admin_sub_cancel_{target_tid}"
    )
    builder.button(text="↩️ Назад", callback_data="admin_list_users_0")
    builder.adjust(1)
    await callback_query.message.edit_text(
        text, parse_mode="HTML", reply_markup=builder.as_markup()
    )
    await callback_query.answer()


@router.callback_query(F.data.startswith("admin_sub_extend_"))
@admin_required
@error_handler(default_return=None)
async def admin_sub_extend_handler(callback_query: types.CallbackQuery) -> None:
    """
    Инициирует процесс продления подписки выбранного пользователя.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    try:
        target_tid = int(callback_query.data.split("_")[-1])
    except ValueError:
        await callback_query.answer("Неверные данные пользователя", show_alert=True)
        return

    admin_id = callback_query.from_user.id
    pending_sub_actions[admin_id] = PendingSubAction(
        action="extend", target_tid=target_tid
    )
    await callback_query.message.edit_text(
        f"Введите количество месяцев для продления подписки пользователя <b>{target_tid}</b>:",
        parse_mode="HTML",
    )
    await callback_query.answer()


@router.callback_query(
    lambda c: c.data.startswith("admin_sub_cancel_")
    and not c.data.startswith("admin_sub_cancel_full_")
    and not c.data.startswith("admin_sub_cancel_part_")
)
@admin_required
@error_handler(default_return=None)
async def admin_sub_cancel_menu(callback_query: types.CallbackQuery) -> None:
    """
    Отображает меню для выбора способа отмены подписки (полностью или частично).

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    try:
        target_tid = int(callback_query.data.split("_")[-1])
    except ValueError:
        await callback_query.answer("Неверные данные пользователя", show_alert=True)
        return

    text = f"Выберите действие для отмены подписки пользователя <b>{target_tid}</b>:"
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Отменить полностью", callback_data=f"admin_sub_cancel_full_{target_tid}"
    )
    builder.button(
        text="Отменить на определённое число месяцев",
        callback_data=f"admin_sub_cancel_part_{target_tid}",
    )
    builder.button(text="↩️ Назад", callback_data=f"admin_sub_user_{target_tid}")
    builder.adjust(1)
    await callback_query.message.edit_text(
        text, parse_mode="HTML", reply_markup=builder.as_markup()
    )
    await callback_query.answer()


@router.callback_query(F.data.startswith("admin_sub_cancel_full_"))
@admin_required
@error_handler(default_return=None)
async def admin_sub_cancel_full_handler(callback_query: types.CallbackQuery) -> None:
    """
    Полностью отменяет подписку выбранного пользователя.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    try:
        target_tid = int(callback_query.data.split("_")[-1])
    except ValueError:
        await callback_query.answer("Неверные данные пользователя", show_alert=True)
        return

    today = datetime.date.today()
    await set_subscription_status(target_tid, "expired")
    await extend_subscription(target_tid, today.isoformat())

    await callback_query.message.edit_text(
        f"Подписка пользователя <b>{target_tid}</b> отменена полностью.",
        parse_mode="HTML",
    )
    await callback_query.answer()


@router.callback_query(F.data.startswith("admin_sub_cancel_part_"))
@admin_required
@error_handler(default_return=None)
async def admin_sub_cancel_part_handler(callback_query: types.CallbackQuery) -> None:
    """
    Инициирует процесс частичного сокращения подписки выбранного пользователя.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    try:
        target_tid = int(callback_query.data.split("_")[-1])
    except ValueError:
        await callback_query.answer("Неверные данные пользователя", show_alert=True)
        return

    admin_id = callback_query.from_user.id
    pending_sub_actions[admin_id] = PendingSubAction(
        action="cancel_partial", target_tid=target_tid
    )
    await callback_query.message.answer(
        f"Введите количество месяцев, на которые нужно сократить подписку пользователя <b>{target_tid}</b>:",
        parse_mode="HTML",
    )
    await callback_query.answer()


@router.message(lambda message: message.text and message.text.strip().isdigit())
@admin_required
@error_handler(default_return=None)
async def process_pending_sub_action(message: types.Message) -> None:
    """
    Обрабатывает ввод администратора для продления или сокращения подписки.

    Args:
        message (types.Message): Сообщение администратора, содержащее число месяцев.
    """
    admin_id = message.from_user.id
    pending_action: Optional[PendingSubAction] = pending_sub_actions.get(admin_id)

    if not pending_action:
        return

    try:
        months = int(message.text.strip())
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число месяцев.")
        return

    today = datetime.date.today()
    async with database.db.execute(
        "SELECT subscription_end FROM users WHERE telegram_id=?",
        (pending_action.target_tid,),
    ) as cursor:
        row = await cursor.fetchone()
    if row and row[0]:
        try:
            current_end_date = datetime.date.fromisoformat(row[0])
        except Exception:
            current_end_date = today
    else:
        current_end_date = today

    if pending_action.action == "extend":
        if current_end_date < today:
            current_end_date = today
        new_end_date = current_end_date + datetime.timedelta(days=months * 30)
        await set_subscription_status(pending_action.target_tid, "premium")
        await extend_subscription(pending_action.target_tid, new_end_date.isoformat())
        response_text = (
            f"Подписка пользователя <b>{pending_action.target_tid}</b> продлена на {months} месяц(ев).\n"
            f"Новая дата окончания: {new_end_date.isoformat()}"
        )
    elif pending_action.action == "cancel_partial":
        new_end_date = current_end_date - datetime.timedelta(days=months * 30)
        if new_end_date <= today:
            await set_subscription_status(pending_action.target_tid, "expired")
            new_end_date = today
            response_text = (
                f"Подписка пользователя <b>{pending_action.target_tid}</b> отменена полностью, "
                f"так как сокращение на {months} месяц(ев) привело к окончанию подписки."
            )
        else:
            response_text = (
                f"Подписка пользователя <b>{pending_action.target_tid}</b> сокращена на {months} месяц(ев).\n"
                f"Новая дата окончания: {new_end_date.isoformat()}"
            )
        await extend_subscription(pending_action.target_tid, new_end_date.isoformat())
    else:
        response_text = "Неизвестное действие."

    pending_sub_actions.pop(admin_id, None)
    await message.answer(response_text, parse_mode="HTML")


@error_handler(default_return=None)
async def clear_gpt_queue(callback_query: types.CallbackQuery) -> None:
    """
    Очищает очередь GPT-запросов.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    n = len(request_dispatcher.queue._queue)
    request_dispatcher.queue._queue.clear()
    request_dispatcher.queue._unfinished_tasks = max(
        0, request_dispatcher.queue._unfinished_tasks - n
    )

    text = f"✅ Очередь GPT-запросов очищена (удалено {n} задач)."
    builder = InlineKeyboardBuilder()
    builder.button(text="↩️ Назад", callback_data="admin_back")
    kb = builder.as_markup()
    await callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback_query.answer()
    logger.info(
        f"Admin {callback_query.from_user.id} очистил очередь GPT-запросов (удалено {n} задач)."
    )


@router.callback_query(F.data == "admin_refund")
@admin_required
@error_handler(default_return=None)
async def admin_refund_button(callback_query: types.CallbackQuery) -> None:
    """
    Информирует администратора о порядке возврата платежа.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    instruction = (
        "Чтобы произвести возврат платежа, отправьте команду:\n"
        "<code>/refund &lt;ID транзакции&gt;</code>\n\n"
        "Например: <code>/refund 1234567890</code>"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="↩️ Назад", callback_data="admin_back")
    kb = builder.as_markup()
    await callback_query.message.answer(instruction, parse_mode="HTML", reply_markup=kb)
    await callback_query.answer()


@router.message(Command("refund"))
@admin_required
@error_handler(default_return=None)
async def admin_refund_command(message: types.Message) -> None:
    """
    Обрабатывает команду возврата платежа.

    Args:
        message (types.Message): Сообщение администратора.
    """
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Пожалуйста, отправьте команду в формате:\n<code>/refund &lt;ID транзакции&gt;</code>",
            parse_mode="HTML",
        )
        return

    t_id = parts[1].strip()

    async with database.db.execute(
        "SELECT telegram_id FROM transactions WHERE transaction_id = ?", (t_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        await message.answer("Транзакция с таким ID не найдена.")
        return
    client_telegram_id = row[0]
    today = datetime.date.today()

    try:
        await bot.refund_star_payment(
            user_id=client_telegram_id, telegram_payment_charge_id=t_id
        )
        await update_transaction_status(t_id, "refunded")

        async with database.db.execute(
            "SELECT subscription_end FROM users WHERE telegram_id=?",
            (client_telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            current_end_date = datetime.date.fromisoformat(row[0])
        else:
            current_end_date = today

        if current_end_date > today:
            new_end_date = current_end_date - datetime.timedelta(days=30)
            if new_end_date > today:
                await extend_subscription(client_telegram_id, new_end_date.isoformat())
            else:
                await set_subscription_status(client_telegram_id, "expired")
                await extend_subscription(client_telegram_id, today.isoformat())
        else:
            await set_subscription_status(client_telegram_id, "expired")
            await extend_subscription(client_telegram_id, today.isoformat())

        await message.answer("Возврат успешно произведен.")
    except TelegramBadRequest as e:
        err_text = "Ошибка возврата. Проверьте ID транзакции."
        if "CHARGE_ALREADY_REFUNDED" in e.message:
            err_text = "Платеж уже был возвращен."
        await message.answer(err_text)


@router.callback_query(F.data == "admin_shutdown")
@admin_required
@error_handler(default_return=None)
async def shutdown_confirmation(callback_query: types.CallbackQuery):
    """
    Запрашивает подтверждение выключения бота.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    text = (
        "Вы действительно хотите выключить бота?\n"
        "Это действие остановит все его процессы."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, выключить", callback_data="confirm_shutdown")
    builder.button(text="Отмена", callback_data="admin_back")
    await callback_query.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback_query.answer()


async def shutdown_bot():
    """
    Проводит graceful shutdown бота: останавливает polling, отменяет фоновые задачи,
    завершает работу диспетчера и закрывает сессию бота.
    """
    logger.info("Выполняется graceful shutdown бота.")
    try:
        await dp.stop_polling()
    except Exception as e:
        logger.exception("Ошибка остановки polling: %s", e)

    try:
        request_dispatcher.worker_task.cancel()
        await request_dispatcher.worker_task
    except asyncio.CancelledError:
        logger.info("Worker task успешно отменён.")

    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if tasks:
        logger.info("Отменяем все оставшиеся задачи...")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    try:
        dp.shutdown()
    except Exception as e:
        logger.exception("Ошибка завершения работы диспетчера: %s", e)
    try:
        await bot.session.close()
    except Exception as e:
        logger.exception("Ошибка закрытия сессии бота: %s", e)
    logger.info("Бот успешно завершил работу.")


@router.callback_query(F.data == "confirm_shutdown")
@admin_required
@error_handler(default_return=None)
async def confirm_shutdown_handler(callback_query: types.CallbackQuery):
    """
    Подтверждает выключение бота и инициирует процедуру shutdown.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    admin_id = callback_query.from_user.id
    await callback_query.answer("Бот выключается...")
    await callback_query.message.edit_text(
        "Бот выключается, до свидания!", parse_mode="HTML"
    )
    logger.info(f"Admin {admin_id} инициировал выключение бота.")
    asyncio.create_task(shutdown_bot())


@router.callback_query(F.data.startswith("admin_"))
@admin_required
@error_handler(default_return=None)
async def process_admin_callbacks(callback_query: types.CallbackQuery) -> None:
    """
    Обрабатывает общие callback-запросы администраторской панели.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    if callback_query.data.startswith("admin_sub_"):
        return

    if callback_query.data in ["admin_shutdown", "confirm_shutdown"]:
        return

    action = callback_query.data
    if action.startswith("admin_list_users_"):
        await admin_list_users_handler(callback_query)
        return
    if action == "admin_show_queue":
        await show_queue(callback_query)
        return
    if action == "admin_clear_queue":
        await clear_gpt_queue(callback_query)
        return
    if action == "admin_broadcast":
        await start_broadcast(callback_query)
        return
    if action == "admin_back":
        text = "🔒 <b>Админ-панель</b>\n\nВыберите действие:"
        await callback_query.message.edit_text(
            text, parse_mode="HTML", reply_markup=get_admin_menu_keyboard()
        )
        await callback_query.answer()
        return
    await callback_query.answer("Неизвестное действие.", show_alert=True)


# ---------- Рассылка сообщений ----------
@error_handler(default_return=None)
async def start_broadcast(callback_query: types.CallbackQuery) -> None:
    """
    Инициирует режим рассылки сообщений для администратора.

    Args:
        callback_query (types.CallbackQuery): Callback-запрос администратора.
    """
    admin_id = callback_query.from_user.id
    broadcast_mode_admins.add(admin_id)
    text = (
        "📢 <b>Рассылка сообщений</b>\n\n"
        "Отправьте сообщение, которое вы хотите разослать всем пользователям. "
        "Вы можете отправить любой тип сообщения: текст, фото, видео, документы, опросы и другие."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="↩️ Отмена и назад", callback_data="admin_back")
    kb = builder.as_markup()
    await callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback_query.answer()
    logger.info(f"Admin {admin_id} начал рассылку сообщений.")


@router.message(F.content_type == ContentType.ANY)
@error_handler(default_return=None)
async def broadcast_handler(message: types.Message) -> None:
    """
    Обрабатывает рассылку сообщений администратора.

    Определяет тип контента и отправляет его всем пользователям из базы данных.

    Args:
        message (types.Message): Сообщение администратора.
    """
    admin_id = message.from_user.id
    if admin_id in broadcast_mode_admins:
        broadcast_mode_admins.remove(admin_id)
        query = "SELECT telegram_id FROM users"
        async with database.db.execute(query) as cursor:
            all_users = [row[0] async for row in cursor]
        sent_count = 0
        failed_count = 0
        content_type = message.content_type
        media = None
        poll = None
        if content_type == ContentType.TEXT:
            media = {"type": "text", "text": message.text}
        elif content_type == ContentType.PHOTO:
            photo = message.photo[-1]
            media = {
                "type": "photo",
                "file_id": photo.file_id,
                "caption": message.caption,
            }
        elif content_type == ContentType.VIDEO:
            media = {
                "type": "video",
                "file_id": message.video.file_id,
                "caption": message.caption,
            }
        elif content_type == ContentType.DOCUMENT:
            media = {
                "type": "document",
                "file_id": message.document.file_id,
                "caption": message.caption,
            }
        elif content_type == ContentType.AUDIO:
            media = {
                "type": "audio",
                "file_id": message.audio.file_id,
                "caption": message.caption,
            }
        elif content_type == ContentType.STICKER:
            media = {"type": "sticker", "file_id": message.sticker.file_id}
        elif content_type == ContentType.POLL:
            poll = {
                "question": message.poll.question,
                "options": [option.text for option in message.poll.options],
                "is_anonymous": message.poll.is_anonymous,
                "type": message.poll.type,
                "allows_multiple_answers": message.poll.allows_multiple_answers,
                "correct_option_id": (
                    message.poll.correct_option_id
                    if message.poll.type == "quiz"
                    else None
                ),
            }
        elif content_type == ContentType.VOICE:
            media = {
                "type": "voice",
                "file_id": message.voice.file_id,
                "caption": message.caption,
            }
        elif content_type == ContentType.VIDEO_NOTE:
            media = {"type": "video_note", "file_id": message.video_note.file_id}
        elif content_type == ContentType.LOCATION:
            media = {
                "type": "location",
                "latitude": message.location.latitude,
                "longitude": message.location.longitude,
            }
        elif content_type == ContentType.CONTACT:
            media = {
                "type": "contact",
                "phone_number": message.contact.phone_number,
                "first_name": message.contact.first_name,
                "last_name": (
                    message.contact.last_name if message.contact.last_name else ""
                ),
            }
        elif content_type == ContentType.DICE:
            media = {"type": "dice", "emoji": message.dice.emoji}
        else:
            await message.answer("Не поддерживаемый тип сообщения для рассылки.")
            return

        for t_id in all_users:
            try:
                if media:
                    if media["type"] == "text":
                        await bot.send_message(t_id, media["text"], parse_mode="HTML")
                    elif media["type"] == "photo":
                        await bot.send_photo(
                            t_id,
                            media["file_id"],
                            caption=media.get("caption"),
                            parse_mode="HTML",
                        )
                    elif media["type"] == "video":
                        await bot.send_video(
                            t_id,
                            media["file_id"],
                            caption=media.get("caption"),
                            parse_mode="HTML",
                        )
                    elif media["type"] == "document":
                        await bot.send_document(
                            t_id,
                            media["file_id"],
                            caption=media.get("caption"),
                            parse_mode="HTML",
                        )
                    elif media["type"] == "audio":
                        await bot.send_audio(
                            t_id,
                            media["file_id"],
                            caption=media.get("caption"),
                            parse_mode="HTML",
                        )
                    elif media["type"] == "sticker":
                        await bot.send_sticker(t_id, media["file_id"])
                    elif media["type"] == "voice":
                        await bot.send_voice(
                            t_id,
                            media["file_id"],
                            caption=media.get("caption"),
                            parse_mode="HTML",
                        )
                    elif media["type"] == "video_note":
                        await bot.send_video_note(t_id, media["file_id"])
                    elif media["type"] == "location":
                        await bot.send_location(
                            t_id, media["latitude"], media["longitude"]
                        )
                    elif media["type"] == "contact":
                        await bot.send_contact(
                            t_id,
                            phone_number=media["phone_number"],
                            first_name=media["first_name"],
                            last_name=media["last_name"],
                        )
                    elif media["type"] == "dice":
                        await bot.send_dice(t_id, emoji=media["emoji"])
                elif poll:
                    await bot.send_poll(
                        chat_id=t_id,
                        question=poll["question"],
                        options=poll["options"],
                        is_anonymous=poll["is_anonymous"],
                        type=poll["type"],
                        allows_multiple_answers=poll["allows_multiple_answers"],
                        correct_option_id=poll["correct_option_id"],
                    )
                sent_count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning(
                    f"Не удалось отправить сообщение пользователю {t_id}: {e}"
                )
                failed_count += 1
        await message.answer(
            f"📢 Рассылка завершена.\nОтправлено: {sent_count} пользователям.\nНе удалось отправить: {failed_count} пользователям."
        )
        logger.info(
            f"Admin {admin_id} завершил рассылку сообщений. Отправлено: {sent_count}, Не удалось: {failed_count}."
        )
    else:
        await fallback_handler(message)


# ---------- Обработка свободного текста (ожидание вопроса) ----------
@router.message(F.content_type == ContentType.TEXT)
@error_handler(default_return=None)
async def fallback_handler(message: types.Message) -> None:
    """
    Обрабатывает свободный текст от пользователя.

    Если пользователь ожидал ответа на свой вопрос, обрабатывается запрос к GPT,
    иначе предлагается перейти в режим вопросов.

    Args:
        message (types.Message): Сообщение пользователя.
    """
    user_id = message.from_user.id
    chat_id = message.chat.id
    text_in = (message.text or "").strip()
    username = message.from_user.username or "Нет username"
    await register_user(user_id, username)
    if user_id in pending_question_variants:
        variant = pending_question_variants.pop(user_id)
        if not await can_ask_question(user_id):
            await message.answer(
                "Вы исчерпали лимит вопросов на сегодня. Пожалуйста, попробуйте завтра."
            )
            return
        if variant == "transact":
            answer = await ask_gpt_in_queue(
                dispatcher=request_dispatcher,
                chat_id=chat_id,
                user_id=user_id,
                func_to_call=ask_yandex_gpt,
                user_question=text_in,
            )
        elif variant == "cbt":
            answer = await ask_gpt_in_queue(
                dispatcher=request_dispatcher,
                chat_id=chat_id,
                user_id=user_id,
                func_to_call=ask_cbt,
                user_question=text_in,
            )
        else:
            answer = "Ошибка: неизвестный вариант ответа."
        safe_ans = html.escape(answer)
        safe_q = html.escape(text_in)
        safe_q = markdown_to_telegram_html(safe_q)
        resp_text = f"📝 <b>Ваш вопрос</b>:\n{safe_q}\n\n💬 <b>Ответ</b>:\n{safe_ans}"
        resp_text = markdown_to_telegram_html(resp_text)
        await message.answer(resp_text, parse_mode="HTML")
        builder = InlineKeyboardBuilder()
        builder.button(text="🔰 Вернуться в меню", callback_data="main_menu")
        kb = builder.as_markup()
        await message.answer("Готово! Чем ещё можем помочь?", reply_markup=kb)
    else:
        text = "Кажется, вы ввели что-то не по теме. Чтобы задать вопрос, нажмите «Задать вопрос психологу»."
        builder = InlineKeyboardBuilder()
        builder.button(text="🔰 Вернуться в меню", callback_data="main_menu")
        kb = builder.as_markup()
        await message.answer(text, reply_markup=kb)


# ============================================================================
# ЗАПУСК БОТА
# ============================================================================


async def dump_all_tasks() -> None:
    """
    Выводит список всех запущенных задач (для отладки).
    """
    tasks = asyncio.all_tasks()
    for task in tasks:
        print(task)


async def main() -> None:
    """
    Основная функция запуска бота:
    - Инициализирует базу данных.
    - Настраивает диспетчер очереди GPT-запросов.
    - Запускает polling для получения обновлений.
    """

    await init_db()

    if database.db is None:
        raise RuntimeError("Database is not initialized!")

    global request_dispatcher
    request_dispatcher = RequestDispatcher(
        bot=bot,
        max_tasks=MAX_SYNCHRONOUS_TASKS,
        req_per_sec=MAX_REQUESTS_PER_SECOND,
        req_per_hour=MAX_REQUESTS_PER_HOUR,
    )

    logger.info("Бот запущен и готов к работе.")
    await dump_all_tasks()
    await dp.start_polling(bot)
    logger.info("Polling завершён. Завершаем работу.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception("Критическая ошибка запуска бота: %s", e)
    finally:
        sys.exit(0)
