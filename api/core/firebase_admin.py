import os

import firebase_admin
from firebase_admin import auth, credentials

FIREBASE_SERVICE_ACCOUNT_PATH = os.getenv(
    "FIREBASE_SERVICE_ACCOUNT_PATH",
    "api/firebase/fitpilot-6b389-firebase-adminsdk-fbsvc-12056c2e01.json",
)

if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred)


def verify_firebase_token(id_token: str) -> dict:
    return auth.verify_id_token(id_token)