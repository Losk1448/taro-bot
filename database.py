# database.py

"""
Модуль database для работы с базой данных с использованием aiosqlite.
Содержит функции инициализации, регистрации пользователя, получения случайной карты,
отметки выбора карты дня, обновления подписки, проверки лимитов и получения информации о пользователе.
"""

import logging
import datetime
import aiosqlite
import html
from typing import Optional, Tuple

from config import SAMPLE_CARDS  # Импортируем список карт из конфигурационного файла
from errors import error_handler  # Импорт декоратора для обработки ошибок

# Глобальное соединение с базой данных
db: Optional[aiosqlite.Connection] = None


@error_handler(default_return=None)
async def init_db() -> None:
    """
    Инициализирует базу данных:
      - Устанавливает соединение с файлом базы данных
      - Устанавливает режим WAL
      - Создаёт таблицы: users, transactions, tarot_cards, daily_cards
      - Заполняет таблицу tarot_cards начальными данными, если она пуста
    """
    global db
    try:
        db = await aiosqlite.connect("database.db", check_same_thread=False)
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.commit()
    except Exception as e:
        logging.exception("Ошибка подключения к базе данных: %s", e)
        return

    # Создание таблицы пользователей
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                username TEXT,
                subscription_status TEXT DEFAULT 'expired',
                subscription_end DATE,
                last_card_date DATE,
                questions_count INTEGER DEFAULT 0,
                questions_date DATE,
                spreads_count INTEGER DEFAULT 0,
                spreads_date DATE
            )
            """
        )
        await db.commit()
    except Exception as e:
        logging.exception("Ошибка создания таблицы users: %s", e)

    # Создание таблицы транзакций
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY,
                telegram_id INTEGER,
                amount INTEGER,
                status TEXT,
                date TEXT
            )
            """
        )
        await db.commit()
    except Exception as e:
        logging.exception("Ошибка создания таблицы transactions: %s", e)

    # Попытка добавить недостающие колонки, если они ещё не существуют
    try:
        await db.execute(
            "ALTER TABLE users ADD COLUMN questions_count INTEGER DEFAULT 0;"
        )
        await db.commit()
    except aiosqlite.OperationalError:
        logging.info("Поле 'questions_count' уже существует в таблице 'users'.")
    try:
        await db.execute("ALTER TABLE users ADD COLUMN questions_date DATE;")
        await db.commit()
    except aiosqlite.OperationalError:
        logging.info("Поле 'questions_date' уже существует в таблице 'users'.")

    # Создание таблицы карт таро
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tarot_cards (
                card_id INTEGER PRIMARY KEY,
                card_name TEXT
            )
            """
        )
        await db.commit()
    except Exception as e:
        logging.exception("Ошибка создания таблицы tarot_cards: %s", e)

    # Заполнение таблицы tarot_cards начальными данными, если таблица пуста
    try:
        async with db.execute("SELECT COUNT(*) FROM tarot_cards") as cursor:
            row = await cursor.fetchone()
            count = row[0] if row else 0

        if count == 0:
            tarot_data = [(i, name) for i, name in enumerate(SAMPLE_CARDS)]
            await db.executemany(
                "INSERT INTO tarot_cards (card_id, card_name) VALUES (?, ?)",
                tarot_data,
            )
            await db.commit()
    except Exception as e:
        logging.exception("Ошибка заполнения таблицы tarot_cards: %s", e)

    # Создание таблицы для отслеживания выбора карты дня
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_cards (
                telegram_id INTEGER,
                card_deck INTEGER,
                date TEXT,
                PRIMARY KEY (telegram_id, card_deck, date)
            )
            """
        )
        await db.commit()
    except Exception as e:
        logging.exception("Ошибка создания таблицы daily_cards: %s", e)

    logging.info("База данных инициализирована.")


