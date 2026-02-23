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

# ===== РОЗШИРЕНА БАЗА ДАНИХ (Твоя аналітика) =====
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
        # Розрахунок тривалості в хвилинах
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
        
        wins = df[df['pnl_percent'] > 0]
        losses = df[df['pnl_percent'] <= 0]
        
        analysis = {
            'total_trades': len(df),
            'wins': len(wins),
            'losses': len(losses),
            'winrate': (len(wins) / len(df)) * 100 if len(df) > 0 else 0,
            'total_pnl': df['pnl_percent'].sum(),
            'avg_pnl': df['pnl_percent'].mean(),
            'best_trade': df['pnl_percent'].max(),
            'worst_trade': df['pnl_percent'].min(),
            'avg_hold': df['duration'].mean(),
            'profit_factor': abs(wins['pnl_percent'].sum() / losses['pnl_percent'].sum()) if not losses.empty else 100
        }
        return analysis

db = StatsDB()

# ===== BOT SETUP =====
bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market(key=config.EXCHANGE_API_KEY, secret=config.EXCHANGE_API_SECRET, passphrase=config.EXCHANGE_API_PASSPHRASE)

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT', 'ADAUSDT', 'NEARUSDT', 'BCHUSDT', 'LTCUSDT', 'XRPUSDT', 'APTUSDT', 'TIAUSDT']

class Position:
    def __init__(self, symbol, strategy, side, price, sl):
        self.symbol = symbol
        self.strategy = strategy
        self.side = side
        self.entry_price = price
        self.stop_loss = sl
        self.entry_time = datetime.now()
        self.max_pnl_reached = 0.0
        self.trailing_active = False

class ProBreakoutBot:
    def __init__(self):
        self.positions = {}
        self.commission = 0.12 # Комісія KuCoin
        
        self.init_handlers()
        threading.Thread(target=self.run, daemon=True).start()

    def get_data(self, symbol):
        try:
            k = client.get_kline(symbol=symbol.replace('USDT', '-USDT'), kline_type='5min', limit=100)
            df = pd.DataFrame(k, columns=['time','open','close','high','low','vol','amt']).astype(float).sort_values('time')
            # Визначаємо локальні рівні за 50 свічок
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
            vol_ok = curr['vol'] > curr['avg_vol'] * 1.5 # Фільтр об'єму 1.5x
            
            # --- ЛОГІКА ПРОБОЮ (BREAKOUT) ---
            if curr['close'] > curr['high_50'] and vol_ok:
                # Вхід в LONG на пробої максимуму
                sl = curr['close'] * 0.988 
                self.open_pos(symbol, "BREAKOUT", "LONG", curr['close'], sl)
            elif curr['close'] < curr['low_50'] and vol_ok:
                # Вхід в SHORT на пробої мінімуму
                sl = curr['close'] * 1.012
                self.open_pos(symbol, "BREAKOUT", "SHORT", curr['close'], sl)
            
            time.sleep(0.1)

    def open_pos(self, symbol, strategy, side, price, sl):
        self.positions[symbol] = Position(symbol, strategy, side, price, sl)
        bot.send_message(config.CHAT_ID, f"🚀 *{strategy} ENTRY*\n#{symbol} | `{side}` | Ціна: `{price}`", parse_mode='Markdown')

    def monitor_positions(self):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            df = self.get_data(symbol)
            if df is None: continue
            curr_p = df.iloc[-1]['close']
            
            # Розрахунок поточного PnL
            pnl = ((curr_p - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else ((pos.entry_price - curr_p) / pos.entry_price * 100)
            if pnl > pos.max_pnl_reached: pos.max_pnl_reached = pnl

            # Трейлінг: активація при +0.6%, відступ 0.35%
            if pnl >= 0.6: pos.trailing_active = True
            if pos.trailing_active:
                if pos.side == 'LONG':
                    new_sl = curr_p * 0.996
                    if new_sl > pos.stop_loss: pos.stop_loss = new_sl
                else:
                    new_sl = curr_p * 1.004
                    if new_sl < pos.stop_loss: pos.stop_loss = new_sl

            # Умова виходу
            is_exit = (pos.side == 'LONG' and curr_p <= pos.stop_loss) or (pos.side == 'SHORT' and curr_p >= pos.stop_loss)
            
            if is_exit:
                trade_data = {
                    'symbol': symbol, 'strategy': pos.strategy, 'side': pos.side,
                    'entry_price': pos.entry_price, 'exit_price': curr_p,
                    'pnl': pnl, 'max_reached': pos.max_pnl_reached,
                    'entry_time': pos.entry_time, 'exit_time': datetime.now(),
                    'reason': 'trailing' if pos.trailing_active else 'stop_loss'
                }
                db.save_trade(trade_data)
                self.positions.pop(symbol)
                
                # Гарний звіт про закриття (як ти просив)
                emoji = '✅' if pnl > 0 else '❌'
                msg = (f"{emoji} *УГОДА ЗАКРИТА*\n"
                       f"Монета: #{symbol}\n"
                       f"Тип: `{pos.side}`\n"
                       f"📊 PnL: `{pnl:+.2f}%` (реальний: `{pnl-self.commission:+.2f}%`)\n"
                       f"📈 Макс: `+{pos.max_pnl_reached:.2f}%`\n"
                       f"🎯 Reason: `{trade_data['reason']}`\n"
                       f"⏱ Час: `{int((trade_data['exit_time']-trade_data['entry_time']).total_seconds()/60)} хв`")
                bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')

    def init_handlers(self):
        @bot.message_handler(commands=['status'])
        def status_cmd(m):
            if not self.positions: return bot.reply_to(m, "Активних угод немає.")
            msg = "📊 *АКТИВНІ ПОЗИЦІЇ:*\n"
            for s, p in self.positions.items():
                msg += f"\n#{s} | {p.side} | Max: {p.max_pnl_reached:.2f}%"
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['stats'])
        def stats_cmd(m):
            a = db.get_detailed_analysis()
            if not a: return bot.reply_to(m, "Статистика порожня.")
            real_pnl = a['total_pnl'] - (a['total_trades'] * self.commission)
            msg = (f"📊 *ЗАГАЛЬНА СТАТИСТИКА*\n\n"
                   f"📈 Всього угод: {a['total_trades']}\n"
                   f"✅ Прибуткових: {a['wins']}\n"
                   f"❌ Збиткових: {a['losses']}\n"
                   f"🎯 Вінрейт: {a['winrate']:.1f}%\n"
                   f"💰 Реальний PnL: {real_pnl:+.2f}%\n"
                   f"🏆 Краща: {a['best_trade']:+.2f}%\n"
                   f"⏱ Сер. час: {a['avg_hold']:.1f} хв")
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['history'])
        def history_cmd(m):
            df = pd.read_sql_query("SELECT * FROM trades ORDER BY exit_time DESC LIMIT 10", db.conn)
            if df.empty: return bot.reply_to(m, "Історія порожня")
            msg = "📜 *ОСТАННІ 10 УГОД:*\n\n"
            for _, t in df.iterrows():
                emoji = '✅' if t['pnl_percent'] > 0 else '❌'
                msg += f"{emoji} {t['symbol']} | {t['pnl_percent']:+.2f}% | {t['exit_reason']}\n"
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

    def run(self):
        while True:
            try:
                self.monitor_positions()
                self.check_signals()
                time.sleep(15)
            except: time.sleep(15)

if __name__ == '__main__':
    print("🚀 Breakout Sniper V5.0 Active...")
    ProBreakoutBot(); bot.infinity_polling()