# error_system.py

import asyncio
import functools
import logging
import contextvars
import traceback
from typing import Any, Callable, Coroutine, Optional, TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


# ============================================================================
# Кастомные исключения
# ============================================================================
class BotError(Exception):
    """Базовый класс ошибок бота."""

    pass


class DatabaseError(BotError):
    """Ошибка работы с базой данных."""

    pass


class APIError(BotError):
    """Ошибка взаимодействия с внешними API."""

    pass


# ============================================================================
# Контекст для структурированного логирования
# ============================================================================
request_context = contextvars.ContextVar("request_context", default={})


# ============================================================================
# Декоратор для централизованной обработки ошибок
# ============================================================================
def error_handler(
    default_return: Optional[T] = None,
    re_raise: bool = False,
    retries: int = 0,
) -> Callable[
    [Callable[..., Coroutine[Any, Any, T]]], Callable[..., Coroutine[Any, Any, T]]
]:
    """
    Декоратор для централизованной обработки ошибок в асинхронных функциях.

    Параметры:
      - default_return: значение, возвращаемое в случае возникновения ошибки.
      - re_raise: если True, исключение пробрасывается дальше после логирования.
      - retries: число повторных попыток (используется tenacity) при возникновении ошибки.

    Пример использования:
        @error_handler(default_return=None, re_raise=False, retries=3)
        async def my_async_function(...):
            ...
    """

    def decorator(
        func: Callable[..., Coroutine[Any, Any, T]],
    ) -> Callable[..., Coroutine[Any, Any, T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            current_context = request_context.get()
            try:
                if retries > 0:
                    # Импорт tenacity только при необходимости
                    from tenacity import (
                        retry,
                        stop_after_attempt,
                        wait_exponential,
                        retry_if_exception_type,
                    )

                    @retry(
                        stop=stop_after_attempt(retries),
                        wait=wait_exponential(multiplier=1, min=1, max=10),
                        retry=retry_if_exception_type(Exception),
                        reraise=True,
                    )
                    async def wrapped() -> T:
                        return await func(*args, **kwargs)

                    result = await wrapped()
                else:
                    result = await func(*args, **kwargs)
                return result
            except Exception as exc:
                # Пропускаем отмену задачи
                if isinstance(exc, asyncio.CancelledError):
                    raise
                log_data = {
                    "function": func.__name__,
                    "args": args,
                    "kwargs": kwargs,
                    "context": current_context,
                    "error": repr(exc),
                    "traceback": traceback.format_exc(),
                }
                logger.exception("Ошибка в функции '%s': %s", func.__name__, log_data)
                if re_raise:
                    raise
                return default_return

        return wrapper

    return decorator


# ============================================================================
# Глобальный обработчик необработанных ошибок event loop
# ============================================================================
def handle_unhandled_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """
    Глобальный обработчик необработанных ошибок в event loop.

    Параметры:
      - loop: event loop.
      - context: контекст ошибки.
    """
    msg = context.get("exception", context.get("message"))
    logger.error("Необработанная ошибка: %s", msg)
