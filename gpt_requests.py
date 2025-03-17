# gpt_requests.py

"""
Модуль gpt_requests для отправки запросов к GPT‑модели через очередь.
Содержит класс TaskRequest для представления запроса, класс RequestDispatcher для управления очередью,
а также функции для формирования запросов к модели YandexGPT с различными вариантами трактовки.
"""

import asyncio
import logging
import datetime
import time
from typing import Any, List, Dict, Optional

from aiogram import Bot, types
from aiogram.exceptions import TelegramAPIError
from aiolimiter import AsyncLimiter
from grpc import StatusCode

from yandex_cloud_ml_sdk import YCloudML
from config import (
    YANDEX_FOLDER_ID,
    YANDEX_AUTH_KEY,
    TOKENS_LIMIT,
)
from errors import error_handler

logger = logging.getLogger(__name__)

# Инициализация Yandex Cloud ML SDK и модели "yandexgpt"
sdk: YCloudML = YCloudML(folder_id=YANDEX_FOLDER_ID, auth=YANDEX_AUTH_KEY)
model = sdk.models.completions("yandexgpt").configure(temperature=0.5)


def approximate_tokens_count(text: str) -> int:
    """
    Простейший подсчёт «токенов» по количеству слов.

    Args:
        text (str): Исходный текст.

    Returns:
        int: Количество слов в тексте.
    """
    return len(text.split())


# ============================================================================
# КЛАСС TaskRequest И RequestDispatcher
# ============================================================================


class TaskRequest:
    """
    Описание одного запроса к GPT, который пойдёт в очередь.

    Args:
        user_id (int): ID пользователя (Telegram).
        chat_id (int): ID чата, куда отправлять сообщения о прогрессе.
        position_msg_id (int): ID сообщения, где отображается позиция в очереди.
        func_to_call (Any): Корутина, которую нужно вызвать (например, get_card_description).
        func_args (Any): Позиционные аргументы для корутины.
        func_kwargs (Any): Именованные аргументы для корутины.
        future (asyncio.Future): Будущий результат, куда будет записан ответ.
    """

    def __init__(
        self,
        user_id: int,
        chat_id: int,
        position_msg_id: int,
        func_to_call: Any,
        func_args: Any,
        func_kwargs: Any,
        future: asyncio.Future,
    ) -> None:
        self.user_id: int = user_id
        self.chat_id: int = chat_id
        self.position_msg_id: int = position_msg_id
        self.func_to_call: Any = func_to_call
        self.func_args: Any = func_args
        self.func_kwargs: Any = func_kwargs
        self.future: asyncio.Future = future


