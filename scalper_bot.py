#!/usr/bin/env python3
import telebot
from telebot import types
from binance.client import Client
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
print(f"🆔 Запуск бота (ID: {BOT_ID})")

# 📝 Файл для блокування
LOCK_FILE = '/tmp/bot.lock'
PID_FILE = '/tmp/bot.pid'

# 🔒 Перевіряємо чи вже запущений інший екземпляр
def check_single_instance():
    if os.path.exists(LOCK_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = f.read().strip()
            print(f"⚠️ Бот вже запущений з PID {old_pid}")
            print("⏹️ Зупиняємо старі процеси...")
            os.system("pkill -f 'python.*scalper_bot.py' || true")
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

bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market()

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
        # 🎯 Для трейлінг-стопу (тимчасово відключено)
        self.max_pnl = 0.0
        self.trailing_activated = False

class ScalperBot:
    def __init__(self):
        self.positions = {}
        self.last_state = {}  # Буде завантажено з БД
        self.running = True
        self.last_signal = {}
        self.last_trade_time = {}
        # 🎯 Налаштування (трейлер вимкнено)
        self.check_interval = 5  # Перевірка кожні 5 секунд
        self.use_trailing = False  # Трейлер ВИМКНЕНО
        
        # Завантажуємо стани з БД
        self.load_states()
    
    def load_states(self):
        """Завантажує збережені стани з БД"""
        for symbol in config.SYMBOLS:
            state = db.load_last_state(symbol)
            if state:
                self.last_state[symbol] = state
                print(f"📥 {symbol}: завантажено стан {state} з БД")
    
    def save_state(self, symbol, state):
        """Зберігає стан в БД"""
        db.save_last_state(symbol, state)
    
    def convert_symbol(self, symbol):
        return symbol  # Binance використовує SOLUSDT, не SOL-USDT
    
    def get_emas(self, symbol):
        try:
            # Отримуємо свічки з Binance
            klines = client.get_klines(
                symbol=symbol,
                interval=Client.KLINE_INTERVAL_5MINUTE,
                limit=500  # 500 свічок достатньо
            )
        
            if not klines or len(klines) < 100:
                return None, None, None
        
            closes = [float(k[4]) for k in klines]  # ціна закриття
            df = pd.DataFrame(closes, columns=['close'])
        
            ema_fast = df['close'].ewm(span=20).mean().iloc[-1]
            ema_slow = df['close'].ewm(span=50).mean().iloc[-1]
        
            return ema_fast, ema_slow, closes[-1]
        except Exception as e:
            print(f"Помилка {symbol}: {e}")
            return None, None, None
    
    def get_real_price(self, symbol):
        try:
            ticker = client.get_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            print(f"Помилка ціни {symbol}: {e}")
            return None
    
    def check_crossover(self, symbol):
        """Перевіряє перетин EMA 20/50 на 5хв свічках"""
        ema_fast, ema_slow, price = self.get_emas(symbol)
        if not ema_fast:
            return None, None, None
        
        # Беремо РЕАЛЬНУ ціну для входу
        real_price = self.get_real_price(symbol)
        if not real_price:
            return None, None, None
        
        current_state = 'ABOVE' if ema_fast > ema_slow else 'BELOW'
        current_time = time.time()
        if ema_fast < 1 or ema_slow < 1:
            ema_format = ".4f"
        elif ema_fast < 10 or ema_slow < 10:
            ema_format = ".3f"
        else:
            ema_format = ".2f"
        # Логуємо EMA для перевірки
        print(f"📊 {symbol}: EMA20={ema_fast:{ema_format}}, EMA50={ema_slow:{ema_format}}, diff={ema_fast-ema_slow:{ema_format}}, стан={current_state}")
        
        # Якщо стан не завантажився з БД - зберігаємо
        if symbol not in self.last_state:
            self.last_state[symbol] = current_state
            self.save_state(symbol, current_state)
            print(f"📊 {symbol}: початковий стан {current_state} (збережено в БД)")
            return None, None, real_price
        
        # ПЕРЕТИН! Стан змінився
        if current_state != self.last_state[symbol]:
            signal = 'LONG' if current_state == 'ABOVE' else 'SHORT'
            
            # Захист від дублікатів (30 секунд)
            if symbol in self.last_signal:
                last_signal_type = self.last_signal[symbol]['type']
                last_signal_time = self.last_signal[symbol]['time']
                if signal == last_signal_type and (current_time - last_signal_time) < 30:
                    print(f"⏱️ {symbol}: ігноруємо дублікат {signal}")
                    return None, None, real_price
            
            # Зберігаємо новий стан
            self.last_signal[symbol] = {'type': signal, 'time': current_time}
            self.last_state[symbol] = current_state
            self.save_state(symbol, current_state)
            
            print(f"🔥 {symbol}: ПЕРЕТИН EMA! {signal} (ціна: {real_price})")
            return signal, current_state, real_price
        
        return None, None, real_price
    
    def check_trailing_stop(self, symbol, current_price):
        """Трейлінг-стоп (ТИМЧАСОВО ВІДКЛЮЧЕНО)"""
        return False
    
    def close_position(self, symbol, exit_price, exit_time, reason="signal"):
        if symbol in self.positions:
            pos = self.positions[symbol]
            pos.exit_price = exit_price
            pos.exit_time = exit_time
            
            if pos.side == 'LONG':
                pos.pnl_percent = ((exit_price - pos.entry_price) / pos.entry_price) * 100
            else:
                pos.pnl_percent = ((pos.entry_price - exit_price) / pos.entry_price) * 100
            
            # Рахуємо максимальний профіт за угоду (для статистики)
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
                        high = float(k[1])
                        low = float(k[2])
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
            
            # Зберігаємо в БД
            db.add_trade(trade_info)
            
            # 📤 Відправляємо в канал
            self.send_to_channel(trade_info)
            
            # Відправляємо результат
            self.send_trade_result(trade_info, reason)
            
            del self.positions[symbol]
            return trade_info
        return None
    
    def open_position(self, symbol, side, price, current_time):
        self.positions[symbol] = Position(symbol, side, price, current_time)
        self.last_trade_time[symbol] = current_time
        
        # Форматуємо ціну для відображення
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
        reason_emoji = "📊" if reason == "signal" else "🎯"
        reason_text = "сигнал EMA" if reason == "signal" else "трейлінг-стоп"
        
        # Визначаємо формат ціни
        if trade['entry'] < 1 or trade['exit'] < 1:
            price_format = ".4f"
        else:
            price_format = ".2f"
        
        # Форматуємо ціни
        entry_price = f"{trade['entry']:{price_format}}"
        exit_price = f"{trade['exit']:{price_format}}"
        
        max_profit_line = f"📈 Макс. профіт: {trade['max_pnl']:+.2f}%\n"
        
        msg = (f"{emoji} *РЕЗУЛЬТАТ УГОДИ*\n"
               f"Монета: {trade['symbol']}\n"
               f"Тип: {'🟢 LONG' if trade['side'] == 'LONG' else '🔴 SHORT'}\n"
               f"Вхід: ${entry_price} → Вихід: ${exit_price}\n"
               f"📊 PnL: *{trade['pnl']:+.2f}%*\n"
               f"{max_profit_line}"
               f"{reason_emoji} Причина: {reason_text}\n"
               f"⏱ Час утримання: {trade['hold_minutes']:.1f} хв\n"
               f"🕒 {trade['entry_time']} → {trade['exit_time']}")
        bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')
    
    def send_to_channel(self, trade_info):
        try:
            if not hasattr(config, 'CHANNEL_ID') or not config.CHANNEL_ID:
                return
            
            # Визначаємо формат ціни
            if trade_info['entry'] < 1 or trade_info['exit'] < 1:
                price_format = ".4f"
            else:
                price_format = ".2f"
            
            # Форматуємо ціни
            entry_price = f"{trade_info['entry']:{price_format}}"
            exit_price = f"{trade_info['exit']:{price_format}}"
            
            emoji = '✅' if trade_info['pnl'] > 0 else '❌'
            reason_emoji = "🎯" if trade_info.get('exit_reason') == 'trailing' else "📊"
            
            msg = (f"{emoji} *УГОДА*\n"
                   f"Монета: {trade_info['symbol']}\n"
                   f"Тип: {'🟢 LONG' if trade_info['side'] == 'LONG' else '🔴 SHORT'}\n"
                   f"Вхід: ${entry_price} → Вихід: ${exit_price}\n"
                   f"📊 PnL: *{trade_info['pnl']:+.2f}%*\n"
                   f"📈 Макс: {trade_info['max_pnl']:+.2f}%\n"
                   f"{reason_emoji} {trade_info.get('exit_reason', 'signal')}\n"
                   f"⏱ {trade_info['hold_minutes']:.1f} хв\n"
                   f"🕒 {trade_info['entry_time']} → {trade_info['exit_time']}")
            
            global bot
            bot.send_message(config.CHANNEL_ID, msg, parse_mode='Markdown')
        except Exception as e:
            print(f"❌ Помилка відправки в канал: {e}")
    
    def monitor_loop(self):
        print("🤖 Моніторинг запущено. Чекаємо на перетин EMA 20/50 на 5хв...")
        print(f"📊 Трейлінг-стоп: ВИМКНЕНО (тільки сигнали EMA)")
    
        while self.running:
            current_time = time.time()
        
            # Перевіряємо сигнали EMA для всіх монет
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
                                print(f"⚠️ {symbol}: ігноруємо {signal} - вже є {current_pos.side}")
                    
                        else:
                            self.open_position(symbol, signal, price, current_time)
                
                except Exception as e:
                    print(f"Помилка для {symbol}: {e}")
        
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
    
    bot.reply_to(message, "🚀 Бот запущено! Чекаємо на перетин EMA 20/50 на 5хв...")

@bot.message_handler(commands=['stop'])
def stop_cmd(message):
    global scalper_instance
    
    if scalper_instance and scalper_instance.running:
        scalper_instance.running = False
        scalper_instance = None
        bot.reply_to(message, "⏹ Бот зупинено. Стан EMA збережено в БД!")
    else:
        bot.reply_to(message, "Бот не запущено")

@bot.message_handler(commands=['price'])
def price_cmd(message):
    try:
        msg = "💰 *Поточні ціни та EMA (KuCoin):*\n"
        for symbol in config.SYMBOLS:
            # Отримуємо EMA
            ema_fast, ema_slow, _ = scalper_instance.get_emas(symbol) if scalper_instance else (None, None, None)
            
            # Отримуємо реальну ціну
            kucoin_symbol = symbol.replace('USDT', '-USDT')
            ticker = client.get_ticker(kucoin_symbol)
            price = float(ticker['price'])
            
            # Форматуємо ціну
            if price < 1:
                price_str = f"{price:.4f}"
            elif price < 10:
                price_str = f"{price:.3f}"
            else:
                price_str = f"{price:.2f}"
            
            # Форматуємо EMA
            if ema_fast and ema_slow:
                if ema_fast < 1 or ema_slow < 1:
                    ema_format = ".4f"
                elif ema_fast < 10 or ema_slow < 10:
                    ema_format = ".3f"
                else:
                    ema_format = ".2f"
                
                ema_fast_str = f"{ema_fast:{ema_format}}"
                ema_slow_str = f"{ema_slow:{ema_format}}"
                diff = ema_fast - ema_slow
                ema_line = f"\n   EMA20: ${ema_fast_str} | EMA50: ${ema_slow_str} | diff: {diff:+.2f}"
            else:
                ema_line = "\n   EMA: немає даних"
            
            msg += f"\n{symbol}: ${price_str}{ema_line}"
        bot.reply_to(message, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Помилка: {e}")

@bot.message_handler(commands=['status'])
def status_cmd(message):
    global scalper_instance
    if scalper_instance and scalper_instance.positions:
        msg = "📊 *Активні позиції:*\n"
        for symbol, pos in scalper_instance.positions.items():
            current_price = scalper_instance.get_real_price(symbol) or 0
            if pos.side == 'LONG':
                pnl = ((current_price - pos.entry_price) / pos.entry_price) * 100
            else:
                pnl = ((pos.entry_price - current_price) / pos.entry_price) * 100
            
            hold_time = (time.time() - pos.entry_time) / 60
            
            # Форматуємо ціну входу
            if pos.entry_price < 1:
                entry_str = f"{pos.entry_price:.4f}"
            elif pos.entry_price < 10:
                entry_str = f"{pos.entry_price:.3f}"
            else:
                entry_str = f"{pos.entry_price:.2f}"
            
            msg += (f"\n{symbol}: {'🟢 LONG' if pos.side == 'LONG' else '🔴 SHORT'}\n"
                    f"Вхід: ${entry_str}\n"
                    f"Поточна PnL: {pnl:+.2f}%\n"
                    f"⏱ {hold_time:.1f} хв\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає активних позицій")

@bot.message_handler(commands=['history'])
def history_cmd(message):
    trades = db.get_trades(limit=10)
    if len(trades) > 0:
        msg = "📜 *Останні 10 угод:*\n\n"
        for _, trade in trades.iterrows():
            emoji = '✅' if trade['pnl_percent'] > 0 else '❌'
            reason_emoji = "🎯" if 'exit_reason' in trade and trade['exit_reason'] == 'trailing' else "📊"
            
            # Форматуємо ціни
            if trade['entry_price'] < 1 or trade['exit_price'] < 1:
                entry_str = f"{trade['entry_price']:.4f}"
                exit_str = f"{trade['exit_price']:.4f}"
            else:
                entry_str = f"{trade['entry_price']:.2f}"
                exit_str = f"{trade['exit_price']:.2f}"
            
            msg += (f"{emoji} {trade['symbol']} {trade['side']}\n"
                   f"PnL: {trade['pnl_percent']:+.2f}% | {reason_emoji} {trade.get('exit_reason', 'signal')}\n"
                   f"${entry_str} → ${exit_str}\n"
                   f"{trade['entry_time']} → {trade['exit_time']}\n\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Історія угод порожня")

@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    analysis = db.get_detailed_analysis()
    if not analysis:
        bot.reply_to(message, "Немає даних для статистики")
        return
    
    msg = "📊 *ЗАГАЛЬНА СТАТИСТИКА*\n\n"
    msg += f"📈 Всього угод: {analysis['total_trades']}\n"
    msg += f"✅ Прибуткових: {analysis['wins']}\n"
    msg += f"❌ Збиткових: {analysis['losses']}\n"
    msg += f"🎯 Загальний вінрейт: {analysis['winrate']:.1f}%\n"
    msg += f"💰 Загальний PnL: {analysis['total_pnl']:+.2f}%\n"
    msg += f"📊 Середній PnL: {analysis['avg_pnl']:+.2f}%\n"
    msg += f"🏆 Найкраща угода: {analysis['best_trade']:+.2f}%\n"
    msg += f"💔 Найгірша угода: {analysis['worst_trade']:+.2f}%\n"
    msg += f"⏱ Сер. час утримання: {analysis['avg_hold']:.1f} хв\n"
    msg += f"📊 Профіт фактор: {analysis['profit_factor']:.2f}"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['maxprofits'])
def maxprofits_cmd(message):
    max_profits = db.get_max_profits(limit=10)
    if len(max_profits) > 0:
        msg = "🏆 *ТОП-10 НАЙБІЛЬШИХ ПРИБУТКІВ*\n\n"
        for i, (_, trade) in enumerate(max_profits.iterrows(), 1):
            emoji = '🥇' if i == 1 else '🥈' if i == 2 else '🥉' if i == 3 else '📈'
            
            # Форматуємо ціни
            if trade['entry_price'] < 1 or trade['exit_price'] < 1:
                entry_str = f"{trade['entry_price']:.4f}"
                exit_str = f"{trade['exit_price']:.4f}"
            else:
                entry_str = f"{trade['entry_price']:.2f}"
                exit_str = f"{trade['exit_price']:.2f}"
            
            msg += (f"{emoji} *{i}. {trade['symbol']} {trade['side']}*\n"
                   f"   PnL: *{trade['pnl_percent']:+.2f}%*\n"
                   f"   Вхід: ${entry_str} → Вихід: ${exit_str}\n"
                   f"   Час: {trade['hold_minutes']} хв\n"
                   f"   {trade['entry_time']} → {trade['exit_time']}\n\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає даних про прибутки")

@bot.message_handler(commands=['maxlosses'])
def maxlosses_cmd(message):
    max_losses = db.get_max_losses(limit=10)
    if len(max_losses) > 0:
        msg = "💔 *ТОП-10 НАЙБІЛЬШИХ ЗБИТКІВ*\n\n"
        for i, (_, trade) in enumerate(max_losses.iterrows(), 1):
            emoji = '💀' if i == 1 else '😱' if i == 2 else '😭' if i == 3 else '📉'
            
            # Форматуємо ціни
            if trade['entry_price'] < 1 or trade['exit_price'] < 1:
                entry_str = f"{trade['entry_price']:.4f}"
                exit_str = f"{trade['exit_price']:.4f}"
            else:
                entry_str = f"{trade['entry_price']:.2f}"
                exit_str = f"{trade['exit_price']:.2f}"
            
            msg += (f"{emoji} *{i}. {trade['symbol']} {trade['side']}*\n"
                   f"   PnL: *{trade['pnl_percent']:+.2f}%*\n"
                   f"   Вхід: ${entry_str} → Вихід: ${exit_str}\n"
                   f"   Час: {trade['hold_minutes']} хв\n"
                   f"   {trade['entry_time']} → {trade['exit_time']}\n\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає даних про збитки")

@bot.message_handler(commands=['records'])
def records_cmd(message):
    records = db.get_records()
    if len(records) > 0:
        msg = "🎯 *РЕКОРДИ*\n\n"
        for _, record in records.iterrows():
            if record['record_type'] == 'MAX_PROFIT':
                # Форматуємо ціни
                if record['entry_price'] < 1 or record['exit_price'] < 1:
                    entry_str = f"{record['entry_price']:.4f}"
                    exit_str = f"{record['exit_price']:.4f}"
                else:
                    entry_str = f"{record['entry_price']:.2f}"
                    exit_str = f"{record['exit_price']:.2f}"
                
                msg += f"🏆 *Найбільший прибуток:*\n"
                msg += f"   {record['symbol']} {record['side']}: +{record['value']:.2f}%\n"
                msg += f"   Вхід: ${entry_str} → Вихід: ${exit_str}\n"
                msg += f"   {record['entry_time']} → {record['exit_time']}\n\n"
            elif record['record_type'] == 'MAX_LOSS':
                # Форматуємо ціни
                if record['entry_price'] < 1 or record['exit_price'] < 1:
                    entry_str = f"{record['entry_price']:.4f}"
                    exit_str = f"{record['exit_price']:.4f}"
                else:
                    entry_str = f"{record['entry_price']:.2f}"
                    exit_str = f"{record['exit_price']:.2f}"
                
                msg += f"💔 *Найбільший збиток:*\n"
                msg += f"   {record['symbol']} {record['side']}: {record['value']:.2f}%\n"
                msg += f"   Вхід: ${entry_str} → Вихід: ${exit_str}\n"
                msg += f"   {record['entry_time']} → {record['exit_time']}\n\n"
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає рекордів")

@bot.message_handler(commands=['daily'])
def daily_cmd(message):
    daily = db.get_daily_stats(days=7)
    if len(daily) > 0:
        msg = "📅 *ОСТАННІ 7 ДНІВ*\n\n"
        for _, day in daily.iterrows():
            winrate = (day['wins'] / day['total_trades'] * 100) if day['total_trades'] > 0 else 0
            msg += (f"*{day['date']} - {day['symbol']}*\n"
                   f"Угод: {day['total_trades']} | PnL: {day['total_pnl']:+.2f}%\n"
                   f"✅ {day['wins']} | ❌ {day['losses']} | вінрейт: {winrate:.0f}%\n"
                   f"📈 Max: {day['max_profit']:+.2f}% | Min: {day['max_loss']:+.2f}%\n\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає даних за дні")

@bot.message_handler(commands=['hourly'])
def hourly_cmd(message):
    hourly = db.get_hourly_stats()
    if len(hourly) > 0:
        msg = "🕐 *ГОДИННА СТАТИСТИКА*\n\n"
        for _, hour in hourly.iterrows():
            msg += (f"*{hour['hour']:02d}:00 - {hour['symbol']}*\n"
                   f"Угод: {hour['total_trades']} | PnL: {hour['avg_pnl']:+.2f}%\n"
                   f"Вінрейт: {hour['winrate']}% | Max: {hour['max_profit']:+.2f}%\n\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає даних за години")

@bot.message_handler(commands=['weekly'])
def weekly_cmd(message):
    weekly = db.get_weekly_stats(weeks=4)
    if len(weekly) > 0:
        msg = "📊 *ТИЖНЕВА СТАТИСТИКА*\n\n"
        for _, week in weekly.iterrows():
            winrate = (week['wins'] / week['total_trades'] * 100) if week['total_trades'] > 0 else 0
            msg += (f"*Тиждень {week['week']}, {week['year']} - {week['symbol']}*\n"
                   f"Угод: {week['total_trades']} | PnL: {week['total_pnl']:+.2f}%\n"
                   f"✅ {week['wins']} | ❌ {week['losses']} | вінрейт: {winrate:.0f}%\n"
                   f"📈 Max: {week['max_profit']:+.2f}% | Min: {week['max_loss']:+.2f}%\n\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає даних за тижні")

@bot.message_handler(commands=['monthly'])
def monthly_cmd(message):
    monthly = db.get_monthly_stats(months=6)
    if len(monthly) > 0:
        msg = "📊 *МІСЯЧНА СТАТИСТИКА*\n\n"
        months = ['Січ', 'Лют', 'Бер', 'Кві', 'Тра', 'Чер', 'Лип', 'Сер', 'Вер', 'Жов', 'Лис', 'Гру']
        for _, month in monthly.iterrows():
            winrate = (month['wins'] / month['total_trades'] * 100) if month['total_trades'] > 0 else 0
            msg += (f"*{months[month['month']-1]} {month['year']} - {month['symbol']}*\n"
                   f"Угод: {month['total_trades']} | PnL: {month['total_pnl']:+.2f}%\n"
                   f"✅ {month['wins']} | ❌ {month['losses']} | вінрейт: {winrate:.0f}%\n"
                   f"📈 Max: {month['max_profit']:+.2f}% | Min: {month['max_loss']:+.2f}%\n\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає даних за місяці")

@bot.message_handler(commands=['analyze'])
def analyze_cmd(message):
    analysis = db.get_detailed_analysis()
    if not analysis:
        bot.reply_to(message, "Немає даних для аналізу")
        return
    
    msg = "📊 *ДЕТАЛЬНИЙ АНАЛІЗ*\n\n"
    
    msg += f"*ЗАГАЛЬНЕ*\n"
    msg += f"📈 Угод: {analysis['total_trades']}\n"
    msg += f"💰 Заг. PnL: {analysis['total_pnl']:+.2f}%\n"
    msg += f"🎯 Вінрейт: {analysis['winrate']:.1f}%\n"
    msg += f"📊 Профіт фактор: {analysis['profit_factor']:.2f}\n\n"
    
    if analysis['records']:
        msg += f"*РЕКОРДИ*\n"
        for record in analysis['records']:
            if record['record_type'] == 'MAX_PROFIT':
                msg += f"🏆 Max прибуток: +{record['value']:.2f}% ({record['symbol']})\n"
            else:
                msg += f"💔 Max збиток: {record['value']:.2f}% ({record['symbol']})\n"
        msg += "\n"
    
    msg += f"*АНАЛІЗ ПО ГОДИНАХ*\n"
    for hour, stats in analysis['by_hour'].iterrows():
        if stats[('pnl_percent', 'count')] >= 3:
            msg += (f"{hour:02d}:00 - {hour+1:02d}:00 | "
                   f"угод: {int(stats[('pnl_percent', 'count')])} | "
                   f"сер: {stats[('pnl_percent', 'mean')]:+.2f}% | "
                   f"max: {stats[('pnl_percent', 'max')]:+.2f}%\n")
    msg += "\n"
    
    msg += f"*АНАЛІЗ ПО ДНЯХ ТИЖНЯ*\n"
    days = ['Пон', 'Вів', 'Сер', 'Чет', 'Пят', 'Суб', 'Нед']
    for day, stats in analysis['by_day'].iterrows():
        if stats[('pnl_percent', 'count')] >= 3:
            msg += (f"{days[day]} | "
                   f"угод: {int(stats[('pnl_percent', 'count')])} | "
                   f"сер: {stats[('pnl_percent', 'mean')]:+.2f}% | "
                   f"max: {stats[('pnl_percent', 'max')]:+.2f}%\n")
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['cleardb'])
def cleardb_cmd(message):
    global scalper_instance
    
    if scalper_instance and scalper_instance.running:
        bot.reply_to(message, "❌ Спочатку зупиніть бот командою /stop")
        return
    
    markup = types.InlineKeyboardMarkup()
    btn1 = types.InlineKeyboardButton("✅ ТАК, очистити", callback_data="clear_yes")
    btn2 = types.InlineKeyboardButton("❌ НІ, скасувати", callback_data="clear_no")
    markup.add(btn1, btn2)
    
    bot.reply_to(message, "⚠️ *ВИ ВПЕВНЕНІ?*\nЦе безповоротно видалить ВСЮ історію угод та стани EMA!", 
                reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data == "clear_yes":
        db.clear_all_data()
        bot.edit_message_text("✅ Базу даних очищено!", 
                            call.message.chat.id, 
                            call.message.message_id)
    elif call.data == "clear_no":
        bot.edit_message_text("❌ Скасовано", 
                            call.message.chat.id, 
                            call.message.message_id)
@bot.message_handler(commands=['crosshistory'])
def crosshistory_cmd(message):
    """Показує історію перетинів EMA 20/50 за останні 7 днів (або 48 годин)"""
    try:
        msg = "📜 *ІСТОРІЯ ПЕРЕТИНІВ EMA 20/50 (7 днів)*\n\n"
        
        for symbol in config.SYMBOLS:
            kucoin_symbol = symbol.replace('USDT', '-USDT')
            
            # Беремо 2000 свічок (≈7 днів) для достатньої історії
            end_time = int(time.time())
            start_time = end_time - 7*24*3600  # 7 днів тому
            klines = client.get_kline(
                symbol=kucoin_symbol,
                kline_type='5min',
                start_at=start_time,
                end_at=end_time
            )
            
            if not klines or len(klines) < 200:  # мінімум 200 свічок для стабільності
                msg += f"*{symbol}* – недостатньо даних\n\n"
                continue
            
            # Отримуємо ціни закриття
            closes = [float(k[2]) for k in klines]
            
            # Розраховуємо EMA з min_periods, щоб уникнути спотворень
            df = pd.DataFrame(closes, columns=['close'])
            df['ema20'] = df['close'].ewm(span=20, adjust=False, min_periods=20).mean()
            df['ema50'] = df['close'].ewm(span=50, adjust=False, min_periods=50).mean()
            
            # Визначаємо стан тільки там, де обидва EMA не NaN
            df['state'] = (df['ema20'] > df['ema50']) & df['ema20'].notna() & df['ema50'].notna()
            
            # Шукаємо перетини
            crosses = []
            for i in range(1, len(df)):
                if pd.notna(df['ema20'].iloc[i]) and pd.notna(df['ema50'].iloc[i]) and \
                   pd.notna(df['ema20'].iloc[i-1]) and pd.notna(df['ema50'].iloc[i-1]):
                    if df['state'].iloc[i] != df['state'].iloc[i-1]:
                        # Час закриття свічки
                        close_time = int(klines[i][0]) + 300
                        # Конвертуємо в локальний (Київ UTC+2)
                        local_time = close_time + 7200
                        time_str = datetime.fromtimestamp(local_time).strftime('%H:%M %d.%m')
                        signal = 'LONG' if df['state'].iloc[i] else 'SHORT'
                        price = df['close'].iloc[i]
                        crosses.append(f"{time_str} - {signal} @ ${price:.2f}")
            
            msg += f"*{symbol}*\n"
            if crosses:
                # Показуємо останні 10 перетинів
                for cross in crosses[-10:]:
                    msg += f"   {cross}\n"
            else:
                msg += "   За 7 днів перетинів не виявлено\n"
            msg += "\n"
        
        bot.reply_to(message, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка: {e}")
        
@bot.message_handler(commands=['emastatus'])
def emastatus_cmd(message):
    """Показує поточний стан EMA з історією"""
    try:
        msg = "📊 *СТАН EMA 20/50 (поточний)*\n\n"
        
        for symbol in config.SYMBOLS:
            kucoin_symbol = symbol.replace('USDT', '-USDT')
            
            # Беремо 100 свічок
            klines = client.get_kline(
                symbol=kucoin_symbol,
                kline_type='5min',
                start_at=int(time.time()) - 500*60,
                end_at=int(time.time())
            )
            
            if not klines or len(klines) < 60:
                continue
            
            closes = [float(k[2]) for k in klines[-60:]]
            df = pd.DataFrame(closes, columns=['close'])
            df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
            df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
            
            current_ema20 = df['ema20'].iloc[-1]
            current_ema50 = df['ema50'].iloc[-1]
            current_price = df['close'].iloc[-1]
            
            # Форматуємо числа
            if current_price < 1:
                price_fmt = ".4f"
                ema_fmt = ".4f"
            elif current_price < 10:
                price_fmt = ".3f"
                ema_fmt = ".3f"
            else:
                price_fmt = ".2f"
                ema_fmt = ".2f"
            
            state = "🟢 LONG" if current_ema20 > current_ema50 else "🔴 SHORT"
            diff = current_ema20 - current_ema50
            
            # Дивимось чи був перетин за останні 3 свічки
            last_states = df['ema20'].iloc[-3:] > df['ema50'].iloc[-3:]
            recent_cross = "⚠️ Щойно!" if last_states.iloc[-1] != last_states.iloc[-2] else ""
            
            msg += (f"*{symbol}*\n"
                   f"   Стан: {state} {recent_cross}\n"
                   f"   Ціна: ${current_price:{price_fmt}}\n"
                   f"   EMA20: ${current_ema20:{ema_fmt}}\n"
                   f"   EMA50: ${current_ema50:{ema_fmt}}\n"
                   f"   Різниця: {diff:+.2f}\n\n")
        
        bot.reply_to(message, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Помилка: {e}")

@bot.message_handler(commands=['menu'])
def menu_cmd(message):
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    buttons = [
        types.KeyboardButton('/price'),
        types.KeyboardButton('/status'),
        types.KeyboardButton('/history'),
        types.KeyboardButton('/stats'),
        types.KeyboardButton('/maxprofits'),
        types.KeyboardButton('/maxlosses'),
        types.KeyboardButton('/records'),
        types.KeyboardButton('/daily'),
        types.KeyboardButton('/hourly'),
        types.KeyboardButton('/weekly'),
        types.KeyboardButton('/monthly'),
        types.KeyboardButton('/analyze'),
        types.KeyboardButton('/cleardb'),
        types.KeyboardButton('/start'),
        types.KeyboardButton('/stop'),
        types.KeyboardButton('/emastatus'),
        types.KeyboardButton('/crosshistory'),
        types.KeyboardButton('/menu')
    ]
    markup.add(*buttons)
    
    bot.send_message(message.chat.id, "📱 *Меню керування*\n\nВиберіть команду:", 
                    reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    text = message.text
    if text == '/price':
        price_cmd(message)
    elif text == '/status':
        status_cmd(message)
    elif text == '/history':
        history_cmd(message)
    elif text == '/stats':
        stats_cmd(message)
    elif text == '/maxprofits':
        maxprofits_cmd(message)
    elif text == '/maxlosses':
        maxlosses_cmd(message)
    elif text == '/records':
        records_cmd(message)
    elif text == '/daily':
        daily_cmd(message)
    elif text == '/hourly':
        hourly_cmd(message)
    elif text == '/weekly':
        weekly_cmd(message)
    elif text == '/monthly':
        monthly_cmd(message)
    elif text == '/analyze':
        analyze_cmd(message)
    elif text == '/cleardb':
        cleardb_cmd(message)
    elif text == '/start':
        start_cmd(message)
    elif text == '/stop':
        stop_cmd(message)
    elif text == '/menu':
        menu_cmd(message)

if __name__ == '__main__':
    try:
        print("🤖 Telegram Scalper Bot (KuCoin) запущено...")
        print(f"Моніторинг пар: {config.SYMBOLS}")
        print(f"EMA 20/50 на 5хв графіку")
        print(f"🆔 Bot ID: {BOT_ID}")
        print(f"📊 Трейлінг-стоп: ВИМКНЕНО")
        if hasattr(config, 'CHANNEL_ID') and config.CHANNEL_ID:
            print(f"📤 Канал підключено: {config.CHANNEL_ID}")
        else:
            print("⚠️ Канал не налаштовано")
        print("Команди: /menu - відкрити меню")
        
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
        
    except Exception as e:
        print(f"❌ Помилка: {e}")
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        db.close()
        print("👋 Бот завершив роботу")