import telebot
from telebot import types
from kucoin.client import Market
import pandas as pd
import time
import threading
from datetime import datetime
import config
from database import db

bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market()

# Глобальний екземпляр бота
scalper_instance = None

class Position:
    def __init__(self, symbol, side, price, time):
        self.symbol = symbol
        self.side = side  # 'LONG' or 'SHORT'
        self.entry_price = price
        self.entry_time = time
        self.exit_price = None
        self.exit_time = None
        self.pnl_percent = None

class ScalperBot:
    def __init__(self):
        self.positions = {}
        self.last_state = {}  # {symbol: 'ABOVE'/'BELOW'}
        self.running = True
    
    def convert_symbol(self, symbol):
        return symbol.replace('USDT', '-USDT')
    
    def get_emas(self, symbol):
        try:
            kucoin_symbol = self.convert_symbol(symbol)
            klines = client.get_kline(
                symbol=kucoin_symbol,
                kline_type='1min',
                start_at=int(time.time()) - 150*60,
                end_at=int(time.time())
            )
            
            if not klines or len(klines) < 30:
                return None, None, None
            
            closes = [float(k[2]) for k in klines]
            df = pd.DataFrame(closes, columns=['close'])
            
            ema_fast = df['close'].ewm(span=12, adjust=False, min_periods=12).mean().iloc[-1]
            ema_slow = df['close'].ewm(span=26, adjust=False, min_periods=26).mean().iloc[-1]
            current_price = closes[-1]
            
            return ema_fast, ema_slow, current_price
        except Exception as e:
            print(f"Помилка для {symbol}: {e}")
            return None, None, None
    
    def check_crossover(self, symbol):
        """Перевіряє перетин EMA для пари"""
        ema_fast, ema_slow, price = self.get_emas(symbol)
        if not ema_fast:
            return None, None, None
        
        current_state = 'ABOVE' if ema_fast > ema_slow else 'BELOW'
        
        # Перший запуск - тільки запам'ятовуємо стан
        if symbol not in self.last_state:
            self.last_state[symbol] = current_state
            print(f"📊 {symbol}: початковий стан {current_state}")
            return None, None, price
        
        # ПЕРЕТИН! Стан змінився
        if current_state != self.last_state[symbol]:
            signal = 'LONG' if current_state == 'ABOVE' else 'SHORT'
            self.last_state[symbol] = current_state
            return signal, current_state, price
        
        return None, None, price
    
    def close_position(self, symbol, exit_price, exit_time):
        """Закриває позицію і рахує результат"""
        if symbol in self.positions:
            pos = self.positions[symbol]
            pos.exit_price = exit_price
            pos.exit_time = exit_time
            
            # PnL без комісій
            if pos.side == 'LONG':
                pos.pnl_percent = ((exit_price - pos.entry_price) / pos.entry_price) * 100
            else:  # SHORT
                pos.pnl_percent = ((pos.entry_price - exit_price) / pos.entry_price) * 100
            
            hold_minutes = (exit_time - pos.entry_time) / 60
            
            trade_info = {
                'symbol': symbol,
                'side': pos.side,
                'entry': round(pos.entry_price, 2),
                'exit': round(exit_price, 2),
                'pnl': round(pos.pnl_percent, 2),
                'hold_minutes': round(hold_minutes, 1),
                'entry_time': datetime.fromtimestamp(pos.entry_time).strftime('%H:%M:%S'),
                'exit_time': datetime.fromtimestamp(exit_time).strftime('%H:%M:%S')
            }
            
            # Зберігаємо в БД
            db.add_trade(trade_info)
            
            self.send_trade_result(trade_info)
            del self.positions[symbol]
            return trade_info
        return None
    
    def open_position(self, symbol, side, price, current_time):
        """Відкриває нову позицію"""
        self.positions[symbol] = Position(symbol, side, price, current_time)
        
        msg = (f"🆓 *НОВА ПОЗИЦІЯ*\n"
               f"Монета: {symbol}\n"
               f"Напрямок: {'🟢 LONG' if side == 'LONG' else '🔴 SHORT'}\n"
               f"Ціна входу: ${round(price, 2)}\n"
               f"Час: {datetime.fromtimestamp(current_time).strftime('%H:%M:%S')}")
        bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')
    
    def send_trade_result(self, trade):
        """Відправляє результат угоди"""
        emoji = '✅' if trade['pnl'] > 0 else '❌'
        msg = (f"{emoji} *РЕЗУЛЬТАТ УГОДИ*\n"
               f"Монета: {trade['symbol']}\n"
               f"Тип: {'🟢 LONG' if trade['side'] == 'LONG' else '🔴 SHORT'}\n"
               f"Вхід: ${trade['entry']} → Вихід: ${trade['exit']}\n"
               f"📊 PnL: *{trade['pnl']:+.2f}%*\n"
               f"⏱ Час утримання: {trade['hold_minutes']} хв\n"
               f"🕒 {trade['entry_time']} → {trade['exit_time']}")
        bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')
    
    def monitor_loop(self):
        """Головний цикл моніторингу"""
        print("🤖 Моніторинг запущено. Чекаємо на перетин EMA...")
        
        while self.running:
            current_time = time.time()
            
            for symbol in config.SYMBOLS:
                try:
                    signal, state, price = self.check_crossover(symbol)
                    
                    if signal:
                        print(f"🔥 {symbol}: СИГНАЛ {signal} (ціна: {price})")
                        
                        # Якщо є відкрита позиція для цієї пари - закриваємо
                        if symbol in self.positions:
                            current_pos = self.positions[symbol]
                            
                            # Закриваємо ТІЛЬКИ якщо сигнал протилежний
                            if (current_pos.side == 'LONG' and signal == 'SHORT') or \
                               (current_pos.side == 'SHORT' and signal == 'LONG'):
                                self.close_position(symbol, price, current_time)
                                # Відкриваємо нову позицію (протилежну)
                                self.open_position(symbol, signal, price, current_time)
                            else:
                                print(f"⚠️ {symbol}: ігноруємо {signal} - вже є {current_pos.side}")
                        
                        else:
                            # Немає позиції - відкриваємо нову
                            self.open_position(symbol, signal, price, current_time)
                    
                except Exception as e:
                    print(f"Помилка для {symbol}: {e}")
            
            time.sleep(5)  # Перевірка кожні 5 секунд

