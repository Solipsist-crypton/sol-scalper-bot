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

BOT_ID = str(uuid.uuid4())[:8]
print(f"🆔 Запуск бота з трейлінгом (ID: {BOT_ID})")

LOCK_FILE = '/tmp/bot_trailing.lock'
PID_FILE = '/tmp/bot_trailing.pid'

def check_single_instance():
    if os.path.exists(LOCK_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = f.read().strip()
            print(f"⚠️ Бот вже запущений з PID {old_pid}")
            os.system("pkill -f 'python.*scalper_bot_trailing.py' || true")
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

# 🟢 Використовуємо TELEGRAM_TOKEN (той самий або інший)
bot = telebot.TeleBot(config.TELEGRAM_TOKEN)

client = Market(
    key=config.EXCHANGE_API_KEY,
    secret=config.EXCHANGE_API_SECRET,
    passphrase=config.EXCHANGE_API_PASSPHRASE
)

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
        self.check_interval = 5
        self.fix_percent = 0.7
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
    
    def check_trailing_stop(self, symbol, current_price):
        if symbol not in self.positions:
            return False
        pos = self.positions[symbol]
        if pos.side == 'LONG':
            current_pnl = ((current_price - pos.entry_price) / pos.entry_price) * 100
        else:
            current_pnl = ((pos.entry_price - current_price) / pos.entry_price) * 100
        if current_pnl > pos.max_pnl:
            pos.max_pnl = current_pnl
            if pos.max_pnl >= 0.1:
                fix_level = pos.max_pnl * self.fix_percent
                pos.trailing_activated = True
                pos.trailing_stop = fix_level
        if pos.trailing_activated and current_pnl <= pos.trailing_stop:
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
            self.send_to_channel(trade_info)
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
        reason_emoji = "🎯" if reason == "trailing" else "📊"
        reason_text = "трейлінг-стоп" if reason == "trailing" else "сигнал EMA"
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
    
    def send_to_channel(self, trade_info):
        try:
            if not hasattr(config, 'CHANNEL_ID') or not config.CHANNEL_ID:
                return
            if trade_info['entry'] < 1 or trade_info['exit'] < 1:
                price_format = ".4f"
            else:
                price_format = ".2f"
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
            bot.send_message(config.CHANNEL_ID, msg, parse_mode='Markdown')
        except Exception as e:
            print(f"❌ Помилка каналу: {e}")
    
    def monitor_loop(self):
        print("🤖 Моніторинг запущено (З ТРЕЙЛІНГОМ)...")
        while self.running:
            current_time = time.time()
            for symbol in list(self.positions.keys()):
                try:
                    current_price = self.get_real_price(symbol)
                    if current_price and self.check_trailing_stop(symbol, current_price):
                        self.close_position(symbol, current_price, current_time, "trailing")
                except:
                    pass
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
    bot.reply_to(message, "🚀 Бот (З трейлінгом) запущено!")

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
        msg = "📊 *Активні позиції (З трейлінгом):*\n"
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
            trailing_info = f" | фікс: {pos.trailing_stop:.2f}%" if pos.trailing_activated else ""
            msg += (f"\n{symbol}: {'🟢 LONG' if pos.side == 'LONG' else '🔴 SHORT'}\n"
                    f"Вхід: ${entry_str}\n"
                    f"PnL: {pnl:+.2f}%{trailing_info}\n"
                    f"📈 макс: {pos.max_pnl:+.2f}% | ⏱ {hold_time:.1f} хв\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає активних позицій")

@bot.message_handler(commands=['price'])
def price_cmd(message):
    try:
        msg = "💰 *Поточні ціни:*\n"
        for symbol in config.SYMBOLS:
            price = scalper_instance.get_real_price(symbol) if scalper_instance else None
            if price:
                if price < 1:
                    price_str = f"{price:.4f}"
                elif price < 10:
                    price_str = f"{price:.3f}"
                else:
                    price_str = f"{price:.2f}"
                msg += f"\n{symbol}: ${price_str}"
        bot.reply_to(message, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Помилка: {e}")

@bot.message_handler(commands=['history'])
def history_cmd(message):
    trades = db.get_trades(limit=10)
    if len(trades) > 0:
        msg = "📜 *Останні 10 угод:*\n\n"
        for _, trade in trades.iterrows():
            emoji = '✅' if trade['pnl_percent'] > 0 else '❌'
            reason_emoji = "🎯" if trade.get('exit_reason') == 'trailing' else "📊"
            if trade['entry_price'] < 1 or trade['exit_price'] < 1:
                entry_str = f"{trade['entry_price']:.4f}"
                exit_str = f"{trade['exit_price']:.4f}"
            else:
                entry_str = f"{trade['entry_price']:.2f}"
                exit_str = f"{trade['exit_price']:.2f}"
            msg += (f"{emoji} {trade['symbol']} {trade['side']}\n"
                   f"PnL: {trade['pnl_percent']:+.2f}% | {reason_emoji}\n"
                   f"${entry_str} → ${exit_str}\n"
                   f"{trade['entry_time']} → {trade['exit_time']}\n\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Історія угод порожня")


@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    """Загальна статистика"""
    analysis = db.get_detailed_analysis()
    if not analysis:
        bot.reply_to(message, "Немає даних для статистики")
        return
    
    msg = "📊 *ЗАГАЛЬНА СТАТИСТИКА*\n\n"
    msg += f"📈 Всього угод: {analysis['total_trades']}\n"
    msg += f"✅ Прибуткових: {analysis['wins']}\n"
    msg += f"❌ Збиткових: {analysis['losses']}\n"
    msg += f"🎯 Вінрейт: {analysis['winrate']:.1f}%\n"
    msg += f"💰 Заг. PnL: {analysis['total_pnl']:+.2f}%\n"
    msg += f"📊 Сер. PnL: {analysis['avg_pnl']:+.2f}%\n"
    msg += f"🏆 Краща: {analysis['best_trade']:+.2f}%\n"
    msg += f"💔 Гірша: {analysis['worst_trade']:+.2f}%\n"
    msg += f"⏱ Сер. час: {analysis['avg_hold']:.1f} хв"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['maxprofits'])
def maxprofits_cmd(message):
    """Топ прибутків"""
    max_profits = db.get_max_profits(limit=10)
    if len(max_profits) > 0:
        msg = "🏆 *ТОП-10 ПРИБУТКІВ*\n\n"
        for i, (_, trade) in enumerate(max_profits.iterrows(), 1):
            emoji = '🥇' if i == 1 else '🥈' if i == 2 else '🥉' if i == 3 else '📈'
            msg += f"{emoji} *{i}. {trade['symbol']} {trade['side']}*\n"
            msg += f"   PnL: *{trade['pnl_percent']:+.2f}%*\n"
            msg += f"   {trade['entry_time']} → {trade['exit_time']}\n\n"
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає даних")

@bot.message_handler(commands=['maxlosses'])
def maxlosses_cmd(message):
    """Топ збитків"""
    max_losses = db.get_max_losses(limit=10)
    if len(max_losses) > 0:
        msg = "💔 *ТОП-10 ЗБИТКІВ*\n\n"
        for i, (_, trade) in enumerate(max_losses.iterrows(), 1):
            emoji = '💀' if i == 1 else '😱' if i == 2 else '😭' if i == 3 else '📉'
            msg += f"{emoji} *{i}. {trade['symbol']} {trade['side']}*\n"
            msg += f"   PnL: *{trade['pnl_percent']:+.2f}%*\n"
            msg += f"   {trade['entry_time']} → {trade['exit_time']}\n\n"
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає даних")

@bot.message_handler(commands=['daily'])
def daily_cmd(message):
    """Денна статистика"""
    daily = db.get_daily_stats(days=7)
    if len(daily) > 0:
        msg = "📅 *ОСТАННІ 7 ДНІВ*\n\n"
        for _, day in daily.iterrows():
            winrate = (day['wins'] / day['total_trades'] * 100) if day['total_trades'] > 0 else 0
            msg += (f"*{day['date']} - {day['symbol']}*\n"
                   f"📊 Угод: {day['total_trades']} | PnL: {day['total_pnl']:+.2f}%\n"
                   f"✅ {day['wins']} | ❌ {day['losses']} | вінрейт: {winrate:.0f}%\n\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає даних")

@bot.message_handler(commands=['hourly'])
def hourly_cmd(message):
    """Годинна статистика"""
    hourly = db.get_hourly_stats()
    if len(hourly) > 0:
        msg = "🕐 *ГОДИННА СТАТИСТИКА*\n\n"
        for _, hour in hourly.iterrows():
            if hour['total_trades'] >= 3:
                msg += (f"*{hour['hour']:02d}:00*\n"
                       f"📊 Угод: {hour['total_trades']} | PnL: {hour['avg_pnl']:+.2f}%\n"
                       f"🎯 Вінрейт: {hour['winrate']}%\n\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає даних")

@bot.message_handler(commands=['analyze'])
def analyze_cmd(message):
    """Детальний аналіз стратегії"""
    analysis = db.get_detailed_analysis()
    if not analysis:
        bot.reply_to(message, "Немає даних для аналізу")
        return
    
    msg = "📊 *ДЕТАЛЬНИЙ АНАЛІЗ СТРАТЕГІЇ*\n\n"
    
    # Загальна статистика
    msg += f"*ЗАГАЛЬНЕ*\n"
    msg += f"📈 Угод: {analysis['total_trades']}\n"
    msg += f"💰 Заг. PnL: {analysis['total_pnl']:+.2f}%\n"
    msg += f"🎯 Вінрейт: {analysis['winrate']:.1f}%\n"
    msg += f"📊 Профіт фактор: {analysis['profit_factor']:.2f}\n\n"
    
    # Рекорди
    if analysis['records']:
        msg += f"*РЕКОРДИ*\n"
        for record in analysis['records']:
            if record['record_type'] == 'MAX_PROFIT':
                msg += f"🏆 Max прибуток: +{record['value']:.2f}%\n"
            else:
                msg += f"💔 Max збиток: {record['value']:.2f}%\n"
        msg += "\n"
    
    # Аналіз по годинах
    msg += f"*НАЙКРАЩІ ГОДИНИ*\n"
    for hour, stats in analysis['by_hour'].iterrows():
        if stats[('pnl_percent', 'count')] >= 3:
            if stats[('pnl_percent', 'mean')] > 0:
                msg += (f"🕐 {hour:02d}:00 | "
                       f"PnL: {stats[('pnl_percent', 'mean')]:+.2f}% | "
                       f"угод: {int(stats[('pnl_percent', 'count')])}\n")
    msg += "\n"
    
    # Аналіз по днях
    msg += f"*НАЙКРАЩІ ДНІ*\n"
    days = ['Пон', 'Вів', 'Сер', 'Чет', 'Пят', 'Суб', 'Нед']
    for day, stats in analysis['by_day'].iterrows():
        if stats[('pnl_percent', 'count')] >= 3:
            if stats[('pnl_percent', 'mean')] > 0:
                msg += (f"📅 {days[day]} | "
                       f"PnL: {stats[('pnl_percent', 'mean')]:+.2f}% | "
                       f"угод: {int(stats[('pnl_percent', 'count')])}\n")
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['menu'])
def menu_cmd(message):
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    buttons = [
        types.KeyboardButton('/start'),
        types.KeyboardButton('/stop'),
        types.KeyboardButton('/status'),
        types.KeyboardButton('/price'),
        types.KeyboardButton('/history'),
        types.KeyboardButton('/stats'),
        types.KeyboardButton('/analyze'),  # 👈 ДОДАЙ ЦЕ
        types.KeyboardButton('/maxprofits'),
        types.KeyboardButton('/maxlosses'),
        types.KeyboardButton('/daily'),
        types.KeyboardButton('/hourly'),
        types.KeyboardButton('/menu')
    ]
    markup.add(*buttons)
    bot.send_message(message.chat.id, "📱 *Меню бота (З трейлінгом)*", 
                    reply_markup=markup, parse_mode='Markdown')
if __name__ == '__main__':
    try:
        print("🤖 Telegram Scalper Bot (З ТРЕЙЛІНГОМ) запущено...")
        print(f"Моніторинг: {config.SYMBOLS}")
        print(f"EMA 20/50 на 5хв | Трейлінг 70%")
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except Exception as e:
        print(f"❌ Помилка: {e}")
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        db.close()