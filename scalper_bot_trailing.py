#!/usr/bin/env python3
import telebot
from telebot import types
from kucoin.client import Market
import pandas as pd
import numpy as np
import time
import threading
from datetime import datetime
import config # Файл з TOKEN, CHAT_ID, API_KEY і т.д.
import sqlite3

# ===== DATABASE: Розширена аналітика =====
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

    def get_strategy_report(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT strategy, SUM(pnl), COUNT(*) FROM trades GROUP BY strategy")
        return cursor.fetchall()

    def get_daily_total(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT SUM(pnl) FROM trades WHERE date(exit_time) = date('now')")
        res = cursor.fetchone()
        return res[0] if res[0] else 0.0

db = StatsDB()

# ===== BOT SETUP =====
bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market(key=config.EXCHANGE_API_KEY, secret=config.EXCHANGE_API_SECRET, passphrase=config.EXCHANGE_API_PASSPHRASE)

SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT', 'ADAUSDT', 'DOTUSDT', 'NEARUSDT',
    'APTUSDT', 'ARBUSDT', 'OPUSDT', 'SUIUSDT', 'TIAUSDT', 'INJUSDT', 'ORDIUSDT', 'FETUSDT',
    'MATICUSDT', 'LTCUSDT', 'BCHUSDT', 'XRPUSDT', 'UNIUSDT', 'AAVEUSDT', 'GALAUSDT'
]

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

class ProBotV4:
    def __init__(self):
        self.positions = {}
        # Налаштування
        self.vol_factor = 1.3    # Об'єм на 30% вище середнього
        self.trail_start = 0.55  # Активація трейлінга при +0.55%
        self.trail_step = 0.35   # Відступ трейлінга

        self.init_handlers()
        threading.Thread(target=self.run, daemon=True).start()

    def get_data(self, symbol):
        try:
            k = client.get_kline(symbol=symbol.replace('USDT', '-USDT'), kline_type='5min', limit=100)
            df = pd.DataFrame(k, columns=['time','open','close','high','low','vol','amt']).astype(float).sort_values('time')
            df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
            df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
            df['avg_vol'] = df['vol'].rolling(20).mean()
            df['high_50'] = df['high'].rolling(50).max().shift(1)
            df['low_50'] = df['low'].rolling(50).min().shift(1)
            # RSI
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14).mean()
            loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14).mean()
            df['rsi'] = 100 - (100 / (1 + (gain / loss)))
            return df
        except: return None

    def check_signals(self):
        for symbol in SYMBOLS:
            if symbol in self.positions: continue
            df = self.get_data(symbol)
            if df is None or len(df) < 60: continue
            
            c = df.iloc[-1]  # Поточна
            p = df.iloc[-2]  # Попередня
            vol_ok = c['vol'] > c['avg_vol'] * self.vol_factor
            
            # 1. BOUNCE (Відскок від EMA 20)
            if c['ema20'] > c['ema50'] and c['low'] <= c['ema20'] and c['close'] > c['ema20'] and vol_ok:
                if c['rsi'] < 65:
                    self.open_pos(symbol, "BOUNCE", "LONG", c['close'], c['low'] * 0.994)
                    continue

            # 2. BREAKOUT (Пробій рівня 50 свічок)
            if c['close'] > c['high_50'] and vol_ok and c['rsi'] < 70:
                self.open_pos(symbol, "BREAKOUT", "LONG", c['close'], c['close'] * 0.989)
                continue
            elif c['close'] < c['low_50'] and vol_ok and c['rsi'] > 30:
                self.open_pos(symbol, "BREAKOUT", "SHORT", c['close'], c['close'] * 1.011)
                continue

            # 3. PATTERN (Бичаче/Ведмеже поглинання)
            bullish_eng = c['close'] > p['open'] and c['open'] < p['close'] and p['close'] < p['open']
            if bullish_eng and vol_ok and c['ema20'] > c['ema50']:
                self.open_pos(symbol, "PATTERN", "LONG", c['close'], c['low'] * 0.994)

            time.sleep(0.1)

    def open_pos(self, symbol, strategy, side, price, sl):
        self.positions[symbol] = Position(symbol, strategy, side, price, sl)
        bot.send_message(config.CHAT_ID, f"🆕 *ВХІД: {strategy}*\n#{symbol} | `{side}` | Ціна: `{price}`", parse_mode='Markdown')

    def monitor_positions(self):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            df = self.get_data(symbol)
            if df is None: continue
            curr_p = df.iloc[-1]['close']
            
            pnl = ((curr_p - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else ((pos.entry_price - curr_p) / pos.entry_price * 100)

            # Трейлінг логіка
            if pnl >= self.trail_start: pos.trailing_active = True
            if pos.trailing_active:
                if pos.side == 'LONG':
                    if curr_p > pos.max_p:
                        pos.max_p = curr_p
                        new_sl = curr_p * (1 - self.trail_step/100)
                        if new_sl > pos.stop_loss: pos.stop_loss = new_sl
                else:
                    if curr_p < pos.min_p:
                        pos.min_p = curr_p
                        new_sl = curr_p * (1 + self.trail_step/100)
                        if new_sl < pos.stop_loss: pos.stop_loss = new_sl

            # Вихід
            is_sl = (pos.side == 'LONG' and curr_p <= pos.stop_loss) or (pos.side == 'SHORT' and curr_p >= pos.stop_loss)
            if is_sl:
                final_pnl = pnl - 0.12 # Комісія
                db.save_trade(symbol, pos.strategy, pos.side, final_pnl)
                self.positions.pop(symbol)
                bot.send_message(config.CHAT_ID, f"{'✅' if final_pnl > 0 else '❌'} *ЗАКРИТО: {pos.strategy}*\n#{symbol} | PnL: `{final_pnl:+.2f}%`", parse_mode='Markdown')

    def init_handlers(self):
        @bot.message_handler(commands=['status'])
        def status_cmd(m):
            if not self.positions: return bot.reply_to(m, "📊 Немає активних угод.")
            msg = "📊 *ПОТОЧНИЙ СТАТУС (PnL):*\n━━━━━━━━━━━━━━━\n"
            for s, p in self.positions.items():
                df = self.get_data(s)
                curr_p = df.iloc[-1]['close'] if df is not None else p.entry_price
                pnl = ((curr_p - p.entry_price) / p.entry_price * 100) if p.side == 'LONG' else ((p.entry_price - curr_p) / p.entry_price * 100)
                msg += f"{'🟢' if pnl>0 else '🔴'} *#{s}* | `{p.strategy}`\n"
                msg += f"├ PnL: *{pnl:+.2f}%* | `{p.side}`\n"
                msg += f"└ Вхід: `{p.entry_price}` | SL: `{p.stop_loss:.2f}`\n\n"
            bot.send_message(m.chat.id, msg + "━━━━━━━━━━━━━━━", parse_mode='Markdown')

        @bot.message_handler(commands=['report'])
        def report_cmd(m):
            stats = db.get_strategy_report()
            total = db.get_daily_total()
            msg = f"📈 *АНАЛІТИКА ЗА СЬОГОДНІ:*\nРазом: `{total:+.2f}%` \n━━━━━━━━━━━━━━━\n"
            for strat, pnl, count in stats:
                msg += f"• *{strat}*: `{pnl:+.2f}%` ({count} угод)\n"
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['check'])
        def check_cmd(m):
            bot.send_message(m.chat.id, f"📡 *Бот працює*\nМонет: `{len(SYMBOLS)}` | ТФ: `5min` | Позицій: `{len(self.positions)}`", parse_mode='Markdown')

    def run(self):
        while True:
            try:
                self.monitor_positions()
                self.check_signals()
                time.sleep(15)
            except: time.sleep(15)

if __name__ == '__main__':
    print("🚀 Sniper V4.1 Final запущен...")
    ProBotV4(); bot.infinity_polling()