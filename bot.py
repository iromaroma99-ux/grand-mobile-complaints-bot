import os
import threading
import time
import json
import requests
import telebot
from datetime import datetime

# ====================== НАСТРОЙКА (через переменные окружения) ======================
# Перед запуском задай эти переменные (например, в .env или export в терминале):
#   BOT_TOKEN       — токен бота от @BotFather
#   GRND_SID        — твоя кука grnd_sid с сайта (см. README.md, как её достать)
#   ADMIN_ID        — твой личный Telegram user ID (узнать через @userinfobot)
#   GROUP_CHAT_ID   — ID группы, куда слать уведомления (через @RawDataBot)
#   SERVER_ID       — номер игрового сервера, например "33"
#   REGION          — регион, например "ru"
#   ADMIN_TAG       — короткий тег, добавляемый в конец игровых Off-команд
# =======================================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_БОТА")
bot = telebot.TeleBot(BOT_TOKEN)

# Username бота — заполняется при старте (setup_bot_commands), нужен для ссылки t.me/<bot>
BOT_USERNAME = None

REGION = os.environ.get("REGION", "ru")
SERVER_ID = os.environ.get("SERVER_ID", "33")

COOKIES = {
    'i18n_redirected': REGION,
    'grnd_sid': os.environ.get("GRND_SID", "ВСТАВЬ_СЮДА_СВЕЖИЙ_grnd_sid"),
    f'filters:/admin/complaints:region': f'%5B%22{REGION}%22%5D',
    f'filters:/admin/complaints:server': f'%7B%22{REGION}%22%3A%5B{SERVER_ID}%5D%7D',
}

HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'user-agent': 'Mozilla/5.0 (Linux; Android 12; Xiaomi 12T) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36',
    'Referer': 'https://grnd.gg/admin/complaints',
}

# Правильный API-эндпоинт (не страница, а JSON API)
URL = "https://api-site.grnd.gg/admin/complaints"

# Эндпоинт деталей одной жалобы (картинки, код правила, полный текст)
DETAIL_URL_TEMPLATE = "https://api-site.grnd.gg/admin/complaints/" + REGION + "/{id}"

# Список админов сервера (для бейджа "Админ, уровень X" в сообщениях о жалобах)
ADMINS_URL = "https://api-site.grnd.gg/admin/admins"


# Интервал проверки в секундах (3 минуты = 180)
CHECK_INTERVAL = 180

# Раз в сколько секунд автоматически постить список висящих жалоб в группу (3600 = 1 час)
PENDING_POST_INTERVAL = 3600

# Твой личный Telegram ID — только с него принимается /start и закрытие жалоб (узнать через @userinfobot)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

# ID группы, куда всегда будут приходить уведомления о жалобах (узнать через @RawDataBot)
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0"))

# Флаг: запущена ли рассылка (включается после /start от админа)
IS_RUNNING = False

# Множество ID уже отправленных жалоб (чтобы не дублировать и не терять старые)
SEEN_IDS = set()

# ID жалоб, которые бот уже показал в группе и которые (насколько известно) ещё не закрыты.
# Если жалоба пропадает из "новых" на сайте, но остаётся тут — значит, её закрыл кто-то
# помимо бота, и при следующей проверке сообщение в группе обновится автоматически.
OPEN_COMPLAINT_IDS = set()

# Время последней успешной/неуспешной проверки и её результат (для команды /status)
LAST_CHECK_TIME = None
LAST_CHECK_STATUS = "Проверок ещё не было"

# {telegram_user_id: complaint_id} — если тут есть запись, значит бот ждёт от этого
# человека текст вердикта следующим сообщением, чтобы закрыть эту жалобу
PENDING_CLOSE = {}

# Русские названия типов наказаний (как в настройках расширения Фловерса)
PUNISHMENT_LABELS = {
    'warn': 'Предупреждение',
    'mute': 'Блокировка чата',
    'jail': 'Jail',
    'ban': 'Блокировка аккаунта',
}

# Готовые варианты сроков для быстрого выбора (минуты для mute/jail, дни для ban)
PUNISHMENT_DURATIONS = {
    'mute': [15, 30, 60, 90, 120, 180],
    'jail': [30, 60],
    'ban': [3, 5, 7, 10, 20, 31],
}

# Игровые/дискорд-команды для каждого типа наказания
OFF_COMMANDS = {
    'warn': '/Offwarn',
    'mute': '/offmute',
    'jail': '/Offjail',
    'ban': '/Offban',
}

# Тег, добавляемый в конец каждой Off-команды
ADMIN_TAG = os.environ.get("ADMIN_TAG", "Admin")

# {complaint_id (str): {'chat_id':.., 'content_message_id':.., 'action_message_id':..}}
# Заполняется при каждой отправке жалобы в группу, используется командой /pending для ссылок
COMPLAINT_LINKS = {}

# Кэш списка админов: {ник: adminLevel}, обновляется не чаще раза в 30 минут
ADMIN_DIRECTORY = {}
ADMIN_DIRECTORY_FETCHED_AT = 0
ADMIN_DIRECTORY_TTL = 1800  # секунд


