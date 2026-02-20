#!/usr/bin/env python3
import telebot
from telebot import types
from kucoin.client import Market
import pandas as pd
import time
import threading
from datetime import datetime
import config
from database import db
import os
import sys
import uuid
import signal

# 🆔 Унікальний ID цього екземпляра
BOT_ID = str(uuid.uuid4())[:8]
print(f"🆔 Запуск бота (ID: {BOT_ID}) - БЕЗ ТРЕЙЛІНГУ")

# 📝 Файл для блокування (унікальний для цього бота)
LOCK_FILE = '/tmp/bot_no_trailing.lock'
PID_FILE = '/tmp/bot_no_trailing.pid'

# 🔒 Перевіряємо чи вже запущений інший екземпляр
def check_single_instance():
    if os.path.exists(LOCK_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = f.read().strip()
            print(f"⚠️ Бот вже запущений з PID {old_pid}")
            print("⏹️ Зупиняємо старі процеси...")
            if os.path.exists('/app'):
                pass
            else:
                os.system("pkill -f 'python.*scalper_bot_no_trailing.py' || true")
            time.sleep(3)
            os.remove(LOCK_FILE)
            os.remove(PID_FILE)
        except:
            pass
    
    with open(LOCK_FILE, 'w') as f:
        f.write('locked')
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    
    print(f"✅ Екземпляр {BOT_ID} заблокував роботу")

check_single_instance()

# Обробник сигналів
def signal_handler(sig, frame):
    print(f"\n🛑 Отримано сигнал {sig}, завершуємо роботу...")
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    db.close()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# 🟢 Використовуємо ДРУГОГО бота (створи окремого в BotFather)
bot = telebot.TeleBot(config.TELEGRAM_TOKEN2)

# KuCoin клієнт з API ключами (ті самі)
client = Market(
    key=config.EXCHANGE_API_KEY,
    secret=config.EXCHANGE_API_SECRET,
    passphrase=config.EXCHANGE_API_PASSPHRASE
)

# Глобальний екземпляр бота
scalper_instance = None

class Position:
    def __init__(self, symbol, side, price, time):
        self.symbol = symbol
        self.side = side
        self.entry_price = price
        self.entry_time = time
        self.exit_price = None
        self.exit_time = None
        self.pnl_percent = None
        # 🚫 Трейлінг ВІДСУТНІЙ
        self.max_pnl = 0.0

class ScalperBot:
    def __init__(self):
        self.positions = {}
        self.last_state = {}
        self.running = True
        self.last_signal = {}
        self.last_trade_time = {}
        # 🚫 Трейлінг вимкнено
        self.check_interval = 5
        
        # Завантажуємо стани з БД
        self.load_states()
    
    def load_states(self):
        for symbol in config.SYMBOLS:
            state = db.load_last_state(symbol)
            if state:
                self.last_state[symbol] = state
    
    def save_state(self, symbol, state):
        db.save_last_state(symbol, state)
    
    def convert_symbol(self, symbol):
        return symbol.replace('USDT', '-USDT')
    
    def get_emas(self, symbol):
        try:
            kucoin_symbol = self.convert_symbol(symbol)
            
            now = int(time.time())
            current_minute = datetime.now().minute
            last_full_candle = now - (current_minute % 5 * 60) - (now % 60) - 300
            
            # Беремо 1000 свічок (10 запитів по 100)
            all_klines = []
            
            for i in range(10):
                start = last_full_candle - (i+1)*100*300
                end = last_full_candle - i*100*300 if i > 0 else last_full_candle
                
                klines = client.get_kline(
                    symbol=kucoin_symbol,
                    kline_type='5min',
                    start_at=start,
                    end_at=end
                )
                
                if klines:
                    all_klines.extend(klines)
                
                time.sleep(0.2)
            
            if not all_klines or len(all_klines) < 500:
                return None, None, None
            
            all_klines.sort(key=lambda x: x[0])
            closes = [float(k[2]) for k in all_klines[-500:]]
            df = pd.DataFrame(closes, columns=['close'])
            
            ema_fast = df['close'].ewm(span=20, adjust=False).mean().iloc[-1]
            ema_slow = df['close'].ewm(span=50, adjust=False).mean().iloc[-1]
            
            return ema_fast, ema_slow, closes[-1]
        except Exception as e:
            return None, None, None
    
    def get_real_price(self, symbol):
        try:
            kucoin_symbol = self.convert_symbol(symbol)
            ticker = client.get_ticker(kucoin_symbol)
            if not ticker or 'price' not in ticker:
                return None
            return float(ticker['price'])
        except Exception as e:
            return None
    
    def check_crossover(self, symbol):
        ema_fast, ema_slow, price = self.get_emas(symbol)
        if not ema_fast:
            return None, None, None
        
        real_price = self.get_real_price(symbol)
        if not real_price:
            return None, None, None
        
        current_state = 'ABOVE' if ema_fast > ema_slow else 'BELOW'
        current_time = time.time()
        
        if symbol not in self.last_state:
            self.last_state[symbol] = current_state
            self.save_state(symbol, current_state)
            return None, None, real_price
        
        if current_state != self.last_state[symbol]:
            if symbol in self.last_signal:
                last_signal_type = self.last_signal[symbol]['type']
                last_signal_time = self.last_signal[symbol]['time']
                if signal == last_signal_type and (current_time - last_signal_time) < 30:
                    return None, None, real_price
            
            signal = 'LONG' if current_state == 'ABOVE' else 'SHORT'
            
            self.last_signal[symbol] = {'type': signal, 'time': current_time}
            self.last_state[symbol] = current_state
            self.save_state(symbol, current_state)
            
            print(f"🔥 {symbol}: {signal} (ціна: {real_price:.2f})")
            return signal, current_state, real_price
        
        return None, None, real_price
    
    # 🚫 Функція трейлінгу ВІДСУТНЯ
    
    def close_position(self, symbol, exit_price, exit_time, reason="signal"):
        if symbol in self.positions:
            pos = self.positions[symbol]
            pos.exit_price = exit_price
            pos.exit_time = exit_time
            
            if pos.side == 'LONG':
                pos.pnl_percent = ((exit_price - pos.entry_price) / pos.entry_price) * 100
            else:
                pos.pnl_percent = ((pos.entry_price - exit_price) / pos.entry_price) * 100
            
            # Рахуємо максимальний профіт за угоду
            max_price = 0
            min_price = float('inf')
            
            try:
                kucoin_symbol = self.convert_symbol(symbol)
                klines = client.get_kline(
                    symbol=kucoin_symbol,
                    kline_type='5min',
                    start_at=int(pos.entry_time) - 60,
                    end_at=int(exit_time) + 60
                )
                
                if klines:
                    for k in klines:
                        high = float(k[3])
                        low = float(k[4])
                        if high > max_price:
                            max_price = high
                        if low < min_price:
                            min_price = low
            except:
                max_price = exit_price
                min_price = exit_price
            
            if pos.side == 'LONG':
                max_pnl = ((max_price - pos.entry_price) / pos.entry_price) * 100
            else:
                max_pnl = ((pos.entry_price - min_price) / pos.entry_price) * 100
            
            hold_minutes = (exit_time - pos.entry_time) / 60
            
            trade_info = {
                'symbol': symbol,
                'side': pos.side,
                'entry': pos.entry_price,
                'exit': exit_price,
                'pnl': pos.pnl_percent,
                'max_pnl': max_pnl,
                'hold_minutes': hold_minutes,
                'entry_time': datetime.fromtimestamp(pos.entry_time).strftime('%H:%M:%S'),
                'exit_time': datetime.fromtimestamp(exit_time).strftime('%H:%M:%S'),
                'exit_reason': reason
            }
            
            db.add_trade(trade_info)
            # 📤 Відправляємо в ДРУГИЙ канал
            self.send_to_channel2(trade_info)
            self.send_trade_result(trade_info, reason)
            
            del self.positions[symbol]
            return trade_info
        return None
    
    def open_position(self, symbol, side, price, current_time):
        self.positions[symbol] = Position(symbol, side, price, current_time)
        self.last_trade_time[symbol] = current_time
        
        if price < 1:
            price_str = f"{price:.4f}"
        elif price < 10:
            price_str = f"{price:.3f}"
        else:
            price_str = f"{price:.2f}"
        
        msg = (f"🆓 *НОВА ПОЗИЦІЯ*\n"
               f"Монета: {symbol}\n"
               f"Напрямок: {'🟢 LONG' if side == 'LONG' else '🔴 SHORT'}\n"
               f"Ціна входу: ${price_str}\n"
               f"Час: {datetime.fromtimestamp(current_time).strftime('%H:%M:%S')}")
        bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')
    
    def send_trade_result(self, trade, reason="signal"):
        emoji = '✅' if trade['pnl'] > 0 else '❌'
        reason_emoji = "📊"
        reason_text = "сигнал EMA"
        
        if trade['entry'] < 1 or trade['exit'] < 1:
            price_format = ".4f"
        else:
            price_format = ".2f"
        
        entry_price = f"{trade['entry']:{price_format}}"
        exit_price = f"{trade['exit']:{price_format}}"
        
        msg = (f"{emoji} *РЕЗУЛЬТАТ УГОДИ*\n"
               f"Монета: {trade['symbol']}\n"
               f"Тип: {'🟢 LONG' if trade['side'] == 'LONG' else '🔴 SHORT'}\n"
               f"Вхід: ${entry_price} → Вихід: ${exit_price}\n"
               f"📊 PnL: *{trade['pnl']:+.2f}%*\n"
               f"📈 Макс: {trade['max_pnl']:+.2f}%\n"
               f"{reason_emoji} Причина: {reason_text}\n"
               f"⏱ {trade['hold_minutes']:.1f} хв\n"
               f"🕒 {trade['entry_time']} → {trade['exit_time']}")
        bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')
    
    def send_to_channel2(self, trade_info):
        try:
            if not hasattr(config, 'CHANNEL_ID2') or not config.CHANNEL_ID2:
                print("⚠️ CHANNEL_ID2 не налаштовано")
                return
            
            if trade_info['entry'] < 1 or trade_info['exit'] < 1:
                price_format = ".4f"
            else:
                price_format = ".2f"
            
            entry_price = f"{trade_info['entry']:{price_format}}"
            exit_price = f"{trade_info['exit']:{price_format}}"
            
            emoji = '✅' if trade_info['pnl'] > 0 else '❌'
            
            msg = (f"{emoji} *УГОДА (БЕЗ ТРЕЙЛІНГУ)*\n"
                   f"Монета: {trade_info['symbol']}\n"
                   f"Тип: {'🟢 LONG' if trade_info['side'] == 'LONG' else '🔴 SHORT'}\n"
                   f"Вхід: ${entry_price} → Вихід: ${exit_price}\n"
                   f"📊 PnL: *{trade_info['pnl']:+.2f}%*\n"
                   f"📈 Макс: {trade_info['max_pnl']:+.2f}%\n"
                   f"⏱ {trade_info['hold_minutes']:.1f} хв\n"
                   f"🕒 {trade_info['entry_time']} → {trade_info['exit_time']}")
            
            bot.send_message(config.CHANNEL_ID2, msg, parse_mode='Markdown')
        except Exception as e:
            print(f"❌ Помилка каналу 2: {e}")
    
    def monitor_loop(self):
        print("🤖 Моніторинг запущено (БЕЗ ТРЕЙЛІНГУ)...")
        
        while self.running:
            current_time = time.time()
            
            # Тільки сигнали EMA, без трейлінгу
            for symbol in config.SYMBOLS:
                try:
                    signal, state, price = self.check_crossover(symbol)
                    
                    if signal:
                        if symbol in self.positions:
                            current_pos = self.positions[symbol]
                            
                            if (current_pos.side == 'LONG' and signal == 'SHORT') or \
                               (current_pos.side == 'SHORT' and signal == 'LONG'):
                                self.close_position(symbol, price, current_time, "signal")
                                time.sleep(1)
                                self.open_position(symbol, signal, price, current_time)
                        
                        else:
                            self.open_position(symbol, signal, price, current_time)
                except:
                    pass
            
            time.sleep(self.check_interval)

# ===== КОМАНДИ TELEGRAM =====
@bot.message_handler(commands=['start'])
def start_cmd(message):
    global scalper_instance
    
    if scalper_instance and scalper_instance.running:
        scalper_instance.running = False
        time.sleep(2)
    
    scalper_instance = ScalperBot()
    thread = threading.Thread(target=scalper_instance.monitor_loop, daemon=True)
    thread.start()
    
    bot.reply_to(message, "🚀 Бот запущено (БЕЗ ТРЕЙЛІНГУ)")

@bot.message_handler(commands=['stop'])
def stop_cmd(message):
    global scalper_instance
    
    if scalper_instance and scalper_instance.running:
        scalper_instance.running = False
        scalper_instance = None
        bot.reply_to(message, "⏹ Бот зупинено")
    else:
        bot.reply_to(message, "Бот не запущено")

@bot.message_handler(commands=['status'])
def status_cmd(message):
    global scalper_instance
    if scalper_instance and scalper_instance.positions:
        msg = "📊 *Активні позиції (БЕЗ ТРЕЙЛІНГУ):*\n"
        for symbol, pos in scalper_instance.positions.items():
            current_price = scalper_instance.get_real_price(symbol) or 0
            if pos.side == 'LONG':
                pnl = ((current_price - pos.entry_price) / pos.entry_price) * 100
            else:
                pnl = ((pos.entry_price - current_price) / pos.entry_price) * 100
            
            hold_time = (time.time() - pos.entry_time) / 60
            
            if pos.entry_price < 1:
                entry_str = f"{pos.entry_price:.4f}"
            elif pos.entry_price < 10:
                entry_str = f"{pos.entry_price:.3f}"
            else:
                entry_str = f"{pos.entry_price:.2f}"
            
            msg += (f"\n{symbol}: {'🟢 LONG' if pos.side == 'LONG' else '🔴 SHORT'}\n"
                    f"Вхід: ${entry_str}\n"
                    f"PnL: {pnl:+.2f}%\n"
                    f"📈 макс: {pos.max_pnl:+.2f}% | ⏱ {hold_time:.1f} хв\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає активних позицій")

if __name__ == '__main__':
    try:
        print("🤖 Telegram Scalper Bot (БЕЗ ТРЕЙЛІНГУ) запущено...")
        print(f"Моніторинг: {config.SYMBOLS}")
        print(f"EMA 20/50 на 5хв")
        if hasattr(config, 'CHANNEL_ID2') and config.CHANNEL_ID2:
            print(f"📤 Канал 2: {config.CHANNEL_ID2}")
        
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
        
    except Exception as e:
        print(f"❌ Помилка: {e}")
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        db.close()