class RequestDispatcher:
    """
    Диспетчер очереди GPT‑запросов.

    Использует asyncio.Queue для хранения задач, Semaphore для ограничения числа одновременных задач
    и AsyncLimiter для контроля rate-limit.

    Args:
        bot (Bot): Объект бота.
        queue (asyncio.Queue): Очередь задач.
        semaphore (asyncio.Semaphore): Ограничение одновременных задач.
        limiter_sec (AsyncLimiter): Ограничение количества запросов в секунду.
        limiter_hour (AsyncLimiter): Ограничение количества запросов в час.
        last_position_update (float): Время последнего обновления позиций.
        update_interval (int): Интервал обновления позиций в очереди (в секундах).
        active_requests (set): Множество активных запросов.
        status_updater_task (asyncio.Task): Фоновая задача для периодического обновления статуса.
        worker_task (asyncio.Task): Фоновая задача для обработки очереди.
    """

    def __init__(
        self, bot: Bot, max_tasks: int, req_per_sec: int, req_per_hour: int
    ) -> None:
        self.bot: Bot = bot
        self.queue: asyncio.Queue = asyncio.Queue()
        self.semaphore: asyncio.Semaphore = asyncio.Semaphore(max_tasks)
        self.limiter_sec: AsyncLimiter = AsyncLimiter(req_per_sec, time_period=1)
        self.limiter_hour: AsyncLimiter = AsyncLimiter(req_per_hour, time_period=3600)
        self.last_position_update: float = 0.0
        self.update_interval: int = 5
        self.active_requests: set = set()
        self.status_updater_task: asyncio.Task = asyncio.create_task(
            self.periodic_status_update()
        )
        self.worker_task: asyncio.Task = asyncio.create_task(self.worker())

    async def worker(self) -> None:
        """
        Обрабатывает задачи из очереди.
        """
        while True:
            task_request: TaskRequest = await self.queue.get()
            self.active_requests.add(task_request)
            async with self.limiter_sec, self.limiter_hour, self.semaphore:
                asyncio.create_task(self.process_task(task_request))
            self.queue.task_done()

    async def process_task(self, task_request: TaskRequest) -> None:
        """
        Обрабатывает отдельный запрос к GPT.

        Args:
            task_request (TaskRequest): Запрос из очереди.
        """
        try:
            await self._notify_in_progress(task_request)
            result_text: str = await task_request.func_to_call(
                *task_request.func_args, **task_request.func_kwargs
            )
            task_request.future.set_result(result_text)
            await self._notify_finished(task_request)
        except Exception as e:
            logger.exception("Ошибка при обработке GPT-запроса: %s", e)
        finally:
            self.active_requests.discard(task_request)

    async def periodic_status_update(self) -> None:
        """
        Периодически обновляет статус для задач в очереди.
        """
        while True:
            try:
                await self.update_positions()
            except Exception as e:
                logger.exception("Ошибка периодического обновления статуса: %s", e)
            await asyncio.sleep(self.update_interval)

    async def add_request(
        self,
        user_id: int,
        chat_id: int,
        func_to_call: Any,
        *func_args: Any,
        **func_kwargs: Any,
    ) -> asyncio.Future:
        """
        Добавляет новый запрос в очередь и возвращает future для получения результата.

        Args:
            user_id (int): ID пользователя.
            chat_id (int): ID чата.
            func_to_call (Any): Функция для вызова.
            *func_args (Any): Позиционные аргументы для функции.
            **func_kwargs (Any): Именованные аргументы для функции.

        Returns:
            asyncio.Future: Будущий результат запроса.
        """
        try:
            queue_msg = await self.bot.send_message(
                chat_id, "⌛ Добавляю ваш запрос в очередь..."
            )
        except Exception as e:
            logger.exception("Ошибка отправки сообщения о постановке в очередь: %s", e)
            # Если отправка не удалась – создаём фиктивное сообщение с message_id=0
            queue_msg = types.Message(
                message_id=0,
                chat=types.Chat(id=chat_id, type="private"),
                date=datetime.datetime.now(),
                text="⌛",
            )
        position_msg_id: int = queue_msg.message_id
        future = asyncio.get_running_loop().create_future()
        task_request = TaskRequest(
            user_id=user_id,
            chat_id=chat_id,
            position_msg_id=position_msg_id,
            func_to_call=func_to_call,
            func_args=func_args,
            func_kwargs=func_kwargs,
            future=future,
        )
        await self.queue.put(task_request)
        current_time: float = time.time()
        if current_time - self.last_position_update >= self.update_interval:
            await self.update_positions()
            self.last_position_update = current_time
        return future

    async def update_positions(self) -> None:
        """
        Обновляет сообщения с информацией о позиции запросов в очереди.
        """
        snapshot: List[Any] = list(self.queue._queue)
        total: int = len(snapshot)
        for idx, req in enumerate(snapshot):
            pos: int = idx + 1
            try:
                await self._edit_position_message(req, pos, total)
            except Exception as e:
                logger.exception("Ошибка редактирования сообщения о позиции: %s", e)

    async def _edit_position_message(
        self, req: TaskRequest, pos: int, total: int
    ) -> None:
        """
        Редактирует сообщение о позиции запроса в очереди.

        Args:
            req (TaskRequest): Запрос из очереди.
            pos (int): Текущая позиция.
            total (int): Общее число запросов в очереди.
        """
        text: str = (
            f"Вы в очереди на генерацию.\n"
            f"Текущая позиция: {pos} из {total}.\n\n"
            "Пожалуйста, дождитесь ответа. Мы стараемся ответить как можно быстрее!"
        )
        try:
            await self.bot.edit_message_text(
                chat_id=req.chat_id, message_id=req.position_msg_id, text=text
            )
        except TelegramAPIError as e:
            logger.warning("Ошибка редактирования сообщения (TelegramAPIError): %s", e)

    async def _notify_in_progress(self, req: TaskRequest) -> None:
        """
        Уведомляет пользователя о начале обработки запроса.

        Args:
            req (TaskRequest): Запрос, который обрабатывается.
        """
        text: str = (
            "✨ Ваш запрос обрабатывается прямо сейчас!\n"
            "Немного терпения, скоро получите ответ."
        )
        try:
            await self.bot.edit_message_text(
                chat_id=req.chat_id, message_id=req.position_msg_id, text=text
            )
        except TelegramAPIError as e:
            logger.warning(
                "Ошибка уведомления о начале обработки (TelegramAPIError): %s", e
            )

    async def _notify_finished(self, req: TaskRequest) -> None:
        """
        Удаляет сообщение о позиции запроса после завершения обработки.

        Args:
            req (TaskRequest): Обработанный запрос.
        """
        try:
            await self.bot.delete_message(
                chat_id=req.chat_id, message_id=req.position_msg_id
            )
        except TelegramAPIError as e:
            logger.warning(
                "Ошибка удаления сообщения о позиции (TelegramAPIError): %s", e
            )