def build_params():
    return {
        'status': '0',
        'server': '{"' + REGION + '":[' + SERVER_ID + ']}',
        '_': str(int(time.time() * 1000)),  # анти-кэш метка, должна обновляться каждый запрос
    }


def fetch_complaints(retry_count=10, delay=1):
    for attempt in range(retry_count + 1):
        try:
            response = requests.get(
                URL,
                params=build_params(),
                cookies=COOKIES,
                headers=HEADERS,
                timeout=10
            )

            print(f"DEBUG - Попытка {attempt}, статус: {response.status_code}")

            if response.status_code == 200:
                return response.json()

            if response.status_code in (424, 500, 502, 503, 504):
                time.sleep(delay * attempt)
                continue

            print(f"DEBUG - Ошибка (Response: {response.text[:300]})")
            return None

        except Exception as e:
            print(f"DEBUG - Ошибка (Попытка {attempt}: {e})")
            time.sleep(delay * attempt)

    print("DEBUG - Все попытки исчерпаны")
    return None


def fetch_admins():
    """Запрашивает список админов сервера. Возвращает {ник: adminLevel} или None при ошибке."""
    try:
        response = requests.get(
            ADMINS_URL,
            params={'server': '{"' + REGION + '":[' + SERVER_ID + ']}'},
            cookies=COOKIES,
            headers=HEADERS,
            timeout=10
        )
        if response.status_code != 200:
            print(f"DEBUG - Список админов: статус {response.status_code}")
            return None

        data = response.json()
        admins_list = data.get('data', [])
        return {a.get('name'): a.get('adminLevel') for a in admins_list if a.get('name')}

    except Exception as e:
        print(f"DEBUG - Ошибка при получении списка админов: {e}")
        return None


def get_admin_directory():
    """Директория админов с кэшем на ADMIN_DIRECTORY_TTL секунд, чтобы не дёргать API на каждую жалобу."""
    global ADMIN_DIRECTORY, ADMIN_DIRECTORY_FETCHED_AT

    now = time.time()
    if not ADMIN_DIRECTORY or (now - ADMIN_DIRECTORY_FETCHED_AT) > ADMIN_DIRECTORY_TTL:
        fresh = fetch_admins()
        if fresh is not None:
            ADMIN_DIRECTORY = fresh
            ADMIN_DIRECTORY_FETCHED_AT = now

    return ADMIN_DIRECTORY


def admin_badge(name):
    """Возвращает ' 🛡 Админ' если ник есть в списке админов, иначе пустую строку."""
    if name in get_admin_directory():
        return " 🛡 <i>Админ</i>"
    return ""


def fetch_complaint_detail(complaint_id, retry_count=3):
    """Запрашивает детали конкретной жалобы: картинки, код правила (rules), полный текст (text), от кого/на кого.
    При таймауте/обрыве — до 3 попыток, чтобы не терять описание из-за разовых сетевых проблем."""
    empty = {'images': [], 'rules': None, 'text': None, 'from_user_name': None, 'to_user_name': None,
             'createdAt': None, 'admin_name': None, 'answer_date': None, 'answer': None}

    data = None
    for attempt in range(retry_count):
        try:
            response = requests.get(
                DETAIL_URL_TEMPLATE.format(id=complaint_id),
                cookies=COOKIES,
                headers=HEADERS,
                timeout=15
            )
            if response.status_code != 200:
                print(f"DEBUG - Детали жалобы #{complaint_id}: статус {response.status_code}")
                return empty

            data = response.json()
            break

        except Exception as e:
            print(f"DEBUG - Детали жалобы #{complaint_id}, попытка {attempt + 1}/{retry_count} не удалась: {e}")
            if attempt < retry_count - 1:
                time.sleep(2)

    if data is None:
        return empty

    try:
        if isinstance(data, list) and data:
            detail = data[0]
        elif isinstance(data, dict):
            detail = data
        else:
            return empty

        images_raw = detail.get('images')
        images_list = []
        if images_raw:
            if isinstance(images_raw, str):
                try:
                    images_list = json.loads(images_raw)
                except json.JSONDecodeError:
                    images_list = []
            else:
                images_list = images_raw

        return {
            'images': images_list if isinstance(images_list, list) else [],
            'rules': detail.get('rules'),
            'text': detail.get('text'),
            'from_user_name': detail.get('from_user_name'),
            'to_user_name': detail.get('to_user_name'),
            'createdAt': detail.get('createdAt'),
            'admin_name': detail.get('admin_name'),
            'answer_date': detail.get('answer_date'),
            'answer': detail.get('answer'),
        }

    except Exception as e:
        print(f"DEBUG - Ошибка при получении деталей жалобы #{complaint_id}: {e}")
        return empty


def check_already_closed(complaint_id):
    """Проверяет текущий статус жалобы на сайте. Возвращает (закрыта_ли, detail)."""
    detail = fetch_complaint_detail(complaint_id)
    return bool(detail.get('answer_date')), detail


