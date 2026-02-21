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

# ===== БАЗА ДАНИХ =====
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
        cursor.execute("SELECT strftime('%H', exit_time) as hr, SUM(pnl), COUNT(*) FROM trades GROUP BY hr ORDER BY hr")
        return cursor.fetchall()

    def get_daily_report(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT date(exit_time) as dt, SUM(pnl) FROM trades GROUP BY dt ORDER BY dt DESC LIMIT 7")
        return cursor.fetchall()

db = StatsDB()

# ===== ІНІЦІАЛІЗАЦІЯ =====
bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market(key=config.EXCHANGE_API_KEY, secret=config.EXCHANGE_API_SECRET, passphrase=config.EXCHANGE_API_PASSPHRASE)

SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT', 'ADAUSDT', 'DOTUSDT', 'NEARUSDT',
    'APTUSDT', 'ARBUSDT', 'OPUSDT', 'SUIUSDT', 'TIAUSDT', 'INJUSDT', 'ORDIUSDT', 'FETUSDT',
    'MATICUSDT', 'LTCUSDT', 'BCHUSDT', 'XRPUSDT', 'UNIUSDT', 'AAVEUSDT', 'GALAUSDT'
]

class Position:
    def __init__(self, symbol, side, price, sl):
        self.symbol, self.side, self.entry_price = symbol, side, price
        self.stop_loss = sl
        self.max_p, self.min_p = price, price
        self.trailing_active = False