# ============================================================================
# ФУНКЦИИ ЗАПРОСОВ К GPT-МОДЕЛИ
# ============================================================================


@error_handler(default_return="Ошибка при обращении к модели.", retries=3)
async def run_gpt_request(messages: List[Dict[str, str]]) -> str:
    """
    Выполняет запрос к модели YandexGPT.
    Если суммарное число «токенов» (слов) превышает лимит, возвращает сообщение об ошибке.

    Args:
        messages (List[Dict[str, str]]): Список сообщений для запроса к модели.

    Returns:
        str: Результат работы модели или сообщение об ошибке.
    """
    total_tokens: int = sum(approximate_tokens_count(msg["text"]) for msg in messages)
    if total_tokens > TOKENS_LIMIT:
        return (
            f"Превышен лимит длины запроса ({total_tokens} слов) — "
            f"попробуйте сократить текст."
        )
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, model.run, messages)
        if result and len(result) > 0:
            return result[0].text.strip()
        else:
            return "Ответ от модели не получен. Попробуйте ещё раз."
    except Exception as e:
        from yandex_cloud_ml_sdk._exceptions import AioRpcError

        if isinstance(e, AioRpcError) and e.code() == StatusCode.DEADLINE_EXCEEDED:
            logger.exception("Время ожидания ответа от модели истек: %s", e)
            return "Время ожидания ответа от модели истек. Попробуйте повторить запрос позже."
        logger.exception("Ошибка при запросе к YandexGPT: %s", e)
        return f"Ошибка при обращении к модели: {e}"


@error_handler(default_return="")
async def get_card_description(card_name: str) -> str:
    """
    Генерирует описание карты Таро.

    Args:
        card_name (str): Название карты Таро.

    Returns:
        str: Описание карты.
    """
    messages: List[Dict[str, str]] = [
        {
            "role": "system",
            "text": (
                "Ты — мудрый эксперт по Таро, хорошо разбирающийся в юнгианской психологии и символизме. "
                "Отвечай мягко и с поддержкой, ориентируясь в основном на женскую аудиторию 30+, но не только."
            ),
        },
        {
            "role": "user",
            "text": (
                f"Опиши карту «{card_name}» в формате:\n"
                "1) Основная идея\n"
                "2) Юнгианский анализ (архетип, тень и т.д.)\n"
                "3) Психологическая поддержка\n"
                "4) Практический совет\n"
            ),
        },
    ]
    return await run_gpt_request(messages)


@error_handler(default_return="")
async def get_shaman_oracle_description(card_name: str) -> str:
    """
    Генерирует трактовку для колоды «Оракул шамана мистика».

    Args:
        card_name (str): Название карты.

    Returns:
        str: Трактовка карты.
    """
    messages: List[Dict[str, str]] = [
        {
            "role": "system",
            "text": (
                "Ты — древний шаманский оракул, обладающий мистической мудростью и глубоким пониманием духовных миров. "
                "Твой стиль поэтичен, насыщен образными метафорами и символами, отражающими связь с природой и духами."
            ),
        },
        {
            "role": "user",
            "text": (
                f"Опиши карту «{card_name}» с учетом следующих пунктов:\n"
                "1) Основной мистический образ и символика;\n"
                "2) Связь с природными элементами и духами;\n"
                "3) Духовное послание;\n"
                "4) Практический совет или ритуал для гармонии.\n"
            ),
        },
    ]
    return await run_gpt_request(messages)


@error_handler(default_return="")
async def get_goddess_union_description(card_name: str) -> str:
    """
    Генерирует трактовку для колоды «Союз богинь».

    Args:
        card_name (str): Название карты.

    Returns:
        str: Трактовка карты.
    """
    messages: List[Dict[str, str]] = [
        {
            "role": "system",
            "text": (
                "Ты — мудрый эксперт мифологии и хранитель древней женской мудрости, способный пробудить образы богинь и героинь. "
                "Говори тепло, поэтично, используя мифологические аллюзии."
            ),
        },
        {
            "role": "user",
            "text": (
                f"Опиши карту «{card_name}» с учетом следующих пунктов:\n"
                "1) Основной архетипический образ;\n"
                "2) Мифологический контекст;\n"
                "3) Духовное послание и эмоциональная поддержка;\n"
                "4) Практический совет, вдохновленный мифологией.\n"
            ),
        },
    ]
    return await run_gpt_request(messages)


