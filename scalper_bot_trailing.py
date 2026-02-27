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

# ===== DATABASE: Аналітика по входу =====
class StatsDB:
    def __init__(self):
        self.conn = sqlite3.connect("trading_pro.db", check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS trades 
                          (id INTEGER PRIMARY KEY AUTOINCREMENT,
                           symbol TEXT, strategy TEXT, side TEXT, 
                           entry_price REAL, exit_price REAL, 
                           pnl_percent REAL, max_reached REAL,
                           entry_time TIMESTAMP, exit_time TIMESTAMP, 
                           exit_reason TEXT, duration REAL)''')
        self.conn.commit()

    def save_trade(self, t):
        cursor = self.conn.cursor()
        duration = (t['exit_time'] - t['entry_time']).total_seconds() / 60
        cursor.execute('''INSERT INTO trades 
            (symbol, strategy, side, entry_price, exit_price, pnl_percent, 
             max_reached, entry_time, exit_time, exit_reason, duration) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
            (t['symbol'], t['strategy'], t['side'], t['entry_price'], t['exit_price'], 
             t['pnl'], t['max_reached'], t['entry_time'], t['exit_time'], t['reason'], duration))
        self.conn.commit()

    def get_detailed_analysis(self):
        df = pd.read_sql_query("SELECT * FROM trades", self.conn)
        if df.empty: return None
        df['entry_time'] = pd.to_datetime(df['entry_time'])
        df['hour_entry'] = df['entry_time'].dt.hour
        wins = df[df['pnl_percent'] > 0]
        losses = df[df['pnl_percent'] <= 0]
        return {
            'total_trades': len(df),
            'wins': len(wins),
            'losses': len(losses),
            'winrate': (len(wins) / len(df)) * 100 if len(df) > 0 else 0,
            'total_pnl': df['pnl_percent'].sum(),
            'avg_pnl': df['pnl_percent'].mean(),
            'best_trade': df['pnl_percent'].max(),
            'worst_trade': df['pnl_percent'].min(),
            'avg_hold': df['duration'].mean(),
            'by_hour': df.groupby('hour_entry')['pnl_percent'].agg(['sum', 'count'])
        }

db = StatsDB()

# ===== BOT SETUP =====
bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market(key=config.EXCHANGE_API_KEY, secret=config.EXCHANGE_API_SECRET, passphrase=config.EXCHANGE_API_PASSPHRASE)

# Рівно 20 найбільш волатильних та ліквідних монет
SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT', 
    'ADAUSDT', 'NEARUSDT', 'BCHUSDT', 'LTCUSDT', 'XRPUSDT', 
    'APTUSDT', 'TIAUSDT', 'ARBUSDT', 'OPUSDT', 'SUIUSDT', 
    'DOTUSDT', 'INJUSDT', 'FETUSDT', 'MATICUSDT', 'STXUSDT'
]

class Position:
    def __init__(self, symbol, strategy, side, price, sl):
        self.symbol, self.strategy, self.side = symbol, strategy, side
        self.entry_price, self.stop_loss = price, sl
        self.entry_time = datetime.now()
        self.max_pnl_reached = 0.0
        self.trailing_active = False

