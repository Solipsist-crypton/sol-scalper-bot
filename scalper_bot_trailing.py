#!/usr/bin/env python3
import telebot
from telebot import types
from kucoin.client import Market
import pandas as pd
import time
import threading
from datetime import datetime
import config  # Має містити TOKEN, CHAT_ID та KuCoin API Keys
import sqlite3
import os

# ===== БАЗА ДАНИХ ДЛЯ АНАЛІТИКИ =====
class StatsDB:
    def __init__(self):
        self.conn = sqlite3.connect("trading_stats.db", check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS trades 
                          (symbol TEXT, side TEXT, pnl REAL, exit_time TIMESTAMP, exit_reason TEXT)''')
        self.conn.commit()

    def save_trade(self, symbol, side, pnl, reason):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO trades VALUES (?, ?, ?, ?, ?)", 
                       (symbol, side, pnl, datetime.now(), reason))
        self.conn.commit()

    def get_hourly_report(self):
        cursor = self.conn.cursor()
        cursor.execute('''SELECT strftime('%H', exit_time) as hr, SUM(pnl), COUNT(*) 
                          FROM trades GROUP BY hr ORDER BY hr''')
        return cursor.fetchall()

    def get_daily_report(self):
        cursor = self.conn.cursor()
        cursor.execute('''SELECT date(exit_time) as dt, SUM(pnl) 
                          FROM trades GROUP BY dt ORDER BY dt DESC LIMIT 7''')
        return cursor.fetchall()

db = StatsDB()

# ===== ІНІЦІАЛІЗАЦІЯ БОТА =====
bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market(
    key=config.EXCHANGE_API_KEY,
    secret=config.EXCHANGE_API_SECRET,
    passphrase=config.EXCHANGE_API_PASSPHRASE
)

# Розширений список монет
SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT', 'ADAUSDT', 'DOTUSDT', 'NEARUSDT',
    'APTUSDT', 'ARBUSDT', 'OPUSDT', 'SUIUSDT', 'TIAUSDT', 'INJUSDT', 'ORDIUSDT', 'FETUSDT',
    'MATICUSDT', 'LTCUSDT', 'BCHUSDT', 'XRPUSDT', 'UNIUSDT', 'AAVEUSDT', 'GALAUSDT'
]

class Position:
    def __init__(self, symbol, side, price, sl):
        self.symbol = symbol
        self.side = side
        self.entry_price = price
        self.stop_loss = sl
        self.max_price = price
        self.min_price = price
        self.entry_time = datetime.now()
        self.trailing_active = False

class ScalperBot:
    def __init__(self):
        self.positions = {}
        self.running = True
        
        # Налаштування
        self.ema_fast = 20
        self.ema_slow = 50
        self.stop_loss_pct = 0.8       # Стоп 0.8%
        self.trailing_activation = 0.5 # Активувати трейлінг при +0.5%
        self.trailing_distance = 0.35  # Відступ трейлінга 0.35%

        # Реєстрація команд у меню
        try:
            bot.set_my_commands([
                types.BotCommand("status", "📊 Поточні позиції та PnL"),
                types.BotCommand("check", "📡 Статус системи"),
                types.BotCommand("report", "📅 Звіт по прибутку")
            ])
        except: pass

        self.init_handlers()
        threading.Thread(target=self.run, daemon=True).start()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()

    def get_data(self, symbol):
        try:
            # Налаштовано на 5 хвилин за твоїм запитом
            k = client.get_kline(symbol=symbol.replace('USDT', '-USDT'), kline_type='5min', limit=100)
            df = pd.DataFrame(k, columns=['time','open','close','high','low','vol','amt']).astype(float).sort_values('time')
            df['f'] = df['close'].ewm(span=self.ema_fast, adjust=False).mean()
            df['s'] = df['close'].ewm(span=self.ema_slow, adjust=False).mean()
            return df
        except: return None

    def check_signals(self):
        for symbol in SYMBOLS:
            if symbol in self.positions: continue
            df = self.get_data(symbol)
            if df is None or len(df) < 55: continue
            
            last, prev = df.iloc[-1], df.iloc[-2]
            
            # Перетин EMA 20/50
            if prev['f'] <= prev['s'] and last['f'] > last['s']: # Cross UP
                sl = last['close'] * (1 - self.stop_loss_pct/100)
                self.positions[symbol] = Position(symbol, 'LONG', last['close'], sl)
                bot.send_message(config.CHAT_ID, f"🚀 *LONG* #{symbol}\nЦіна: `{last['close']}`")
                
            elif prev['f'] >= prev['s'] and last['f'] < last['s']: # Cross DOWN
                sl = last['close'] * (1 + self.stop_loss_pct/100)
                self.positions[symbol] = Position(symbol, 'SHORT', last['close'], sl)
                bot.send_message(config.CHAT_ID, f"🔻 *SHORT* #{symbol}\nЦіна: `{last['close']}`")
            
            time.sleep(0.1)

    def monitor_positions(self):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            df = self.get_data(symbol)
            if df is None: continue
            curr_p = df.iloc[-1]['close']
            
            pnl = ((curr_p - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else ((pos.entry_price - curr_p) / pos.entry_price * 100)

            # --- ЛОГІКА ТРЕЙЛІНГ СТОПУ ---
            if pnl >= self.trailing_activation and not pos.trailing_active:
                pos.trailing_active = True
                bot.send_message(config.CHAT_ID, f"🛡 #{symbol}: Трейлінг-стоп активовано!")
                
            if pos.trailing_active:
                if pos.side == 'LONG':
                    if curr_p > pos.max_price:
                        pos.max_price = curr_p
                        new_sl = curr_p * (1 - self.trailing_distance/100)
                        if new_sl > pos.stop_loss: pos.stop_loss = new_sl
                else:
                    if curr_p < pos.min_price:
                        pos.min_price = curr_p
                        new_sl = curr_p * (1 + self.trailing_distance/100)
                        if new_sl < pos.stop_loss: pos.stop_loss = new_sl

            # --- ПЕРЕВІРКА ВИХОДУ ---
            is_sl = (pos.side == 'LONG' and curr_p <= pos.stop_loss) or (pos.side == 'SHORT' and curr_p >= pos.stop_loss)
            
            if is_sl:
                final_pnl = pnl - 0.15 # Враховуємо комісію KuCoin
                reason = "TRAILING" if pos.trailing_active else "STOP_LOSS"
                db.save_trade(symbol, pos.side, final_pnl, reason)
                self.positions.pop(symbol)
                bot.send_message(config.CHAT_ID, f"{'✅' if final_pnl > 0 else '❌'} *ЗАКРИТО ({reason})*\n#{symbol} | PnL: `{final_pnl:+.2f}%`")

    def init_handlers(self):
        @bot.message_handler(commands=['status'])
        def status_cmd(m):
            if not self.positions: 
                return bot.reply_to(m, "📊 Активних угод немає.")
            
            msg = "📊 *АКТИВНІ ПОЗИЦІЇ:*\n━━━━━━━━━━━━━━━\n"
            for s, p in self.positions.items():
                df = self.get_data(s)
                curr_p = df.iloc[-1]['close'] if df is not None else p.entry_price
                pnl = ((curr_p - p.entry_price) / p.entry_price * 100) if p.side == 'LONG' else ((p.entry_price - curr_p) / p.entry_price * 100)
                
                emoji = "🟢" if pnl >= 0 else "🔴"
                msg += f"{emoji} *#{s}* ({p.side})\n"
                msg += f"├ PnL: *{pnl:+.2f}%*\n"
                msg += f"├ Вхід: `{p.entry_price}` | Зараз: `{curr_p}`\n"
                msg += f"└ SL: `{p.stop_loss:.2f}` | Trail: {'✅' if p.trailing_active else '❌'}\n\n"
            
            bot.send_message(m.chat.id, msg + "━━━━━━━━━━━━━━━", parse_mode='Markdown')

        @bot.message_handler(commands=['check'])
        def check_cmd(m):
            bot.send_message(m.chat.id, f"📡 *Бот онлайн*\nМоніторинг: `{len(SYMBOLS)}` монет\nТаймфрейм: `5min`", parse_mode='Markdown')

        @bot.message_handler(commands=['report'])
        def report_cmd(m):
            daily = db.get_daily_report()
            hourly = db.get_hourly_report()
            
            msg = "📅 *ПРИБУТОК ПО ДНЯХ:*\n"
            msg += "\n".join([f"• {d}: `{p:+.2f}%`" for d, p in daily]) if daily else "Даних ще немає."
            
            msg += "\n\n⏰ *АНАЛІЗ ПО ГОДИНАХ (UTC):*\n"
            msg += "\n".join([f"• {h}h: `{p:+.2f}%` ({c} угод)" for h, p, c in hourly]) if hourly else "Даних ще немає."
            
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

    def heartbeat_loop(self):
        while self.running:
            time.sleep(3600)
            try: bot.send_message(config.CHAT_ID, "🤖 Я працюю. Позицій відкрито: " + str(len(self.positions)))
            except: pass

    def run(self):
        while self.running:
            try:
                self.monitor_positions()
                self.check_signals()
                time.sleep(15)
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(15)

if __name__ == '__main__':
    print("🚀 PRO Scalper запущен...")
    bot_instance = ScalperBot()
    bot.infinity_polling()