class ScalperBot:
    def __init__(self):
        self.positions = {}
        # --- НАЛАШТУВАННЯ БАЛАНСУ ---
        self.stop_loss_pct = 1.1        # Трохи ширше, щоб не вибило шумом
        self.trailing_activation = 0.55 # Активуємо при +0.55%
        self.trailing_distance = 0.35   # Відступ трейлінга
        self.ad_min = 18                # М'який ADX (для активності)

        try:
            bot.set_my_commands([
                types.BotCommand("status", "📊 PnL та позиції"),
                types.BotCommand("report", "📅 Звіт"),
                types.BotCommand("check", "📡 Стан")
            ])
        except: pass

        self.init_handlers()
        threading.Thread(target=self.run, daemon=True).start()

    def get_data(self, symbol):
        try:
            k = client.get_kline(symbol=symbol.replace('USDT', '-USDT'), kline_type='5min', limit=100)
            df = pd.DataFrame(k, columns=['time','open','close','high','low','vol','amt']).astype(float).sort_values('time')
            
            # EMA 20/50
            df['f'] = df['close'].ewm(span=20, adjust=False).mean()
            df['s'] = df['close'].ewm(span=50, adjust=False).mean()
            
            # RSI
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14).mean()
            loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14).mean()
            df['rsi'] = 100 - (100 / (1 + (gain / loss)))
            
            # ADX (Сила тренду)
            tr = pd.concat([df['high'] - df['low'], abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))], axis=1).max(axis=1)
            atr = tr.ewm(span=14).mean()
            plus_dm = (df['high'].diff()).where(lambda x: (x > df['low'].diff().abs()) & (x > 0), 0).ewm(span=14).mean()
            minus_dm = (df['low'].diff().abs()).where(lambda x: (x > df['high'].diff()) & (x > 0), 0).ewm(span=14).mean()
            df['adx'] = (100 * (abs(plus_dm - minus_dm) / (plus_dm + minus_dm + 1e-10))).ewm(span=14).mean()
            
            return df
        except: return None

    def check_signals(self):
        for symbol in SYMBOLS:
            if symbol in self.positions: continue
            df = self.get_data(symbol)
            if df is None or len(df) < 60: continue
            
            last, prev = df.iloc[-1], df.iloc[-2]
            
            # Фільтр: наявність хоч якогось тренду та розрив між EMA (відступ 0.03%)
            gap = abs(last['f'] - last['s']) / last['s'] * 100
            trend_ok = last['adx'] > self.ad_min and gap > 0.03

            # LONG
            if prev['f'] <= prev['s'] and last['f'] > last['s'] and trend_ok:
                if last['rsi'] < 65:
                    sl = last['close'] * (1 - self.stop_loss_pct/100)
                    self.positions[symbol] = Position(symbol, 'LONG', last['close'], sl)
                    bot.send_message(config.CHAT_ID, f"🎯 *LONG* #{symbol}\nADX: `{last['adx']:.1f}` | RSI: `{last['rsi']:.1f}`")
            
            # SHORT
            elif prev['f'] >= prev['s'] and last['f'] < last['s'] and trend_ok:
                if last['rsi'] > 35:
                    sl = last['close'] * (1 + self.stop_loss_pct/100)
                    self.positions[symbol] = Position(symbol, 'SHORT', last['close'], sl)
                    bot.send_message(config.CHAT_ID, f"🎯 *SHORT* #{symbol}\nADX: `{last['adx']:.1f}` | RSI: `{last['rsi']:.1f}`")
            
            time.sleep(0.1)

    def monitor_positions(self):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            df = self.get_data(symbol)
            if df is None: continue
            curr_p = df.iloc[-1]['close']
            
            pnl = ((curr_p - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else ((pos.entry_price - curr_p) / pos.entry_price * 100)

            if pnl >= self.trailing_activation and not pos.trailing_active:
                pos.trailing_active = True
                
            if pos.trailing_active:
                if pos.side == 'LONG':
                    if curr_p > pos.max_p:
                        pos.max_p = curr_p
                        new_sl = curr_p * (1 - self.trailing_distance/100)
                        if new_sl > pos.stop_loss: pos.stop_loss = new_sl
                else:
                    if curr_p < pos.min_p:
                        pos.min_p = curr_p
                        new_sl = curr_p * (1 + self.trailing_distance/100)
                        if new_sl < pos.stop_loss: pos.stop_loss = new_sl

            is_exit = (pos.side == 'LONG' and curr_p <= pos.stop_loss) or (pos.side == 'SHORT' and curr_p >= pos.stop_loss)
            
            if is_exit:
                final_pnl = pnl - 0.12 # Комісія
                reason = "TRAILING" if pos.trailing_active else "STOP_LOSS"
                db.save_trade(symbol, pos.side, final_pnl, reason)
                self.positions.pop(symbol)
                bot.send_message(config.CHAT_ID, f"{'💎' if final_pnl > 0 else '💀'} *ЗАКРИТО ({reason})*\n#{symbol} | PnL: `{final_pnl:+.2f}%`")

    def init_handlers(self):
        @bot.message_handler(commands=['status'])
        def status_cmd(m):
            if not self.positions: return bot.reply_to(m, "Угод немає. Чекаю сигнал...")
            msg = "📊 *ПОТОЧНИЙ PnL:*\n"
            for s, p in self.positions.items():
                df = self.get_data(s); curr_p = df.iloc[-1]['close'] if df is not None else p.entry_price
                pnl = ((curr_p - p.entry_price) / p.entry_price * 100) if p.side == 'LONG' else ((p.entry_price - curr_p) / p.entry_price * 100)
                msg += f"\n{'🟢' if pnl>0 else '🔴'} *#{s}*: `{pnl:+.2f}%` (Trail: {'✅' if p.trailing_active else '❌'})"
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['report'])
        def report_cmd(m):
            daily = db.get_daily_report(); hourly = db.get_hourly_report()
            msg = "📅 *ПРИБУТОК:*\n" + "\n".join([f"• {d}: `{p:+.2f}%`" for d, p in daily])
            msg += "\n\n⏰ *ГОДИНИ (UTC):*\n" + "\n".join([f"• {h}h: `{p:+.2f}%` ({c} у)" for h, p, c in hourly])
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['check'])
        def check_cmd(m):
            bot.send_message(m.chat.id, f"📡 *STATUS:* OK\nADX Filter: `{self.ad_min}`\nSL: `{self.stop_loss_pct}%`")

    def run(self):
        while self.running:
            try:
                self.monitor_positions(); self.check_signals()
                time.sleep(15)
            except: time.sleep(15)

if __name__ == '__main__':
    print("🚀 Sniper V2.0 запущен...")
    bot_instance = ScalperBot(); bot.infinity_polling()