@error_handler(default_return="")
async def ask_yandex_gpt(user_question: str) -> str:
    """
    Генерирует ответ в стиле транзактного анализа (Родитель, Взрослый, Ребёнок).

    Args:
        user_question (str): Вопрос пользователя.

    Returns:
        str: Ответ, сгенерированный моделью.
    """
    messages: List[Dict[str, str]] = [
        {
            "role": "system",
            "text": (
                "Ты — заботливый психолог и коуч, опирающийся на принципы транзактного анализа, разработанного Эриком Берном. "
                "Отвечай мягко, с эмпатией и уважением, учитывая учение Эрика Берна."
            ),
        },
        {
            "role": "user",
            "text": (
                f"{user_question}\n\n"
                "Пожалуйста, ответь, используя следующий структурированный формат транзактного психоанализа, "
                "основанного на учении Эрика Берна:\n"
                "- Эго-состояния (Родитель, Взрослый, Ребёнок)\n"
                "- Транзакции (комплементарные и перекрещённые)\n"
                "- Жизненные сценарии\n"
                "- Игры\n"
                "- Стимулы и признание\n"
                "И добавь заключение, подчеркивающее значимость понимания этих аспектов для личностного роста."
            ),
        },
    ]
    return await run_gpt_request(messages)


@error_handler(default_return="")
async def ask_cbt(user_question: str) -> str:
    """
    Генерирует ответ с использованием метода когнитивно-поведенческой терапии (КПТ).

    Args:
        user_question (str): Вопрос пользователя.

    Returns:
        str: Ответ, сгенерированный моделью.
    """
    messages: List[Dict[str, str]] = [
        {
            "role": "system",
            "text": (
                "Ты — квалифицированный психотерапевт, специализирующийся на КПТ. "
                "Отвечай структурированно и давай конкретные рекомендации."
            ),
        },
        {
            "role": "user",
            "text": (
                "Пожалуйста, представь структурированный анализ КПТ по следующим ключевым пунктам:\n\n"
                "1. Определение проблемы и постановка целей\n"
                "   - Анализ симптомов\n"
                "   - Постановка конкретных целей\n\n"
                "2. Идентификация автоматических мыслей\n"
                "   - Отслеживание негативных мыслей\n"
                "   - Выявление повторяющихся паттернов\n\n"
                "3. Анализ когнитивных искажений\n"
                "   - Определение ошибок мышления (черно-белое мышление, генерализация, катастрофизация)\n\n"
                "4. Разработка альтернативных мыслей\n"
                "   - Переосмысление негативных мыслей\n"
                "   - Применение техник рефрейминга\n\n"
                "5. Изменение поведенческих реакций\n"
                "   - Анализ поведения, влияющего на эмоциональное состояние\n"
                "   - Коррекция поведения\n\n"
                "6. Практические задания и самонаблюдение\n"
                "   - Домашние задания для закрепления навыков\n"
                "   - Ведение дневника мыслей и эмоций\n\n"
                "7. Оценка результатов и корректировка плана\n"
                "   - Регулярный мониторинг прогресса\n"
                "   - Адаптация терапевтического плана\n\n"
                f"Вопрос: {user_question}"
            ),
        },
    ]
    return await run_gpt_request(messages)


@error_handler(default_return="")
async def get_spread_interpretation(cards: List[str]) -> str:
    """
    Генерирует трактовку расклада по списку карт.

    Args:
        cards (List[str]): Список названий карт.

    Returns:
        str: Подробная трактовка расклада.
    """
    cards_text: str = ", ".join(cards)
    messages: List[Dict[str, str]] = [
        {
            "role": "system",
            "text": (
                "Ты опытный таролог, который интерпретирует расклад исключительно исходя из комбинации карт. "
                "Не запрашивай дополнительный контекст и не упоминай о достоверности или точности результатов. "
                "Просто предоставь подробную трактовку расклада, описывая символику каждой карты и их взаимодействие."
            ),
        },
        {
            "role": "user",
            "text": (
                f"Сделай подробную трактовку расклада, состоящего из следующих карт: {cards_text}.\n"
                "Опиши значение каждой карты, их взаимосвязь и общий смысл расклада."
            ),
        },
    ]
    return await run_gpt_request(messages)


@error_handler(default_return="")
async def ask_gpt_in_queue(
    dispatcher: RequestDispatcher,
    chat_id: int,
    user_id: int,
    func_to_call: Any,
    *args: Any,
    **kwargs: Any,
) -> str:
    """
    Отправляет задачу на выполнение через очередь и ждёт результата.

    Args:
        dispatcher (RequestDispatcher): Диспетчер очереди GPT-запросов.
        chat_id (int): ID чата.
        user_id (int): ID пользователя.
        func_to_call (Any): Функция, которая будет вызвана.
        *args (Any): Позиционные аргументы для функции.
        **kwargs (Any): Именованные аргументы для функции.

    Returns:
        str: Результат выполнения запроса.
    """
    future = await dispatcher.add_request(
        user_id=user_id,
        chat_id=chat_id,
        func_to_call=func_to_call,
        *args,
        **kwargs,
    )
    result: str = await future
    return result