def close_complaint(complaint_id, answer_text, retry_count=3):
    """Закрывает жалобу на сайте: PUT-запрос с текстом вердикта. При таймауте/обрыве — до 3 попыток
    (сам запрос idempotent: повторная отправка того же текста ничего не портит).
    Возвращает (успех, текст_ошибки, admin_name)."""
    last_error = None
    for attempt in range(retry_count):
        try:
            response = requests.put(
                URL,
                json={
                    "region": REGION,
                    "id": int(complaint_id),
                    "answer": answer_text,
                },
                cookies=COOKIES,
                headers={**HEADERS, 'Content-Type': 'application/json'},
                timeout=20
            )
            if response.status_code == 200:
                admin_name = None
                try:
                    admin_name = response.json().get('admin_name')
                except Exception:
                    pass
                return True, None, admin_name
            return False, f"статус {response.status_code}: {response.text[:200]}", None
        except Exception as e:
            last_error = str(e)
            print(f"DEBUG - close_complaint попытка {attempt + 1}/{retry_count} не удалась: {e}")
            if attempt < retry_count - 1:
                time.sleep(2)

    return False, last_error, None


def format_complaint_message(complaint, full_text=None, rules=None):
    text_to_show = full_text or complaint.get('text') or 'Не указан'

    msg = "🆕 <b>НОВАЯ ЖАЛОБА!</b>\n\n"
    msg += f"<b>Номер:</b> <code>#{complaint.get('id')}</code>\n"
    msg += f"<b>Время:</b> {complaint.get('createdAt', 'Не указано')}\n"
    from_name = complaint.get('from_user_name', 'Неизвестно')
    to_name = complaint.get('to_user_name', 'Неизвестно')
    msg += f"<b>От:</b> <code>{from_name}</code>{admin_badge(from_name)}\n"
    msg += f"<b>На:</b> <code>{to_name}</code>{admin_badge(to_name)}\n"
    if rules:
        msg += f"<b>Код правила:</b> {rules}\n"
    msg += "\n"
    msg += f"<b>Описание жалобы:</b>\n{text_to_show[:800]}\n\n"
    msg += f"🔗 <a href='https://grnd.gg/admin/complaints/{REGION}/{complaint.get('id')}'>Открыть жалобу</a>"
    return msg


def build_complaint_keyboard(complaint_id):
    keyboard = telebot.types.InlineKeyboardMarkup()
    keyboard.row(
        telebot.types.InlineKeyboardButton("✍️ Вынести вердикт", callback_data=f"write:{complaint_id}"),
        telebot.types.InlineKeyboardButton("⚖️ Наказание", callback_data=f"punish:{complaint_id}"),
    )
    return keyboard


def build_punishment_type_keyboard(complaint_id):
    keyboard = telebot.types.InlineKeyboardMarkup()
    keyboard.row(
        telebot.types.InlineKeyboardButton("Warn", callback_data=f"ptype:{complaint_id}:warn"),
        telebot.types.InlineKeyboardButton("Mute", callback_data=f"ptype:{complaint_id}:mute"),
    )
    keyboard.row(
        telebot.types.InlineKeyboardButton("Jail", callback_data=f"ptype:{complaint_id}:jail"),
        telebot.types.InlineKeyboardButton("Ban", callback_data=f"ptype:{complaint_id}:ban"),
    )
    keyboard.row(
        telebot.types.InlineKeyboardButton("Warn+Jail", callback_data=f"ptype:{complaint_id}:warn_jail"),
        telebot.types.InlineKeyboardButton("Jail+Mute", callback_data=f"ptype:{complaint_id}:jail_mute"),
    )
    return keyboard


def build_duration_keyboard(complaint_id, ptype, unit_override=None, duration_list=None):
    keyboard = telebot.types.InlineKeyboardMarkup()
    unit = unit_override or ('д' if ptype == 'ban' else 'мин')
    durations = duration_list if duration_list is not None else PUNISHMENT_DURATIONS[ptype]
    buttons = [
        telebot.types.InlineKeyboardButton(f"{d} {unit}", callback_data=f"pdur:{complaint_id}:{ptype}:{d}")
        for d in durations
    ]
    keyboard.add(*buttons)
    return keyboard


def build_second_duration_keyboard(complaint_id, jail_duration):
    """Второй шаг для Jail+Mute — выбор срока мута после уже выбранного срока jail."""
    keyboard = telebot.types.InlineKeyboardMarkup()
    buttons = [
        telebot.types.InlineKeyboardButton(f"{d} мин", callback_data=f"pdur2:{complaint_id}:{jail_duration}:{d}")
        for d in PUNISHMENT_DURATIONS['mute']
    ]
    keyboard.add(*buttons)
    return keyboard


