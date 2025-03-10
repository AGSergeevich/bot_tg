import asyncio
import sys
import os
import logging
import aiohttp
import random
import json
import time
import re
from dotenv import load_dotenv
from typing import Union

# Исправление для Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from functools import wraps

# Загрузка окружения
load_dotenv()

# Конфигурация
class Config:
    MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    CHANNEL_ID = os.getenv("CHANNEL_ID")
    ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(',')))
    MODEL = "mistral-small"
    MAX_TOKENS = 1500
    TEMPERATURE = 0.8
    REQUEST_TIMEOUT = 25
    CALLBACK_TIMEOUT = 30

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Стейты
class PostStates(StatesGroup):
    waiting_for_edit = State()

# Инициализация aiogram
bot = Bot(token=Config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Список подтем косметики
COSMETIC_SUBTOPICS = [
    "новинки косметики 2024",
    "уход за кожей зимой",
    "антивозрастной уход за кожей",
    "макияж для разных типов лица",
    "натуральные ингредиенты в косметике"
]

# Функции для работы с темами
def load_used_topics():
    try:
        with open('used_topics.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_used_topics(topics):
    with open('used_topics.json', 'w') as f:
        json.dump(topics, f)

def get_unique_subtopic():
    used = load_used_topics()
    available = [t for t in COSMETIC_SUBTOPICS if t not in used]
    
    if not available:
        available = COSMETIC_SUBTOPICS
        used = []
    
    subtopic = random.choice(available)
    used.append(subtopic)
    save_used_topics(used)
    return subtopic

# Клавиатуры
def post_actions_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data="publish"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data="edit"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")
        ]
    ])

# Декоратор для проверки админа
def admin_required(func):
    @wraps(func)
    async def wrapper(handler: Union[Message, CallbackQuery], *args, **kwargs):
        try:
            if isinstance(handler, CallbackQuery):
                message_time = handler.message.date.timestamp()
                if (time.time() - message_time) > Config.CALLBACK_TIMEOUT:
                    await handler.answer("⌛ Время ответа истекло!", show_alert=True)
                    return

            user_id = handler.from_user.id
            if user_id not in Config.ADMIN_IDS:
                msg = f"⛔ Доступ запрещен! Ваш ID: {user_id}"
                
                if isinstance(handler, Message):
                    await handler.answer(msg)
                elif isinstance(handler, CallbackQuery):
                    await handler.answer(msg, show_alert=True)
                
                logger.warning(f"Несанкционированный доступ: {user_id}")
                return
            return await func(handler, *args, **kwargs)
        except Exception as e:
            logger.error(f"Ошибка проверки прав: {str(e)}")
    return wrapper

# Очистка текста для MarkdownV2
def escape_markdown(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# Mistral API клиент
class MistralClient:
    def __init__(self):
        self.base_url = "https://api.mistral.ai/v1/chat/completions"
        self.session = aiohttp.ClientSession()

    async def generate_post(self, prompt: str) -> str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {Config.MISTRAL_API_KEY}"
        }
        
        payload = {
            "model": Config.MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": Config.TEMPERATURE,
            "max_tokens": Config.MAX_TOKENS
        }

        try:
            async with self.session.post(
                self.base_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT)
            ) as response:
                response.raise_for_status()
                data = await response.json()
                return escape_markdown(data['choices'][0]['message']['content'])
        except Exception as e:
            logger.error(f"Ошибка Mistral API: {str(e)}")
            raise
        finally:
            await self.session.close()

# Проверка прав бота в канале
async def check_bot_permissions():
    try:
        chat_member = await bot.get_chat_member(
            chat_id=Config.CHANNEL_ID,
            user_id=(await bot.get_me()).id
        )
        logger.info(f"Права бота: {chat_member}")
        return chat_member.can_post_messages
    except Exception as e:
        logger.error(f"Ошибка проверки прав: {str(e)}")
        return False

# Хендлеры
@dp.message(Command("start"))
@admin_required
async def cmd_start(message: Message):
    text = (
        "🌟 <b>Бот-генератор постов о косметике</b>\n\n"
        "Доступные команды:\n"
        "/post - Создать новый пост\n"
        "/reset_topics - Сбросить историю тем\n"
        "/id - Показать ваш ID\n"
        "/test - Тест публикации"
    )
    await message.answer(text)