class ProSniperV5_1:
    def __init__(self):
        self.positions = {}
        self.commission = 0.12
        
        try:
            bot.set_my_commands([
                types.BotCommand("status", "📊 Поточні позиції"),
                types.BotCommand("stats", "📈 Загальна статистика"),
                types.BotCommand("report", "📅 Профіт по годинах входу"),
                types.BotCommand("history", "📜 Останні 10 угод"),
                types.BotCommand("check", "📡 Стан бота")
            ])
        except: pass

        self.init_handlers()
        threading.Thread(target=self.run, daemon=True).start()

    def get_indicators(self, df):
        # Розрахунок ADX (сила тренду)
        plus_dm = df['high'].diff()
        minus_dm = df['low'].diff()
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm > 0] = 0
        tr = pd.concat([df['high'] - df['low'], 
                        abs(df['high'] - df['close'].shift()), 
                        abs(df['low'] - df['close'].shift())], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/14).mean() / atr)
        minus_di = 100 * (abs(minus_dm).ewm(alpha=1/14).mean() / atr)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
        adx = dx.rolling(14).mean()
        return adx.iloc[-1]

    def get_data(self, symbol):
        try:
            k = client.get_kline(symbol=symbol.replace('USDT', '-USDT'), kline_type='5min', limit=100)
            df = pd.DataFrame(k, columns=['time','open','close','high','low','vol','amt']).astype(float).sort_values('time')
            df['high_50'] = df['high'].rolling(50).max().shift(1)
            df['low_50'] = df['low'].rolling(50).min().shift(1)
            df['avg_vol'] = df['vol'].rolling(20).mean()
            return df
        except: return None

    def check_signals(self):
        for symbol in SYMBOLS:
            if symbol in self.positions: continue
            df = self.get_data(symbol)
            if df is None or len(df) < 60: continue
            curr = df.iloc[-1]
            adx_val = self.get_indicators(df)
            
            vol_ok = curr['vol'] > curr['avg_vol'] * 1.5
            trend_ok = adx_val > 22 # Фільтр ADX
            
            if trend_ok and vol_ok:
                if curr['close'] > curr['high_50']:
                    self.open_pos(symbol, "BREAKOUT", "LONG", curr['close'], curr['close'] * 0.988)
                elif curr['close'] < curr['low_50']:
                    self.open_pos(symbol, "BREAKOUT", "SHORT", curr['close'], curr['close'] * 1.012)
            time.sleep(0.1)

    def open_pos(self, symbol, strategy, side, price, sl):
        self.positions[symbol] = Position(symbol, strategy, side, price, sl)
        bot.send_message(config.CHAT_ID, f"⚡️ *ВХІД: BREAKOUT*\n#{symbol} | `{side}` | Ціна: `{price}`", parse_mode='Markdown')

    def monitor_positions(self):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            df = self.get_data(symbol); curr_p = df.iloc[-1]['close'] if df is not None else pos.entry_price
            pnl = ((curr_p - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else ((pos.entry_price - curr_p) / pos.entry_price * 100)
            if pnl > pos.max_pnl_reached: pos.max_pnl_reached = pnl
            if pnl >= 0.6: pos.trailing_active = True
            if pos.trailing_active:
                if pos.side == 'LONG':
                    new_sl = curr_p * 0.996
                    if new_sl > pos.stop_loss: pos.stop_loss = new_sl
                else:
                    new_sl = curr_p * 1.004
                    if new_sl < pos.stop_loss: pos.stop_loss = new_sl
            if (pos.side == 'LONG' and curr_p <= pos.stop_loss) or (pos.side == 'SHORT' and curr_p >= pos.stop_loss):
                t_data = {'symbol': symbol, 'strategy': pos.strategy, 'side': pos.side, 'entry_price': pos.entry_price, 'exit_price': curr_p, 'pnl': pnl, 'max_reached': pos.max_pnl_reached, 'entry_time': pos.entry_time, 'exit_time': datetime.now(), 'reason': 'trailing' if pos.trailing_active else 'stop_loss'}
                db.save_trade(t_data); self.positions.pop(symbol)
                emoji = '✅' if pnl > 0 else '❌'
                bot.send_message(config.CHAT_ID, f"{emoji} *УГОДА ЗАКРИТА*\n#{symbol} | PnL: `{pnl:+.2f}%` (чистий: `{pnl-self.commission:+.2f}%`)\n📈 Макс: `+{pos.max_pnl_reached:.2f}%`", parse_mode='Markdown')

    def init_handlers(self):
        @bot.message_handler(commands=['status'])
        def status_cmd(m):
            if not self.positions: return bot.reply_to(m, "Угод немає.")
            msg = "📊 *ПОТОЧНИЙ СТАТУС:*\n"
            for s, p in self.positions.items():
                df = self.get_data(s); curr = df.iloc[-1]['close'] if df is not None else p.entry_price
                pnl = ((curr - p.entry_price) / p.entry_price * 100) if p.side == 'LONG' else ((p.entry_price - curr) / p.entry_price * 100)
                msg += f"\n{'🟢' if pnl>0 else '🔴'} #{s} | `{pnl:+.2f}%` | Max: `{p.max_pnl_reached:.2f}%`"
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['stats'])
        def stats_cmd(m):
            a = db.get_detailed_analysis()
            if not a: return bot.reply_to(m, "Статистика порожня.")
            msg = (f"📊 *СТАТИСТИКА*\n\n📈 Угод: {a['total_trades']}\n✅ Вінрейт: {a['winrate']:.1f}%\n💰 Заг. PnL: {a['total_pnl']:+.2f}%\n🏆 Краща: {a['best_trade']:+.2f}%")
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['report'])
        def report_cmd(m):
            a = db.get_detailed_analysis()
            if not a: return bot.reply_to(m, "Даних немає.")
            msg = "📅 *СУМАРНИЙ ПРИБУТОК ПО ГОДИНАХ ВХОДУ:*\n\n"
            for hr, row in a['by_hour'].iterrows():
                icon = "➕" if row['sum'] > 0 else "➖"
                msg += f"{icon} {hr:02d}:00 | *{row['sum']:+.2f}%* | `{int(row['count'])} угод`\n"
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['history'])
        def history_cmd(m):
            df = pd.read_sql_query("SELECT * FROM trades ORDER BY exit_time DESC LIMIT 10", db.conn)
            if df.empty: return bot.reply_to(m, "Історія порожня.")
            msg = "📜 *ОСТАННІ 10 УГОД:*\n"
            for _, t in df.iterrows():
                msg += f"\n{'✅' if t['pnl_percent']>0 else '❌'} {t['symbol']} | {t['pnl_percent']:+.2f}%"
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['check'])
        def check_cmd(m):
            bot.send_message(m.chat.id, f"📡 *STATUS:* ACTIVE\nМонет: `{len(SYMBOLS)}` | Позицій: `{len(self.positions)}`")

    def run(self):
        while True:
            try: self.monitor_positions(); self.check_signals(); time.sleep(15)
            except: time.sleep(15)

if __name__ == '__main__':
    ProSniperV5_1(); bot.infinity_polling()