def send_complaint(chat_id, complaint):
    """Отправляет жалобу в чат (текст/фото/альбом), а затем ОТДЕЛЬНОЕ сообщение
    "Действия для жалобы #id:" с кнопками — именно это сообщение бот потом
    редактирует при закрытии, вместо того чтобы слать новые. Возвращает его message_id."""
    complaint_id = complaint.get('id')
    detail = fetch_complaint_detail(complaint_id)
    images = detail['images']
    text = format_complaint_message(complaint, full_text=detail['text'], rules=detail['rules'])
    keyboard = build_complaint_keyboard(complaint_id)

    if images:
        try:
            if len(images) == 1:
                bot.send_photo(chat_id, images[0], caption=text, parse_mode='HTML')
            else:
                media = [telebot.types.InputMediaPhoto(images[0], caption=text, parse_mode='HTML')]
                for img_url in images[1:]:
                    media.append(telebot.types.InputMediaPhoto(img_url))
                bot.send_media_group(chat_id, media)
        except Exception as e:
            print(f"DEBUG - Не удалось отправить картинку(и): {e}")
            bot.send_message(chat_id, text, parse_mode='HTML')
    else:
        bot.send_message(chat_id, text, parse_mode='HTML')

    action_msg = bot.send_message(
        chat_id, f"Действия для жалобы <code>#{complaint_id}</code>:",
        parse_mode='HTML', reply_markup=keyboard
    )

    COMPLAINT_LINKS[str(complaint_id)] = {
        'chat_id': chat_id,
        'action_message_id': action_msg.message_id,
        'from_user_name': complaint.get('from_user_name'),
        'to_user_name': complaint.get('to_user_name'),
    }

    return action_msg.message_id


def group_message_link(chat_id, message_id):
    """Строит ссылку вида https://t.me/c/.../msgid на сообщение в супергруппе."""
    chat_id_str = str(chat_id)
    if chat_id_str.startswith('-100'):
        internal_id = chat_id_str[4:]
        return f"https://t.me/c/{internal_id}/{message_id}"
    return None


def post_pending_list(announce_empty=True):
    """Собирает и шлёт в группу список всех висящих жалоб одним сообщением.
    announce_empty=False — для автопостинга по таймеру, чтобы не спамить "жалоб нет" каждый час."""
    data = fetch_complaints()
    if not data:
        return None  # ошибку уже залогирует run_check/caller при желании

    complaints = data.get('complaints', [])
    if not complaints:
        if announce_empty:
            bot.send_message(GROUP_CHAT_ID, "✅ Висящих жалоб нет — все разобраны.")
        return 0

    complaints.sort(key=lambda c: int(c.get('id', 0)))

    lines = [f"📋 <b>Висящие жалобы ({len(complaints)}):</b>\n"]
    for c in complaints:
        cid = str(c.get('id'))
        from_name = c.get('from_user_name', '?')
        to_name = c.get('to_user_name', '?')
        link_info = COMPLAINT_LINKS.get(cid)
        if link_info:
            link = group_message_link(link_info['chat_id'], link_info['action_message_id'])
        else:
            link = None
        if not link:
            link = f"https://grnd.gg/admin/complaints/ru/{cid}"
        lines.append(f"• <code>#{cid}</code> — <code>{from_name}</code> → <code>{to_name}</code> — <a href='{link}'>перейти</a>")

    bot.send_message(GROUP_CHAT_ID, "\n".join(lines), parse_mode='HTML', disable_web_page_preview=True)
    return len(complaints)


@bot.message_handler(commands=['pending'])
def handle_pending(message):
    if message.from_user.id != ADMIN_ID:
        return

    count = post_pending_list(announce_empty=True)
    if count is None:
        bot.send_message(message.chat.id, "❌ Не удалось получить данные с сайта.")
        return

    if message.chat.id != GROUP_CHAT_ID:
        bot.send_message(message.chat.id, "✅ Список отправлен в группу.")
    else:
        # Команду написали прямо в группе — удаляем её, чтобы не засорять чат
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception as e:
            print(f"DEBUG - Не удалось удалить сообщение с командой /pending: {e}")


@bot.message_handler(commands=['start'])
def handle_start(message):
    global IS_RUNNING, SEEN_IDS, OPEN_COMPLAINT_IDS

    if message.from_user.id != ADMIN_ID:
        return

    IS_RUNNING = True
    bot.send_message(message.chat.id, "✅ Бот запущен, проверяю текущие жалобы...")
    bot.send_message(GROUP_CHAT_ID, "✅ Бот запущен, проверяю текущие жалобы...")

    data = fetch_complaints()
    if not data:
        bot.send_message(message.chat.id, "❌ Не удалось получить данные с сайта (проверь куки/токен).")
        return

    complaints = data.get('complaints', [])
    if not complaints:
        bot.send_message(GROUP_CHAT_ID, "ℹ️ Сейчас открытых жалоб нет.")
        SEEN_IDS = set()
        OPEN_COMPLAINT_IDS = set()
        return

    # Запоминаем ВСЕ текущие жалобы как уже увиденные, чтобы фоновая проверка не слала их повторно
    SEEN_IDS = {str(c.get('id')) for c in complaints}
    OPEN_COMPLAINT_IDS = set(SEEN_IDS)

    bot.send_message(GROUP_CHAT_ID, f"📡 Найдено жалоб: {len(complaints)}")

    # Сортируем по самому ID (он всегда растёт), а не полагаемся на порядок из API
    for complaint in sorted(complaints, key=lambda c: int(c.get('id', 0))):
        send_complaint(GROUP_CHAT_ID, complaint)
        time.sleep(0.5)

    bot.send_message(GROUP_CHAT_ID, f"🔄 Дальше буду проверять каждые {CHECK_INTERVAL // 60} мин и присылать только новые.")