@dp.message(Command("id"))
async def get_id(message: Message):
    await message.answer(f"🆔 Ваш ID: {message.from_user.id}")

@dp.message(Command("post"))
@admin_required
async def cmd_post(message: Message, state: FSMContext):
    try:
        mistral = MistralClient()
        msg = await message.answer("⚙️ Генерация поста...")
        
        subtopic = get_unique_subtopic()
        
        prompt = (
            f"Создай пост для Telegram о косметике. Тема: {subtopic}\n"
            "Требования:\n"
            "- Используй MarkdownV2 разметку\n"
            "- Хештеги в конце поста\n"
            "- Максимум 3 эмодзи\n"
            "- Не более 2000 символов"
        )
        
        post = await mistral.generate_post(prompt)
        await bot.delete_message(message.chat.id, msg.message_id)
        
        await state.update_data(generated_post=post)
        await message.answer(
            f"✅ <b>Новый пост готов!</b>\n\n"
            f"🏷 <i>Тема:</i> {subtopic}\n"
            "📝 <i>Предпросмотр:</i>\n"
            "──────────────────\n"
            f"{post}\n"
            "──────────────────\n"
            "Выберите действие:",
            reply_markup=post_actions_keyboard(),
            parse_mode=ParseMode.HTML
        )
        
    except asyncio.TimeoutError:
        await message.answer("⏳ Превышено время ожидания ответа от API")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

@dp.message(Command("reset_topics"))
@admin_required
async def cmd_reset_topics(message: Message):
    save_used_topics([])
    await message.answer("🔄 История тем сброшена!")

@dp.callback_query(F.data.in_(["publish", "edit", "cancel"]))
@admin_required
async def handle_buttons(callback: CallbackQuery, state: FSMContext):
    try:
        logger.info(f"Начало обработки callback: {callback.data}")
        data = await state.get_data()
        post = data.get("generated_post")
        
        if not post:
            logger.error("Пост не найден в состоянии!")
            await callback.answer("❌ Ошибка: пост не сгенерирован!", show_alert=True)
            return

        await callback.answer()
        
        if callback.data == "publish":
            if not await check_bot_permissions():
                await callback.message.edit_text("❌ Бот не имеет прав на публикацию!")
                return

            try:
                await bot.send_message(
                    chat_id=Config.CHANNEL_ID,
                    text=post,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                await callback.message.edit_text("✅ Пост успешно опубликован!")
                logger.info(f"Пост опубликован: {post[:100]}...")
                
            except TelegramForbiddenError:
                error_msg = "❌ Нет прав для публикации в канале!"
                await callback.message.edit_text(error_msg)
                logger.error("Доступ к каналу запрещен")
                
            except Exception as e:
                await callback.message.edit_text(f"❌ Ошибка публикации: {str(e)}")
                logger.error(f"Ошибка публикации: {str(e)}")

        elif callback.data == "edit":
            await callback.message.edit_text("✏️ Введите исправленный текст поста:")
            await state.set_state(PostStates.waiting_for_edit)

        elif callback.data == "cancel":
            await callback.message.edit_text("🗑 Публикация отменена")
            await state.clear()

    except TelegramBadRequest as e:
        if "query is too old" in str(e):
            logger.warning("Просроченный callback-запрос")
            await callback.answer("⌛ Время действия истекло!", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка обработки callback: {str(e)}")
        await callback.answer("⚠️ Произошла ошибка!", show_alert=True)

@dp.message(PostStates.waiting_for_edit)
@admin_required
async def handle_edit(message: Message, state: FSMContext):
    try:
        sanitized_text = escape_markdown(message.text)
        await bot.send_message(
            chat_id=Config.CHANNEL_ID,
            text=sanitized_text,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        await message.answer("📢 Исправленный пост опубликован!")
    except TelegramForbiddenError:
        await message.answer("❌ Нет доступа к каналу!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
    finally:
        await state.clear()

# Тестовая публикация
@dp.message(Command("test"))
@admin_required
async def cmd_test(message: Message):
    test_post = "Тестовый пост ✨\n#тест #бота"
    try:
        await bot.send_message(
            chat_id=Config.CHANNEL_ID,
            text=escape_markdown(test_post),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        await message.answer("✅ Тестовый пост опубликован!")
    except Exception as e:
        await message.answer(f"❌ Ошибка теста: {str(e)}")

# Запуск
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())