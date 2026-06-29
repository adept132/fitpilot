import os
import json

import firebase_admin
from firebase_admin import auth, credentials

# Локальный путь оставляем для разработки на твоем компе
LOCAL_SERVICE_ACCOUNT_PATH = "api/firebase/fitpilot-6b389-firebase-adminsdk-fbsvc-12056c2e01.json"

if not firebase_admin._apps:
    # 1. Пытаемся достать JSON прямо из переменной окружения (сработает на Render)
    firebase_creds_str = os.getenv("FIREBASE_CREDENTIALS")

    if firebase_creds_str:
        # Парсим строку в словарь и отдаем Firebase
        creds_dict = json.loads(firebase_creds_str)
        cred = credentials.Certificate(creds_dict)
    else:
        # 2. Если переменной нет, читаем из файла (сработает у тебя локально)
        cred = credentials.Certificate(LOCAL_SERVICE_ACCOUNT_PATH)

    firebase_admin.initialize_app(cred)


def verify_firebase_token(id_token: str) -> dict:
    return auth.verify_id_token(id_token)