@bot.message_handler(commands=['status'])
def handle_status(message):
    if message.from_user.id != ADMIN_ID:
        return

    running_text = "✅ Запущен" if IS_RUNNING else "⏸ Не запущен (напиши /start)"
    check_time_text = LAST_CHECK_TIME or "ещё не проверял"

    reply = (
        f"<b>Статус бота:</b> {running_text}\n"
        f"<b>Последняя проверка:</b> {check_time_text}\n"
        f"<b>Результат:</b> {LAST_CHECK_STATUS}"
    )
    bot.send_message(message.chat.id, reply, parse_mode='HTML')


@bot.message_handler(commands=['check'])
def handle_check(message):
    if message.from_user.id != ADMIN_ID:
        return

    if not IS_RUNNING:
        bot.send_message(message.chat.id, "⏸ Бот не запущен. Сначала напиши /start.")
        return

    bot.send_message(message.chat.id, "🔄 Проверяю...")
    run_check()
    bot.send_message(message.chat.id, f"✅ Готово. {LAST_CHECK_STATUS}")


@bot.message_handler(commands=['stop'])
def handle_stop(message):
    global IS_RUNNING

    if message.from_user.id != ADMIN_ID:
        return

    IS_RUNNING = False
    bot.send_message(message.chat.id, "⏸ Мониторинг остановлен. Напиши /start, чтобы возобновить.")


