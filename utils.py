# utils.py

"""
Модуль utils содержит вспомогательные функции для преобразования Markdown в HTML,
безопасного редактирования сообщений и проверки прав администратора.

Функции:
  - markdown_to_telegram_html: Преобразует базовые элементы Markdown в HTML-теги.
  - safe_edit_or_send: Декоратор для безопасного редактирования или отправки сообщения.
  - admin_required: Декоратор для проверки прав администратора.
"""

import re
from functools import wraps
from typing import cast
from typing import Any, Callable, Awaitable, Tuple, TypeVar

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup

from config import ADMIN_IDS


def markdown_to_telegram_html(md_text: str) -> str:
    """
    Преобразует базовые элементы Markdown в HTML-теги, совместимые с Telegram.

    Поддерживаемые преобразования:
      - Заголовок "### текст" преобразуется в <b>текст</b>
      - Элементы списка, начинающиеся с "*" заменяются на "• " с сохранением отступов
      - \*\*жирный текст** преобразуется в <b>жирный текст</b>
      - *курсив* преобразуется в <i>курсив</i>
      - \~\~зачеркнутый текст~~ преобразуется в <s>зачеркнутый текст</s>
      - \`код` преобразуется в <code>код</code>

    Args:
        md_text (str): Исходный текст в формате Markdown.

    Returns:
        str: Текст, преобразованный в HTML-разметку для Telegram.
    """
    html_text = md_text
    html_text = re.sub(r"(?m)^###\s+(.+)$", r"<b>\1</b>", html_text)
    html_text = re.sub(r"(?m)^\s*\*\s+", "• ", html_text)
    html_text = re.sub(r"`([^`]+?)`", r"<code>\1</code>", html_text)
    html_text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html_text)
    html_text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", html_text)
    html_text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", html_text)
    return html_text


# Определяем тип для декорируемых функций, возвращающих кортеж (str, InlineKeyboardMarkup)
F = TypeVar("F", bound=Callable[..., Awaitable[Tuple[str, InlineKeyboardMarkup]]])


def safe_edit_or_send(func: F) -> F:
    """
    Декоратор для безопасного редактирования сообщения или отправки нового, если редактирование невозможно.

    Если функция-обработчик возвращает кортеж (text, reply_markup), то происходит попытка
    редактирования исходного сообщения. Если сообщение нельзя редактировать (например, оно слишком старое),
    то текст отправляется в виде нового сообщения.

    Args:
        func: Функция-обработчик, которая должна вернуть кортеж (text, reply_markup).

    Returns:
        Функция-обёртка, которая выполняет безопасное редактирование или отправку сообщения.
    """

    async def wrapper(callback_query: CallbackQuery, *args: Any, **kwargs: Any) -> None:
        result: Tuple[str, InlineKeyboardMarkup] = await func(
            callback_query, *args, **kwargs
        )
        # Если результат отсутствует или не является кортежем (str, InlineKeyboardMarkup)
        if not result or not isinstance(result, (list, tuple)) or len(result) != 2:
            await callback_query.answer(
                "Ошибка: обработчик не вернул необходимые данные."
            )
            return

        text, reply_markup = result
        try:
            await callback_query.message.edit_text(
                text, reply_markup=reply_markup, parse_mode="HTML"
            )
        except TelegramBadRequest as e:
            if "message can't be edited" in e.message:
                await callback_query.bot.send_message(
                    callback_query.from_user.id,
                    text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            else:
                raise
        await callback_query.answer()

    return cast(F, wrapper)


G = TypeVar("G", bound=Callable[..., Awaitable[Any]])


def admin_required(func: G) -> G:
    """
    Декоратор для проверки прав администратора.

    Если пользователь, отправивший сообщение или callback, не входит в список ADMIN_IDS,
    ему отправляется сообщение об отсутствии прав администратора.

    Args:
        func: Функция-обработчик, к которой применяется декоратор.

    Returns:
        Функция-обёртка, выполняющая проверку прав перед вызовом исходной функции.
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        user_id: Any = None

        # Поиск объекта Message или CallbackQuery в позиционных аргументах
        for arg in args:
            if isinstance(arg, Message):
                user_id = arg.from_user.id
                break
            elif isinstance(arg, CallbackQuery):
                user_id = arg.from_user.id
                break

        # Если не найдено, пробуем получить из именованных аргументов
        if user_id is None:
            for value in kwargs.values():
                if isinstance(value, Message) or isinstance(value, CallbackQuery):
                    user_id = value.from_user.id
                    break

        # Если не удалось определить пользователя, продолжаем вызов функции
        if user_id is None:
            return await func(*args, **kwargs)

        # Проверка, что пользователь является администратором
        if user_id not in ADMIN_IDS:
            # Обработка для Message
            for arg in args:
                if isinstance(arg, Message):
                    return await arg.answer("У вас нет прав администратора.")
            # Обработка для CallbackQuery
            for arg in args:
                if isinstance(arg, CallbackQuery):
                    return await arg.answer(
                        "У вас нет прав администратора.", show_alert=True
                    )
            # Если объект не найден в args, проверяем kwargs
            for value in kwargs.values():
                if isinstance(value, Message):
                    return await value.answer("У вас нет прав администратора.")
                elif isinstance(value, CallbackQuery):
                    return await value.answer(
                        "У вас нет прав администратора.", show_alert=True
                    )
        return await func(*args, **kwargs)

    return cast(G, wrapper)