# ===== КОМАНДИ TELEGRAM =====
@bot.message_handler(commands=['start'])
def start_cmd(message):
    global scalper_instance
    
    # Якщо бот вже запущено, зупиняємо старий екземпляр
    if scalper_instance and scalper_instance.running:
        scalper_instance.running = False
        time.sleep(2)  # Чекаємо поки зупиниться
    
    # Створюємо новий екземпляр
    scalper_instance = ScalperBot()
    thread = threading.Thread(target=scalper_instance.monitor_loop, daemon=True)
    thread.start()
    
    bot.reply_to(message, "🚀 Бот запущено! Чекаємо на перетин EMA 12/26...")

@bot.message_handler(commands=['stop'])
def stop_cmd(message):
    global scalper_instance
    
    if scalper_instance and scalper_instance.running:
        scalper_instance.running = False
        scalper_instance = None
        bot.reply_to(message, "⏹ Бот зупинено. База даних ЗБЕРЕЖЕНА!")
    else:
        bot.reply_to(message, "Бот не запущено")

@bot.message_handler(commands=['cleardb'])
def cleardb_cmd(message):
    """Очистити базу даних (тільки якщо треба)"""
    global scalper_instance
    
    # Перевіряємо чи бот не працює
    if scalper_instance and scalper_instance.running:
        bot.reply_to(message, "❌ Спочатку зупиніть бот командою /stop")
        return
    
    # Підтвердження
    markup = types.InlineKeyboardMarkup()
    btn1 = types.InlineKeyboardButton("✅ ТАК, очистити", callback_data="clear_yes")
    btn2 = types.InlineKeyboardButton("❌ НІ, скасувати", callback_data="clear_no")
    markup.add(btn1, btn2)
    
    bot.reply_to(message, "⚠️ *ВИ ВПЕВНЕНІ?*\nЦе безповоротно видалить ВСЮ історію угод!", 
                reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data == "clear_yes":
        # Очищаємо БД
        db.clear_all_data()
        bot.edit_message_text("✅ Базу даних очищено!", 
                            call.message.chat.id, 
                            call.message.message_id)
    elif call.data == "clear_no":
        bot.edit_message_text("❌ Скасовано", 
                            call.message.chat.id, 
                            call.message.message_id)

# ... (решта команд /price, /status, /history, /stats, /maxprofits, /maxlosses, /records, /daily, /hourly, /weekly, /monthly, /analyze, /menu - БЕЗ ЗМІН)