import os

# ===== НАЛАШТУВАННЯ =====
# Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
CHANNEL_ID = -1003877678504  # ID каналу для копій угод

# 🟢 KuCoin API ключі (додай в Railway Variables)
EXCHANGE_API_KEY = os.getenv('EXCHANGE_API_KEY')
EXCHANGE_API_SECRET = os.getenv('EXCHANGE_API_SECRET')
EXCHANGE_API_PASSPHRASE = os.getenv('EXCHANGE_API_PASSPHRASE')  # KuCoin вимагає passphrase!

# Торгові налаштування
#SYMBOLS = ['SOLUSDT', 'BTCUSDT', 'ETHUSDT', 'ARBUSDT', 'LINKUSDT', 'AVAXUSDT', 'DOTUSDT', 'UNIUSDT', 'APTUSDT']
SYMBOLS = ['BTCUSDT']
INTERVAL = '5m'
EMA_FAST = 20
EMA_SLOW = 50
SEND_PHOTO = False
# ========================