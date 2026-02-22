#!/usr/bin/env python3
import telebot
from telebot import types
from kucoin.client import Market
import pandas as pd
import numpy as np
import time
import threading
from datetime import datetime
import config
import sqlite3

# ===== DATABASE (Оновлена для стратегій) =====
class StatsDB:
    def __init__(self):
        self.conn = sqlite3.connect("trading_stats.db", check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS trades 
                          (symbol TEXT, strategy TEXT, side TEXT, pnl REAL, exit_time TIMESTAMP)''')
        self.conn.commit()

    def save_trade(self, symbol, strategy, side, pnl):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO trades (symbol, strategy, side, pnl, exit_time) VALUES (?, ?, ?, ?, ?)", 
                       (symbol, strategy, side, pnl, datetime.now()))
        self.conn.commit()

    def get_report(self):
        cursor = self.conn.cursor()
        # Статистика по кожній стратегії окремо
        cursor.execute("SELECT strategy, SUM(pnl), COUNT(*) FROM trades GROUP BY strategy")
        return cursor.fetchall()

db = StatsDB()

# ===== BOT SETUP =====
bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market(key=config.EXCHANGE_API_KEY, secret=config.EXCHANGE_API_SECRET, passphrase=config.EXCHANGE_API_PASSPHRASE)

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT', 'ADAUSDT', 'NEARUSDT', 'BCHUSDT', 'LTCUSDT', 'XRPUSDT']

class Position:
    def __init__(self, symbol, strategy, side, price, sl):
        self.symbol = symbol
        self.strategy = strategy
        self.side = side
        self.entry_price = price
        self.stop_loss = sl
        self.max_p = price
        self.min_p = price
        self.trailing_active = False

class MultiStrategyBot:
    def __init__(self):
        self.positions = {}
        self.init_handlers()
        threading.Thread(target=self.run, daemon=True).start()

    def get_data(self, symbol):
        try:
            k = client.get_kline(symbol=symbol.replace('USDT', '-USDT'), kline_type='5min', limit=100)
            df = pd.DataFrame(k, columns=['time','open','close','high','low','vol','amt']).astype(float).sort_values('time')
            df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
            df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
            df['avg_vol'] = df['vol'].rolling(20).mean()
            df['max_high'] = df['high'].rolling(50).max().shift(1) # Для пробоїв
            df['min_low'] = df['low'].rolling(50).min().shift(1)
            return df
        except: return None

    def check_signals(self):
        for symbol in SYMBOLS:
            if symbol in self.positions: continue
            df = self.get_data(symbol)
            if df is None or len(df) < 60: continue
            
            curr = df.iloc[-1]
            prev = df.iloc[-2]
            vol_ok = curr['vol'] > curr['avg_vol'] * 1.3
            
            # --- 1. STRATEGY: BOUNCE (Відскок від EMA 20) ---
            if curr['ema20'] > curr['ema50'] and curr['low'] <= curr['ema20'] and curr['close'] > curr['ema20'] and vol_ok:
                self.open_pos(symbol, "BOUNCE", "LONG", curr['close'], curr['low'] * 0.995)
                continue

            # --- 2. STRATEGY: BREAKOUT (Пробій рівня) ---
            if curr['close'] > curr['max_high'] and vol_ok:
                self.open_pos(symbol, "BREAKOUT", "LONG", curr['close'], curr['close'] * 0.99)
                continue
            elif curr['close'] < curr['min_low'] and vol_ok:
                self.open_pos(symbol, "BREAKOUT", "SHORT", curr['close'], curr['close'] * 1.01)
                continue

            # --- 3. STRATEGY: PATTERN (Поглинання) ---
            # Буча поглинання: поточна зелена свічка перекриває попередню червону
            if curr['close'] > prev['open'] and curr['open'] < prev['close'] and prev['close'] < prev['open'] and vol_ok:
                self.open_pos(symbol, "PATTERN", "LONG", curr['close'], curr['low'] * 0.995)

            time.sleep(0.1)

    def open_pos(self, symbol, strategy, side, price, sl):
        self.positions[symbol] = Position(symbol, strategy, side, price, sl)
        bot.send_message(config.CHAT_ID, f"🚀 *{strategy} {side}*\n#{symbol} | Ціна: `{price}`")

    def monitor_positions(self):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            df = self.get_data(symbol)
            if df is None: continue
            price = df.iloc[-1]['close']
            
            pnl = ((price - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else ((pos.entry_price - price) / pos.entry_price * 100)

            # Простий трейлінг (активація при +0.5%)
            if pnl > 0.5: pos.trailing_active = True
            if pos.trailing_active:
                if pos.side == 'LONG':
                    new_sl = price * 0.996
                    if new_sl > pos.stop_loss: pos.stop_loss = new_sl
                else:
                    new_sl = price * 1.004
                    if new_sl < pos.stop_loss: pos.stop_loss = new_sl

            # Умова виходу
            exit_long = pos.side == 'LONG' and price <= pos.stop_loss
            exit_short = pos.side == 'SHORT' and price >= pos.stop_loss
            
            if exit_long or exit_short:
                db.save_trade(symbol, pos.strategy, pos.side, pnl - 0.12)
                self.positions.pop(symbol)
                bot.send_message(config.CHAT_ID, f"🏁 *ЗАКРИТО ({pos.strategy})*\n#{symbol} | PnL: `{pnl-0.12:+.2f}%`")

    def init_handlers(self):
        @bot.message_handler(commands=['status'])
        def status_cmd(m):
            if not self.positions: return bot.reply_to(m, "Угод немає.")
            msg = "📊 *ПОТОЧНІ УГОДИ:*\n"
            for s, p in self.positions.items():
                msg += f"\n#{s} | *{p.strategy}* | `{p.side}`"
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['report'])
        def report_cmd(m):
            stats = db.get_report()
            msg = "📈 *АНАЛІТИКА СТРАТЕГІЙ:*\n"
            for strat, pnl, count in stats:
                msg += f"\n• *{strat}*: `{pnl:+.2f}%` ({count} угод)"
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

    def run(self):
        while True:
            try:
                self.monitor_positions()
                self.check_signals()
                time.sleep(15)
            except: time.sleep(15)

if __name__ == '__main__':
    print("🚀 Sniper V4.0 Multi-Strategy запущен...")
    MultiStrategyBot()
    bot.infinity_polling()