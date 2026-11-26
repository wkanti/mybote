"""Запусти один раз на своём компьютере или в Termux, чтобы получить строку сессии номера.
pip install telethon
python make_session.py
Введи API ID и API HASH (my.telegram.org), потом номер и код входа.
Выведенную строку передай боту: /addnum +39123456789 СТРОКА
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API ID: ").strip())
api_hash = input("API HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nСТРОКА СЕССИИ (держи в секрете):\n")
    print(client.session.save())
