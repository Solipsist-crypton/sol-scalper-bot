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
        # 🎯 Для трейлінг-стопу
        self.max_pnl = 0.0
        self.trailing_stop = None
        self.trailing_activated = False

class ScalperBot:
    def __init__(self):
        self.positions = {}
        self.last_state = {}
        self.running = True
        self.last_signal = {}
        self.last_trade_time = {}
        # 🎯 Налаштування трейлінг-стопу
        self.check_interval = 1  # Перевірка КОЖНУ СЕКУНДУ для швидкої реакції
        self.fix_percent = 0.7  # Фіксуємо 70% від максимуму
    
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
        ema_fast, ema_slow, price = self.get_emas(symbol)
        if not ema_fast:
            return None, None, None
        
        current_state = 'ABOVE' if ema_fast > ema_slow else 'BELOW'
        current_time = time.time()
        
        if symbol not in self.last_state:
            self.last_state[symbol] = current_state
            print(f"📊 {symbol}: початковий стан {current_state}")
            return None, None, price
        
        if current_state != self.last_state[symbol]:
            signal = 'LONG' if current_state == 'ABOVE' else 'SHORT'
            
            # Захист від дублікатів
            if symbol in self.last_signal:
                last_signal_type = self.last_signal[symbol]['type']
                last_signal_time = self.last_signal[symbol]['time']
                if signal == last_signal_type and (current_time - last_signal_time) < 30:
                    print(f"⏱️ {symbol}: ігноруємо дублікат {signal}")
                    return None, None, price
            
            self.last_signal[symbol] = {'type': signal, 'time': current_time}
            self.last_state[symbol] = current_state
            print(f"🔥 {symbol}: СИГНАЛ {signal} (ціна: {price})")
            return signal, current_state, price
        
        return None, None, price
    
    def check_trailing_stop(self, symbol, current_price):
        """Перевіряє трейлінг-стоп з фіксацією 70% і швидкою реакцією"""
        if symbol not in self.positions:
            return False

        pos = self.positions[symbol]

        if pos.side == 'LONG':
            current_pnl = ((current_price - pos.entry_price) / pos.entry_price) * 100
        else:
            current_pnl = ((pos.entry_price - current_price) / pos.entry_price) * 100

        # Оновлюємо максимальний PnL
        if current_pnl > pos.max_pnl:
            pos.max_pnl = current_pnl
            print(f"📈 {symbol}: новий максимум {pos.max_pnl:.2f}%")
            
            # 🛡️ ФІКСУЄМО 70% ВІД МАКСИМУМУ
            if pos.max_pnl >= 0.1:  # Навіть маленький профіт
                fix_level = pos.max_pnl * self.fix_percent
                pos.trailing_activated = True
                pos.trailing_stop = fix_level
                print(f"🎯 {symbol}: фіксація {self.fix_percent*100}% на {pos.trailing_stop:.2f}%")

        # 🚨 ШВИДКИЙ ТРЕЙЛІНГ - перевіряємо чи профіт впав нижче 70% від максимуму
        if pos.trailing_activated:
            if current_pnl <= pos.max_pnl * self.fix_percent:
                print(f"🔥 {symbol}: швидка фіксація! {current_pnl:.2f}% ≤ {self.fix_percent*100}% від {pos.max_pnl:.2f}%")
                return True

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
            
            # 🔥 Рахуємо максимальний профіт за угоду
            max_price = 0
            min_price = float('inf')
            
            try:
                kucoin_symbol = self.convert_symbol(symbol)
                klines = client.get_kline(
                    symbol=kucoin_symbol,
                    kline_type='1min',
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
            
            # Додаємо інформацію про фіксацію
            fix_info = ""
            if reason == "trailing" and 'max_pnl' in locals():
                fix_info = f"🔒 Фіксація {int(self.fix_percent*100)}%: {pos.max_pnl * self.fix_percent:.2f}%\n"
            
            trade_info = {
                'symbol': symbol,
                'side': pos.side,
                'entry': round(pos.entry_price, 2),
                'exit': round(exit_price, 2),
                'pnl': round(pos.pnl_percent, 2),
                'max_pnl': round(max_pnl, 2),
                'fix_percent': self.fix_percent,
                'fix_level': round(pos.max_pnl * self.fix_percent, 2) if pos.max_pnl > 0 else 0,
                'hold_minutes': round(hold_minutes, 1),
                'entry_time': datetime.fromtimestamp(pos.entry_time).strftime('%H:%M:%S'),
                'exit_time': datetime.fromtimestamp(exit_time).strftime('%H:%M:%S'),
                'exit_reason': reason
            }
            
            # Зберігаємо в БД
            db.add_trade(trade_info)
            
            # 📤 Відправляємо в канал (якщо налаштовано)
            self.send_to_channel(trade_info)
            
            # Відправляємо результат
            self.send_trade_result(trade_info, reason)
            
            del self.positions[symbol]
            return trade_info
        return None
    
    def open_position(self, symbol, side, price, current_time):
        self.positions[symbol] = Position(symbol, side, price, current_time)
        self.last_trade_time[symbol] = current_time
        
        msg = (f"🆓 *НОВА ПОЗИЦІЯ*\n"
               f"Монета: {symbol}\n"
               f"Напрямок: {'🟢 LONG' if side == 'LONG' else '🔴 SHORT'}\n"
               f"Ціна входу: ${round(price, 2)}\n"
               f"Час: {datetime.fromtimestamp(current_time).strftime('%H:%M:%S')}")
        bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')
    
    def send_trade_result(self, trade, reason="signal"):
        emoji = '✅' if trade['pnl'] > 0 else '❌'
        reason_emoji = "📊" if reason == "signal" else "🎯"
        reason_text = "сигнал EMA" if reason == "signal" else "трейлінг-стоп"
        
        # Додаємо інформацію про фіксацію
        fix_info = ""
        if reason == "trailing" and 'fix_level' in trade and trade['fix_level'] > 0:
            fix_info = f"🔒 Фіксація {int(trade['fix_percent']*100)}%: {trade['fix_level']:.2f}%\n"
        
        max_profit_line = f"📈 Макс. профіт: {trade['max_pnl']:+.2f}%\n"
        
        msg = (f"{emoji} *РЕЗУЛЬТАТ УГОДИ*\n"
               f"Монета: {trade['symbol']}\n"
               f"Тип: {'🟢 LONG' if trade['side'] == 'LONG' else '🔴 SHORT'}\n"
               f"Вхід: ${trade['entry']} → Вихід: ${trade['exit']}\n"
               f"📊 PnL: *{trade['pnl']:+.2f}%*\n"
               f"{max_profit_line}"
               f"{fix_info}"
               f"{reason_emoji} Причина: {reason_text}\n"
               f"⏱ Час утримання: {trade['hold_minutes']} хв\n"
               f"🕒 {trade['entry_time']} → {trade['exit_time']}")
        bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')
    
    def send_to_channel(self, trade_info):
        """Відправляє угоду в Telegram канал"""
        try:
            # Перевіряємо чи налаштовано канал
            if not hasattr(config, 'CHANNEL_ID') or not config.CHANNEL_ID:
                print("⚠️ CHANNEL_ID не налаштовано, пропускаємо відправку в канал")
                return
            
            emoji = '✅' if trade_info['pnl'] > 0 else '❌'
            reason_emoji = "🎯" if trade_info.get('exit_reason') == 'trailing' else "📊"
            
            # Додаємо інформацію про фіксацію
            fix_info = ""
            if trade_info.get('exit_reason') == 'trailing' and 'fix_level' in trade_info and trade_info['fix_level'] > 0:
                fix_info = f"🔒 Фіксація {int(trade_info['fix_percent']*100)}%: {trade_info['fix_level']:.2f}%\n"
            
            msg = (f"{emoji} *УГОДА*\n"
                   f"Монета: {trade_info['symbol']}\n"
                   f"Тип: {'🟢 LONG' if trade_info['side'] == 'LONG' else '🔴 SHORT'}\n"
                   f"Вхід: ${trade_info['entry']} → Вихід: ${trade_info['exit']}\n"
                   f"📊 PnL: *{trade_info['pnl']:+.2f}%*\n"
                   f"📈 Макс: {trade_info['max_pnl']:+.2f}%\n"
                   f"{fix_info}"
                   f"{reason_emoji} {trade_info.get('exit_reason', 'signal')}\n"
                   f"⏱ {trade_info['hold_minutes']} хв\n"
                   f"🕒 {trade_info['entry_time']} → {trade_info['exit_time']}")
            
            # Використовуємо глобальний bot
            global bot
            bot.send_message(config.CHANNEL_ID, msg, parse_mode='Markdown')
            print(f"📤 Угоду відправлено в канал {config.CHANNEL_ID}")
        except Exception as e:
            print(f"❌ Помилка відправки в канал: {e}")
    
    def monitor_loop(self):
        print("🤖 Моніторинг запущено. Чекаємо на перетин EMA...")
        print(f"🎯 Фіксація {self.fix_percent*100}% від максимуму, перевірка кожні {self.check_interval} сек")
        
        while self.running:
            current_time = time.time()
            
            # Спочатку перевіряємо трейлінг-стопи для всіх відкритих позицій
            for symbol in list(self.positions.keys()):
                try:
                    _, _, current_price = self.get_emas(symbol)
                    if current_price:
                        if self.check_trailing_stop(symbol, current_price):
                            self.close_position(symbol, current_price, current_time, "trailing")
                except Exception as e:
                    print(f"Помилка трейлінгу для {symbol}: {e}")
            
            # Потім перевіряємо сигнали EMA для нових угод
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
            
            time.sleep(self.check_interval)  # Перевірка кожну секунду

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

@bot.message_handler(commands=['price'])
def price_cmd(message):
    try:
        msg = "💰 *Поточні ціни (KuCoin):*\n"
        for symbol in config.SYMBOLS:
            kucoin_symbol = symbol.replace('USDT', '-USDT')
            ticker = client.get_ticker(kucoin_symbol)
            price = float(ticker['price'])
            msg += f"\n{symbol}: ${round(price, 2)}"
        bot.reply_to(message, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Помилка: {e}")

@bot.message_handler(commands=['status'])
def status_cmd(message):
    global scalper_instance
    if scalper_instance and scalper_instance.positions:
        msg = "📊 *Активні позиції:*\n"
        for symbol, pos in scalper_instance.positions.items():
            _, _, current_price = scalper_instance.get_emas(symbol)
            if pos.side == 'LONG':
                pnl = ((current_price - pos.entry_price) / pos.entry_price) * 100
            else:
                pnl = ((pos.entry_price - current_price) / pos.entry_price) * 100
            
            hold_time = (time.time() - pos.entry_time) / 60
            
            # Додаємо інформацію про трейлінг
            trailing_info = ""
            if pos.trailing_activated:
                fix_level = pos.max_pnl * scalper_instance.fix_percent
                trailing_info = f" | фіксація: {fix_level:.2f}%"
            
            msg += (f"\n{symbol}: {'🟢 LONG' if pos.side == 'LONG' else '🔴 SHORT'}\n"
                    f"Вхід: ${round(pos.entry_price, 2)}\n"
                    f"Поточна PnL: {pnl:+.2f}%{trailing_info}\n"
                    f"⏱ {round(hold_time, 1)} хв | 📈 макс: {pos.max_pnl:+.2f}%\n")
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
            msg += (f"{emoji} {trade['symbol']} {trade['side']}\n"
                   f"PnL: {trade['pnl_percent']:+.2f}% | {reason_emoji} {trade.get('exit_reason', 'signal')}\n"
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
            msg += (f"{emoji} *{i}. {trade['symbol']} {trade['side']}*\n"
                   f"   PnL: *{trade['pnl_percent']:+.2f}%*\n"
                   f"   Вхід: ${trade['entry_price']} → Вихід: ${trade['exit_price']}\n"
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
            msg += (f"{emoji} *{i}. {trade['symbol']} {trade['side']}*\n"
                   f"   PnL: *{trade['pnl_percent']:+.2f}%*\n"
                   f"   Вхід: ${trade['entry_price']} → Вихід: ${trade['exit_price']}\n"
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
                msg += f"🏆 *Найбільший прибуток:*\n"
                msg += f"   {record['symbol']} {record['side']}: +{record['value']:.2f}%\n"
                msg += f"   Вхід: ${record['entry_price']} → Вихід: ${record['exit_price']}\n"
                msg += f"   {record['entry_time']} → {record['exit_time']}\n\n"
            elif record['record_type'] == 'MAX_LOSS':
                msg += f"💔 *Найбільший збиток:*\n"
                msg += f"   {record['symbol']} {record['side']}: {record['value']:.2f}%\n"
                msg += f"   Вхід: ${record['entry_price']} → Вихід: ${record['exit_price']}\n"
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
    
    bot.reply_to(message, "⚠️ *ВИ ВПЕВНЕНІ?*\nЦе безповоротно видалить ВСЮ історію угод!", 
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
        print(f"EMA {config.EMA_FAST}/{config.EMA_SLOW} на {config.INTERVAL}")
        print(f"🆔 Bot ID: {BOT_ID}")
        print(f"🎯 Фіксація 70% від максимуму, перевірка кожну секунду")
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