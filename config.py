import os

# ===== НАЛАШТУВАННЯ =====
# Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
CHANNEL_ID = -1003877678504  # ID каналу для копій угод

# KuCoin API ключі
EXCHANGE_API_KEY = os.getenv('EXCHANGE_API_KEY')
EXCHANGE_API_SECRET = os.getenv('EXCHANGE_API_SECRET')
EXCHANGE_API_PASSPHRASE = os.getenv('EXCHANGE_API_PASSPHRASE')

# Торгові налаштування
SYMBOLS = ['SOLUSDT', 'BTCUSDT', 'ETHUSDT', 'ARBUSDT', 'LINKUSDT', 'AVAXUSDT', 'DOTUSDT', 'UNIUSDT', 'APTUSDT']
INTERVAL = '5m'  # Таймфрейм для RSI
RSI_PERIOD = 14  # Період RSI
RSI_OVERSOLD = 30  # Рівень перепроданості
RSI_OVERBOUGHT = 70  # Рівень перекупленості
SEND_PHOTO = False
# ========================