@error_handler(default_return=None)
async def register_user(telegram_id: int, username: Optional[str] = None) -> None:
    """
    Регистрирует нового пользователя или обновляет имя уже зарегистрированного.
    При регистрации нового пользователя устанавливается статус 'expired',
    что означает отсутствие активной подписки.

    Args:
        telegram_id (int): Идентификатор Telegram пользователя.
        username (Optional[str]): Имя пользователя (если указано).
    """
    try:
        async with db.execute(
            "SELECT user_id, username FROM users WHERE telegram_id=?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
    except Exception as e:
        logging.exception("Ошибка выборки пользователя: %s", e)
        return

    today_str: str = datetime.date.today().isoformat()
    if not row:
        try:
            await db.execute(
                """
                INSERT INTO users (telegram_id, username, subscription_status, subscription_end, questions_count, questions_date)
                VALUES (?, ?, 'expired', NULL, 0, ?)
                """,
                (telegram_id, username, today_str),
            )
            await db.commit()
            logging.info(
                f"Новый пользователь зарегистрирован: {telegram_id} (@{username})"
            )
        except Exception as e:
            logging.exception("Ошибка регистрации пользователя: %s", e)
    else:
        _, existing_username = row
        if username and username != existing_username:
            try:
                await db.execute(
                    "UPDATE users SET username=? WHERE telegram_id=?",
                    (username, telegram_id),
                )
                await db.commit()
                logging.info(
                    f"Username обновлён для пользователя {telegram_id}: @{username}"
                )
            except Exception as e:
                logging.exception("Ошибка обновления username: %s", e)


@error_handler(default_return="Неизвестная карта")
async def get_random_card() -> str:
    """
    Возвращает название случайной карты из таблицы tarot_cards.

    Returns:
        str: Название случайной карты или "Неизвестная карта" при ошибке.
    """
    try:
        async with db.execute(
            "SELECT card_name FROM tarot_cards ORDER BY RANDOM() LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else "Неизвестная карта"
    except Exception as e:
        logging.exception("Ошибка получения случайной карты: %s", e)
        return "Неизвестная карта"


@error_handler(default_return=False)
async def mark_daily_card(telegram_id: int, deck_number: int) -> None:
    """
    Отмечает, что пользователь выбрал карту дня для конкретной колоды в текущий день.

    Args:
        telegram_id (int): Идентификатор Telegram пользователя.
        deck_number (int): Номер колоды, из которой выбирается карта дня.
    """
    today_str: str = datetime.date.today().isoformat()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO daily_cards (telegram_id, card_deck, date) VALUES (?, ?, ?)",
            (telegram_id, deck_number, today_str),
        )
        await db.commit()
    except Exception as e:
        logging.exception("Ошибка отметки выбранной карты: %s", e)


@error_handler(default_return=False)
async def refresh_subscription_status(telegram_id: int) -> None:
    """
    Проверяет, истёк ли срок подписки пользователя.
    Если срок подписки истёк, обновляет статус на 'expired'.

    Args:
        telegram_id (int): Идентификатор Telegram пользователя.
    """
    try:
        async with db.execute(
            "SELECT subscription_status, subscription_end FROM users WHERE telegram_id=?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return
        sub_status, sub_end = row
        if not sub_end:
            return
        if sub_status == "expired":
            return

        end_date = datetime.date.fromisoformat(sub_end)
        today = datetime.date.today()
        if today > end_date:
            await db.execute(
                "UPDATE users SET subscription_status='expired' WHERE telegram_id=?",
                (telegram_id,),
            )
            await db.commit()
    except Exception as e:
        logging.exception("Ошибка обновления статуса подписки: %s", e)


@error_handler(default_return=None)
async def save_transaction(
    telegram_id: int, transaction_id: str, amount: int, status: str = "paid"
) -> None:
    """
    Сохраняет транзакцию в базе данных или обновляет существующую запись при конфликте.

    Args:
        telegram_id (int): Идентификатор Telegram пользователя.
        transaction_id (str): Уникальный идентификатор транзакции.
        amount (int): Сумма платежа.
        status (str): Статус транзакции (по умолчанию "paid").
    """
    date_str: str = datetime.datetime.now().isoformat()
    try:
        await db.execute(
            """
            INSERT INTO transactions (transaction_id, telegram_id, amount, status, date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(transaction_id) DO UPDATE SET
                telegram_id=excluded.telegram_id,
                amount=excluded.amount,
                status=excluded.status,
                date=excluded.date
            """,
            (transaction_id, telegram_id, amount, status, date_str),
        )
        await db.commit()
        logging.info(
            f"Транзакция {transaction_id} сохранена для пользователя {telegram_id}."
        )
    except Exception as e:
        logging.exception("Ошибка сохранения транзакции: %s", e)


@error_handler(default_return=None)
async def update_transaction_status(transaction_id: str, new_status: str) -> None:
    """
    Обновляет статус транзакции в базе данных.

    Args:
        transaction_id (str): Уникальный идентификатор транзакции.
        new_status (str): Новый статус транзакции.
    """
    try:
        await db.execute(
            "UPDATE transactions SET status=? WHERE transaction_id=?",
            (new_status, transaction_id),
        )
        await db.commit()
        logging.info(f"Статус транзакции {transaction_id} обновлен на {new_status}.")
    except Exception as e:
        logging.exception("Ошибка обновления статуса транзакции: %s", e)


@error_handler(default_return=False)
async def can_ask_question(telegram_id: int) -> bool:
    """
    Проверяет, может ли пользователь задать вопрос, учитывая лимит вопросов на день.
    Для пользователей с неактивной подпиской (не premium) возвращается False.

    Args:
        telegram_id (int): Идентификатор Telegram пользователя.

    Returns:
        bool: True, если можно задать вопрос, иначе False.
    """
    await refresh_subscription_status(telegram_id)
    try:
        async with db.execute(
            "SELECT subscription_status, questions_count, questions_date FROM users WHERE telegram_id=?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
    except Exception as e:
        logging.exception("Ошибка проверки лимита вопросов: %s", e)
        return False

    if not row:
        return False

    sub_status = row[0]
    if sub_status != "premium":
        return False

    try:
        await db.execute(
            "UPDATE users SET questions_count = questions_count + 1, questions_date = ? WHERE telegram_id=?",
            (datetime.date.today().isoformat(), telegram_id),
        )
        await db.commit()
    except Exception as e:
        logging.exception("Ошибка увеличения счетчика вопросов: %s", e)
    return True


@error_handler(default_return=False)
async def can_choose_daily_card(telegram_id: int, deck_number: int) -> bool:
    """
    Проверяет, может ли пользователь выбрать карту дня для заданной колоды.
    Выбор разрешён только для пользователей с активной подпиской (premium)
    и если карта не была выбрана в текущий день.

    Args:
        telegram_id (int): Идентификатор Telegram пользователя.
        deck_number (int): Номер колоды.

    Returns:
        bool: True, если выбор карты возможен, иначе False.
    """
    await refresh_subscription_status(telegram_id)

    try:
        async with db.execute(
            "SELECT subscription_status FROM users WHERE telegram_id=?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
    except Exception as e:
        logging.exception("Ошибка проверки подписки: %s", e)
        return False

    if not row:
        return False

    sub_status = row[0]
    if sub_status != "premium":
        return False

    today_str = datetime.date.today().isoformat()
    try:
        async with db.execute(
            "SELECT COUNT(*) FROM daily_cards WHERE telegram_id=? AND card_deck=? AND date=?",
            (telegram_id, deck_number, today_str),
        ) as cursor:
            row = await cursor.fetchone()
        used_deck_today = row[0] if row else 0
        return used_deck_today == 0
    except Exception as e:
        logging.exception("Ошибка проверки выбранной колоды для premium: %s", e)
        return False


@error_handler(default_return=False)
async def can_do_spread(telegram_id: int) -> bool:
    """
    Проверяет, может ли пользователь выполнить расклад сегодня.
    Для выполнения расклада требуется активная подписка (premium) и не превышен лимит раскладов.

    Args:
        telegram_id (int): Идентификатор Telegram пользователя.

    Returns:
        bool: True, если пользователь может сделать расклад, иначе False.
    """
    await refresh_subscription_status(telegram_id)
    try:
        async with db.execute(
            "SELECT subscription_status FROM users WHERE telegram_id=?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
    except Exception as e:
        logging.exception("Ошибка проверки подписки: %s", e)
        return False

    if not row or row[0] != "premium":
        return False

    async with db.execute(
        "SELECT spreads_count, spreads_date FROM users WHERE telegram_id=?",
        (telegram_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return False
    spreads_count, spreads_date = row
    today_str: str = datetime.date.today().isoformat()
    if spreads_date != today_str:
        try:
            await db.execute(
                "UPDATE users SET spreads_count=0, spreads_date=? WHERE telegram_id=?",
                (today_str, telegram_id),
            )
            await db.commit()
        except Exception as e:
            logging.exception("Ошибка сброса счетчика раскладов: %s", e)
    limit = 7  # для premium-пользователей
    if spreads_count >= limit:
        return False
    try:
        await db.execute(
            "UPDATE users SET spreads_count = spreads_count + 1 WHERE telegram_id=?",
            (telegram_id,),
        )
        await db.commit()
    except Exception as e:
        logging.exception("Ошибка увеличения счетчика раскладов: %s", e)
    return True


@error_handler(
    default_return="Пользователь не найден. Нажмите /start, чтобы начать заново."
)
async def subscription_info_text(telegram_id: int) -> str:
    """
    Формирует информационное сообщение о статусе подписки, количестве оставшихся карт,
    вопросов и раскладов на сегодня.

    Args:
        telegram_id (int): Идентификатор Telegram пользователя.

    Returns:
        str: Форматированное информационное сообщение для пользователя.
    """
    async with db.execute(
        "SELECT subscription_status, subscription_end, last_card_date, questions_count, questions_date, spreads_count, spreads_date FROM users WHERE telegram_id=?",
        (telegram_id,),
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        return "Пользователь не найден. Нажмите /start, чтобы начать заново."

    (
        sub_status,
        sub_end,
        last_card_date,
        questions_count,
        questions_date,
        spreads_count,
        spreads_date,
    ) = row

    sub_end_str: str = html.escape(str(sub_end)) if sub_end else "не указана"
    today_str: str = datetime.date.today().isoformat()

    if sub_status == "premium":
        if questions_date != today_str:
            current_questions: int = 0
        else:
            current_questions = questions_count if questions_count is not None else 0
        available_questions: int = max(0, 7 - current_questions)

        async with db.execute(
            "SELECT COUNT(*) FROM daily_cards WHERE telegram_id=? AND date=?",
            (telegram_id, today_str),
        ) as cursor:
            row2 = await cursor.fetchone()
        used: int = row2[0] if row2 else 0
        left_cards: int = max(0, 3 - used)
        total_cards: int = 3

        if spreads_date != today_str:
            current_spreads: int = 0
        else:
            current_spreads = spreads_count if spreads_count is not None else 0
        available_spreads: int = max(0, 7 - current_spreads)
    else:
        available_questions = 0
        left_cards = 0
        total_cards = 3
        available_spreads = 0

    return (
        f"💎 <b>Ваш статус подписки:</b> {html.escape(sub_status)}\n"
        f"📆 <b>Подписка действует до:</b> {html.escape(str(sub_end_str))}\n\n"
        f"🃏 <b>Карт сегодня:</b> <code>{left_cards}</code> из <code>{total_cards}</code>\n"
        f"❓ <b>Вопросов сегодня:</b> <code>{available_questions}</code> из <code>7</code>\n"
        f"🔮 <b>Раскладов сегодня:</b> <code>{available_spreads}</code> из <code>7</code>\n\n"
        "Чтобы воспользоваться всеми возможностями бота, оформите платную подписку."
    )


@error_handler(default_return=None)
async def list_users_paginated(page: int, users_per_page: int = 5) -> Tuple[str, int]:
    """
    Возвращает строку с информацией о пользователях для указанной страницы,
    а также общее количество страниц.

    Для каждого пользователя дополнительно выводится статус подписки и информация
    о последней транзакции (если она существует).

    Args:
        page (int): Номер текущей страницы (начиная с 0).
        users_per_page (int): Количество пользователей на странице (по умолчанию 5).

    Returns:
        Tuple[str, int]: Кортеж, где первый элемент - форматированный текст,
                         второй элемент - общее число страниц.
    """
    count_query: str = "SELECT COUNT(*) FROM users"
    async with db.execute(count_query) as cursor:
        row = await cursor.fetchone()
        total_users: int = row[0] if row else 0

    offset: int = page * users_per_page
    query: str = """SELECT telegram_id, username, subscription_status, subscription_end
                    FROM users
                    ORDER BY user_id ASC
                    LIMIT ? OFFSET ?"""
    users_text: str = ""
    async with db.execute(query, (users_per_page, offset)) as cursor:
        idx: int = 0
        async for row in cursor:
            idx += 1
            tid, username, sub_status, sub_end = row
            user_display: str = (
                f"{tid} (@{username})"
                if username and username != "Нет username"
                else f"{tid} (Нет username)"
            )
            async with db.execute(
                "SELECT transaction_id, status FROM transactions WHERE telegram_id=? ORDER BY date DESC LIMIT 1",
                (tid,),
            ) as tr_cursor:
                transaction_row = await tr_cursor.fetchone()
            if transaction_row:
                last_transaction_id, tr_status = transaction_row
                transaction_info: str = (
                    f", транзакция: {last_transaction_id} ({tr_status})"
                )
            else:
                transaction_info = ""
            users_text += f"{offset + idx}. <b>{user_display}</b> — {sub_status}, до {sub_end}{transaction_info}\n"

    if not users_text:
        users_text = "Нет записей на этой странице."
    pages_count: int = (total_users + users_per_page - 1) // users_per_page
    text: str = (
        "👥 <b>Список пользователей</b>\n\n"
        f"Всего: <b>{total_users}</b>\n\n"
        f"{users_text}\n"
        f"<i>Страница {page+1} из {pages_count}</i>"
    )
    return text, pages_count


@error_handler(default_return=None)
async def set_subscription_status(telegram_id: int, new_status: str) -> None:
    """
    Изменяет статус подписки пользователя.

    Args:
        telegram_id (int): Идентификатор Telegram пользователя.
        new_status (str): Новый статус подписки.
    """
    await db.execute(
        "UPDATE users SET subscription_status=? WHERE telegram_id=?",
        (new_status, telegram_id),
    )
    await db.commit()


@error_handler(default_return=None)
async def extend_subscription(telegram_id: int, new_date_str: str) -> None:
    """
    Обновляет дату окончания подписки пользователя.

    Args:
        telegram_id (int): Идентификатор Telegram пользователя.
        new_date_str (str): Новая дата окончания подписки в формате ISO.
    """
    await db.execute(
        "UPDATE users SET subscription_end=? WHERE telegram_id=?",
        (new_date_str, telegram_id),
    )
    await db.commit()
