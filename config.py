import os

# ===== НАЛАШТУВАННЯ =====
# Telegram (буде в змінних Railway)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

# Торгові налаштування
SYMBOLS = ['SOLUSDT', 'BTCUSDT', 'ETHUSDT']
INTERVAL = '1m'
EMA_FAST = 12
EMA_SLOW = 26
SEND_PHOTO = False
# ========================