@bot.callback_query_handler(func=lambda call: call.data.startswith(('write:', 'punish:')))
def handle_complaint_buttons(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ У тебя нет доступа к этому действию.", show_alert=True)
        return

    action, complaint_id = call.data.split(':', 1)

    # Общая проверка для всех трёх кнопок: вдруг жалобу уже закрыли (на сайте или другой админ)
    already_closed, detail = check_already_closed(complaint_id)
    if already_closed:
        who = detail.get('admin_name') or 'неизвестным админом'
        when = detail.get('answer_date') or ''
        bot.answer_callback_query(call.id, f"Уже закрыта: {who}", show_alert=True)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"Действия для жалобы <code>#{complaint_id}</code>:\n\n⚠️ Жалоба уже закрыта администратором {who} ({when}).\n\n<b>Ответ:</b>\n{detail.get('answer') or 'не указан'}",
            parse_mode='HTML',
            reply_markup=None
        )
        return

    if action == 'write':
        PENDING_CLOSE[call.from_user.id] = {
            'complaint_id': complaint_id,
            'chat_id': call.message.chat.id,
            'action_message_id': call.message.message_id,
        }
        bot.answer_callback_query(call.id)

        detail = fetch_complaint_detail(complaint_id)
        ctx_from = detail.get('from_user_name', 'Неизвестно')
        ctx_to = detail.get('to_user_name', 'Неизвестно')
        context_text = (
            f"<b>Жалоба <code>#{complaint_id}</code></b>\n"
            f"<b>От:</b> <code>{ctx_from}</code>{admin_badge(ctx_from)}\n"
            f"<b>На:</b> <code>{ctx_to}</code>{admin_badge(ctx_to)}\n"
        )
        if detail.get('rules'):
            context_text += f"<b>Код правила:</b> {detail['rules']}\n"
        context_text += f"\n<b>Описание:</b>\n{detail.get('text') or 'Не указан'}"

        images = detail.get('images') or []
        if images:
            try:
                if len(images) == 1:
                    bot.send_photo(call.from_user.id, images[0], caption=context_text, parse_mode='HTML')
                else:
                    media = [telebot.types.InputMediaPhoto(images[0], caption=context_text, parse_mode='HTML')]
                    for img_url in images[1:]:
                        media.append(telebot.types.InputMediaPhoto(img_url))
                    bot.send_media_group(call.from_user.id, media)
            except Exception as e:
                print(f"DEBUG - Не удалось отправить картинку в личку: {e}")
                bot.send_message(call.from_user.id, context_text, parse_mode='HTML')
        else:
            bot.send_message(call.from_user.id, context_text, parse_mode='HTML')

        bot.send_message(
            call.from_user.id,
            f"✍️ Напиши мне текст вердикта для жалобы #{complaint_id} следующим сообщением."
        )

    elif action == 'punish':
        PENDING_CLOSE[f"ctx:{complaint_id}"] = {
            'chat_id': call.message.chat.id,
            'action_message_id': call.message.message_id,
        }
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.from_user.id,
            f"⚖️ Жалоба #{complaint_id} — выбери тип наказания:",
            reply_markup=build_punishment_type_keyboard(complaint_id)
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith('ptype:'))
def handle_ptype(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ У тебя нет доступа.", show_alert=True)
        return

    _, complaint_id, ptype = call.data.split(':')
    bot.answer_callback_query(call.id)

    ctx = PENDING_CLOSE.get(f"ctx:{complaint_id}", {'chat_id': GROUP_CHAT_ID, 'action_message_id': None})

    def start_point_stage(parts):
        PENDING_CLOSE[call.from_user.id] = {
            'mode': 'point',
            'complaint_id': complaint_id,
            'parts': parts,
            'chat_id': ctx['chat_id'],
            'action_message_id': ctx['action_message_id'],
        }
        bot.send_message(
            call.from_user.id,
            f"✍️ Напиши номер пункта правил для жалобы #{complaint_id} (например: 2.1 ОП)."
        )

    if ptype == 'warn':
        # У Warn нет срока — сразу просим номер пункта правил
        start_point_stage([('warn', None)])

    elif ptype == 'warn_jail':
        # Сразу переходим к выбору срока Jail (Warn без срока)
        bot.send_message(
            call.from_user.id,
            "⏱ Выбери срок Jail:",
            reply_markup=build_duration_keyboard(complaint_id, 'warn_jail', duration_list=PUNISHMENT_DURATIONS['jail'])
        )

    elif ptype == 'jail_mute':
        bot.send_message(
            call.from_user.id,
            "⏱ Выбери срок Jail:",
            reply_markup=build_duration_keyboard(complaint_id, 'jail_mute', duration_list=PUNISHMENT_DURATIONS['jail'])
        )

    else:
        bot.send_message(
            call.from_user.id,
            f"⏱ Выбери срок ({PUNISHMENT_LABELS[ptype]}):",
            reply_markup=build_duration_keyboard(complaint_id, ptype)
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith('pdur:'))
def handle_pdur(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ У тебя нет доступа.", show_alert=True)
        return

    _, complaint_id, ptype, duration = call.data.split(':')
    duration = int(duration)
    bot.answer_callback_query(call.id)

    ctx = PENDING_CLOSE.get(f"ctx:{complaint_id}", {'chat_id': GROUP_CHAT_ID, 'action_message_id': None})

    if ptype == 'jail_mute':
        # Это был срок Jail — теперь просим срок Mute вторым шагом
        bot.send_message(
            call.from_user.id,
            "⏱ Теперь выбери срок Mute:",
            reply_markup=build_second_duration_keyboard(complaint_id, duration)
        )
        return

    if ptype == 'warn_jail':
        parts = [('warn', None), ('jail', duration)]
    else:
        parts = [(ptype, duration)]

    PENDING_CLOSE[call.from_user.id] = {
        'mode': 'point',
        'complaint_id': complaint_id,
        'parts': parts,
        'chat_id': ctx['chat_id'],
        'action_message_id': ctx['action_message_id'],
    }
    bot.send_message(
        call.from_user.id,
        f"✍️ Напиши номер пункта правил для жалобы #{complaint_id} (например: 2.1 ОП)."
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('pdur2:'))
def handle_pdur2(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ У тебя нет доступа.", show_alert=True)
        return

    _, complaint_id, jail_duration, mute_duration = call.data.split(':')
    bot.answer_callback_query(call.id)

    ctx = PENDING_CLOSE.get(f"ctx:{complaint_id}", {'chat_id': GROUP_CHAT_ID, 'action_message_id': None})

    PENDING_CLOSE[call.from_user.id] = {
        'mode': 'point',
        'complaint_id': complaint_id,
        'parts': [('jail', int(jail_duration)), ('mute', int(mute_duration))],
        'chat_id': ctx['chat_id'],
        'action_message_id': ctx['action_message_id'],
    }
    bot.send_message(
        call.from_user.id,
        f"✍️ Напиши номер пункта правил для жалобы #{complaint_id} (например: 2.1 ОП)."
    )


def format_punishment_part(ptype, duration):
    label = PUNISHMENT_LABELS[ptype]
    if ptype == 'ban':
        return f"{label} на {duration} дней" if duration else label
    if duration:
        return f"{label} на {duration} минут"
    return label


def build_punishment_answer(detail, parts, point_text, fallback_to_name=None):
    """parts — список (ptype, duration); duration=None для Warn."""
    to_name = detail.get('to_user_name') or fallback_to_name or 'игрок'
    parts_text = " и ".join(format_punishment_part(pt, d) for pt, d in parts)

    return (
        f"Приветствую.\n\n"
        f"Игрок {to_name} получит наказание в виде {parts_text}, по п. {point_text}.\n\n"
        f"Закрыто."
    )


def build_off_command(to_name, ptype, duration, point_text, complaint_id):
    """Собирает игровую/дискорд-команду (Off* по типу наказания) для копирования."""
    cmd = OFF_COMMANDS[ptype]
    if duration:
        return f"{cmd} {to_name} {duration} {point_text} | #{complaint_id} by {ADMIN_TAG}"
    return f"{cmd} {to_name} {point_text} | #{complaint_id} by {ADMIN_TAG}"


def build_off_commands(to_name, parts, point_text, complaint_id):
    """Для комбо-наказаний — отдельная команда на каждую часть."""
    return [build_off_command(to_name, pt, d, point_text, complaint_id) for pt, d in parts]


@bot.message_handler(func=lambda m: m.from_user.id in PENDING_CLOSE and PENDING_CLOSE[m.from_user.id].get('mode') == 'point' and m.chat.type == 'private' and not (m.text or '').startswith('/'))
def handle_point_text(message):
    global OPEN_COMPLAINT_IDS

    pending = PENDING_CLOSE.pop(message.from_user.id)
    PENDING_CLOSE.pop(f"ctx:{pending['complaint_id']}", None)
    complaint_id = pending['complaint_id']
    point_text = message.text.strip()

    already_closed, detail = check_already_closed(complaint_id)
    if already_closed:
        OPEN_COMPLAINT_IDS.discard(str(complaint_id))
        who = detail.get('admin_name') or 'неизвестным админом'
        bot.send_message(message.chat.id, f"⚠️ Жалоба #{complaint_id} уже закрыта администратором {who}, отменяю.")
        if pending.get('action_message_id'):
            bot.edit_message_text(
                chat_id=pending['chat_id'],
                message_id=pending['action_message_id'],
                text=f"Действия для жалобы <code>#{complaint_id}</code>:\n\n⚠️ Жалоба уже закрыта администратором {who}.\n\n<b>Ответ:</b>\n{detail.get('answer') or 'не указан'}",
                parse_mode='HTML',
                reply_markup=None
            )
        return

    # Резервное имя — то, что было сохранено при изначальной отправке жалобы в группу,
    # на случай если API деталей вдруг не вернул to_user_name
    fallback_to_name = COMPLAINT_LINKS.get(str(complaint_id), {}).get('to_user_name')

    parts = pending['parts']
    verdict_text = build_punishment_answer(detail, parts, point_text, fallback_to_name=fallback_to_name)

    success, error, admin_name = close_complaint(complaint_id, verdict_text)
    if success:
        OPEN_COMPLAINT_IDS.discard(str(complaint_id))
        who = admin_name or 'тобой'
        bot.send_message(message.chat.id, f"✅ Жалоба #{complaint_id} закрыта с наказанием.")
        if pending.get('action_message_id'):
            bot.edit_message_text(
                chat_id=pending['chat_id'],
                message_id=pending['action_message_id'],
                text=f"Действия для жалобы #{complaint_id}:\n\n✅ Жалоба закрыта администратором {who} с ответом:\n\n{verdict_text}",
                reply_markup=None
            )
        else:
            bot.send_message(pending['chat_id'], f"✅ Жалоба #{complaint_id} закрыта:\n\n{verdict_text}")

        to_name = detail.get('to_user_name') or fallback_to_name or 'игрок'
        off_cmds = build_off_commands(to_name, parts, point_text, complaint_id)
        for cmd in off_cmds:
            bot.send_message(message.chat.id, f"<code>{cmd}</code>", parse_mode='HTML')
    else:
        bot.send_message(message.chat.id, f"⚠️ Не удалось закрыть жалобу #{complaint_id}: {error}")


@bot.message_handler(func=lambda m: m.from_user.id in PENDING_CLOSE and PENDING_CLOSE[m.from_user.id].get('mode') != 'point' and m.chat.type == 'private' and not (m.text or '').startswith('/'))
def handle_verdict_text(message):
    global OPEN_COMPLAINT_IDS

    pending = PENDING_CLOSE.pop(message.from_user.id)
    complaint_id = pending['complaint_id']

    already_closed, detail = check_already_closed(complaint_id)
    if already_closed:
        OPEN_COMPLAINT_IDS.discard(str(complaint_id))
        who = detail.get('admin_name') or 'неизвестным админом'
        bot.send_message(message.chat.id, f"⚠️ Жалоба #{complaint_id} уже закрыта администратором {who}, отменяю.")
        bot.edit_message_text(
            chat_id=pending['chat_id'],
            message_id=pending['action_message_id'],
            text=f"Действия для жалобы <code>#{complaint_id}</code>:\n\n⚠️ Жалоба уже закрыта администратором {who}.\n\n<b>Ответ:</b>\n{detail.get('answer') or 'не указан'}",
            parse_mode='HTML',
            reply_markup=None
        )
        return

    body = message.text.strip()
    verdict_text = f"Приветствую.\n\n{body}"
    if not verdict_text.endswith('Закрыто.'):
        verdict_text += "\n\nЗакрыто."

    success, error, admin_name = close_complaint(complaint_id, verdict_text)
    if success:
        OPEN_COMPLAINT_IDS.discard(str(complaint_id))
        who = admin_name or 'тобой'
        bot.send_message(message.chat.id, f"✅ Жалоба #{complaint_id} закрыта.")
        bot.edit_message_text(
            chat_id=pending['chat_id'],
            message_id=pending['action_message_id'],
            text=f"Действия для жалобы #{complaint_id}:\n\n✅ Жалоба закрыта администратором {who} с ответом:\n\n{verdict_text}",
            reply_markup=None
        )
    else:
        bot.send_message(message.chat.id, f"⚠️ Не удалось закрыть жалобу #{complaint_id}: {error}")


def mark_closed_elsewhere(complaint_id, now_str):
    """Жалоба пропала из списка новых, но мы её не закрывали сами — значит, закрыл кто-то другой.
    Обновляет сообщение в группе, убирает кнопки."""
    link_info = COMPLAINT_LINKS.get(str(complaint_id))
    if not link_info:
        return  # нечего обновлять — сообщение не отслеживается

    detail = fetch_complaint_detail(complaint_id)
    who = detail.get('admin_name') or 'другим админом'
    answer = detail.get('answer') or 'не указан'

    print(f"[{now_str}] ✅ Жалоба #{complaint_id} закрыта кем-то ещё ({who})")
    try:
        bot.edit_message_text(
            chat_id=link_info['chat_id'],
            message_id=link_info['action_message_id'],
            text=f"Действия для жалобы #{complaint_id}:\n\n✅ Жалоба закрыта администратором {who} с ответом:\n\n{answer}",
            reply_markup=None
        )
    except Exception as e:
        print(f"DEBUG - Не удалось обновить сообщение для #{complaint_id}: {e}")


def run_check(now_str=None):
    """Один прогон проверки: новые жалобы + обнаружение закрытых кем-то другим.
    Используется и фоновым потоком, и командой /check."""
    global SEEN_IDS, OPEN_COMPLAINT_IDS, LAST_CHECK_TIME, LAST_CHECK_STATUS

    if now_str is None:
        now_str = datetime.now().strftime('%H:%M:%S')

    data = fetch_complaints()
    LAST_CHECK_TIME = now_str

    if not data:
        print(f"[{now_str}] ❌ Не удалось получить данные с API.")
        LAST_CHECK_STATUS = "❌ Ошибка получения данных (возможно, протухла кука)"
        return

    complaints = data.get('complaints', [])
    fresh_ids = {str(c.get('id')) for c in complaints}

    # Новые жалобы
    new_ones = [c for c in complaints if str(c.get('id')) not in SEEN_IDS]
    if new_ones:
        new_ones.sort(key=lambda c: int(c.get('id', 0)))  # от старых к новым, по самому ID
        for complaint in new_ones:
            complaint_id = str(complaint.get('id'))
            print(f"[{now_str}] 🆕 Новая жалоба #{complaint_id}")
            send_complaint(GROUP_CHAT_ID, complaint)
            SEEN_IDS.add(complaint_id)
            OPEN_COMPLAINT_IDS.add(complaint_id)
            time.sleep(0.5)

    # Жалобы, которые были "открытыми" у нас, но пропали из списка новых на сайте —
    # значит, их закрыл кто-то без участия бота
    closed_elsewhere = OPEN_COMPLAINT_IDS - fresh_ids
    for complaint_id in closed_elsewhere:
        mark_closed_elsewhere(complaint_id, now_str)
        OPEN_COMPLAINT_IDS.discard(complaint_id)
        time.sleep(0.3)

    if new_ones:
        LAST_CHECK_STATUS = f"Найдено новых жалоб: {len(new_ones)}"
    elif closed_elsewhere:
        LAST_CHECK_STATUS = f"Новых жалоб нет, закрыто без бота: {len(closed_elsewhere)}"
    else:
        print(f"[{now_str}] ℹ️ Новых жалоб нет.")
        LAST_CHECK_STATUS = "Новых жалоб нет"


def background_checker():
    last_pending_post = time.time()

    while True:
        now_str = datetime.now().strftime('%H:%M:%S')

        if IS_RUNNING:
            run_check(now_str)

            if time.time() - last_pending_post >= PENDING_POST_INTERVAL:
                print(f"[{now_str}] 📋 Почасовой автопостинг списка висящих жалоб")
                try:
                    post_pending_list(announce_empty=False)
                except Exception as e:
                    print(f"DEBUG - Автопостинг списка жалоб не удался: {e}")
                last_pending_post = time.time()
        else:
            print(f"[{now_str}] ⏳ Жду команду /start от админа...")

        time.sleep(CHECK_INTERVAL)


def setup_bot_commands():
    """Регистрирует команды (подсказки при вводе /) и запоминает username бота для ссылок."""
    global BOT_USERNAME
    try:
        bot.set_my_commands([
            telebot.types.BotCommand("start", "Запустить бота и проверить текущие жалобы"),
            telebot.types.BotCommand("status", "Статус бота и время последней проверки"),
            telebot.types.BotCommand("pending", "Список всех висящих жалоб одним сообщением"),
            telebot.types.BotCommand("check", "Принудительная проверка новых жалоб"),
            telebot.types.BotCommand("stop", "Остановить мониторинг"),
        ])
    except Exception as e:
        print(f"DEBUG - Не удалось установить команды: {e}")

    try:
        BOT_USERNAME = bot.get_me().username
    except Exception as e:
        print(f"DEBUG - Не удалось получить username бота: {e}")


if __name__ == '__main__':
    setup_bot_commands()

    thread = threading.Thread(target=background_checker)
    thread.daemon = True
    thread.start()

    print("Бот запущен в фоновом режиме, жду /start в Telegram...")
    bot.infinity_polling(none_stop=True)
