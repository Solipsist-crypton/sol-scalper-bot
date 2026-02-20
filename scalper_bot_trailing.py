#!/usr/bin/env python3
import telebot
from telebot import types
from kucoin.client import Market
import pandas as pd
import numpy as np
import time
import threading
from datetime import datetime, timedelta
import config
from database import db
import os
import sys
import uuid
import signal
import matplotlib.pyplot as plt
import mplfinance as mpf
import io

# ===== СИСТЕМНІ НАЛАШТУВАННЯ =====
BOT_ID = str(uuid.uuid4())[:8]
LOCK_FILE = '/tmp/bot_rsi.lock'
PID_FILE = '/tmp/bot_rsi.pid'

def check_single_instance():
    if os.path.exists(LOCK_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = f.read().strip()
            os.system(f"kill -9 {old_pid} || true")
            time.sleep(1)
        except: pass
    with open(LOCK_FILE, 'w') as f: f.write('locked')
    with open(PID_FILE, 'w') as f: f.write(str(os.getpid()))

check_single_instance()

def signal_handler(sig, frame):
    if os.path.exists(LOCK_FILE): os.remove(LOCK_FILE)
    if os.path.exists(PID_FILE): os.remove(PID_FILE)
    db.close()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ===== ІНІЦІАЛІЗАЦІЯ =====
bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market(
    key=config.EXCHANGE_API_KEY,
    secret=config.EXCHANGE_API_SECRET,
    passphrase=config.EXCHANGE_API_PASSPHRASE
)

class Position:
    def __init__(self, symbol, side, price, sl, time_now):
        self.symbol = symbol
        self.side = side
        self.entry_price = price
        self.stop_loss = sl
        self.entry_time = time_now
        self.max_pnl = 0.0
        self.trailing_activated = False
        self.trailing_stop_level = 0.0
        self.be_activated = False

class ScalperBot:
    def __init__(self):
        self.positions = {}
        self.last_rsi_state = {}
        self.running = True
        self.check_interval = 10
        
        # Стратегія
        self.rsi_period = 14
        self.rsi_oversold = 30
        self.rsi_overbought = 70
        self.hysteresis = 0.5
        self.rsi_extreme_exit = 83
        
        # Ризики
        self.commission = 0.2
        self.be_trigger = 0.45
        self.trailing_activation = 0.7
        self.trailing_callback = 0.7
        self.max_sl_percent = 1.5

        self.load_states()
        self.set_bot_commands()
        self.init_telegram_handlers()
        
        # Потоки управління
        threading.Thread(target=self.run, daemon=True).start()
        threading.Thread(target=self.daily_report_loop, daemon=True).start()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()

    def set_bot_commands(self):
        try:
            commands = [
                types.BotCommand("status", "🔍 Активні позиції"),
                types.BotCommand("check", "⚡️ RSI зараз (Перевірка роботи)"),
                types.BotCommand("stats", "📊 Статистика закриттів"),
                types.BotCommand("report", "📅 Звіт за вчора"),
                types.BotCommand("start", "♻️ Перезапустити меню")
            ]
            bot.set_my_commands(commands)
        except: pass

    def load_states(self):
        for symbol in config.SYMBOLS:
            state = db.load_last_state(symbol)
            if state: self.last_rsi_state[symbol] = state

    def convert_symbol(self, symbol):
        return symbol.replace('USDT', '-USDT')

    def calculate_indicators(self, df):
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/self.rsi_period, min_periods=self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/self.rsi_period, min_periods=self.rsi_period).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss)))
        
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        df['atr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()
        df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
        return df

    def get_market_data(self, symbol):
        try:
            kucoin_symbol = self.convert_symbol(symbol)
            klines = client.get_kline(symbol=kucoin_symbol, kline_type='5min', limit=100)
            if not klines: return None
            df = pd.DataFrame(klines, columns=['time', 'open', 'close', 'high', 'low', 'vol', 'amount'])
            df = df.astype(float).sort_values('time')
            df = self.calculate_indicators(df)
            last = df.iloc[-1]
            avg_vol = df['vol'].tail(20).mean()
            return {
                'rsi': last['rsi'], 'price': last['close'], 'ema200': last['ema200'],
                'vol_ok': last['vol'] > (avg_vol * 1.15),
                'candle_bullish': last['close'] > last['open'],
                'candle_bearish': last['close'] < last['open'],
                'strength_ok': abs(last['close'] - last['open']) > (last['atr'] * 0.4),
                'low_shadow': df['low'].tail(5).min(), 'high_shadow': df['high'].tail(5).max(),
                'df': df
            }
        except: return None

    def check_signals(self):
        for symbol in config.SYMBOLS:
            if symbol in self.positions: continue
            data = self.get_market_data(symbol)
            if not data: continue
            rsi = data['rsi']
            last_zone = self.last_rsi_state.get(symbol, 'NORMAL')
            current_zone = 'OVERSOLD' if rsi <= self.rsi_oversold else ('OVERBOUGHT' if rsi >= self.rsi_overbought else 'NORMAL')

            signal, sl_price = None, 0
            if last_zone == 'OVERSOLD' and rsi > (self.rsi_oversold + self.hysteresis):
                if data['price'] > data['ema200'] and data['candle_bullish'] and data['vol_ok']:
                    signal, sl_price = 'LONG', data['low_shadow']
            elif last_zone == 'OVERBOUGHT' and rsi < (self.rsi_overbought - self.hysteresis):
                if data['price'] < data['ema200'] and data['candle_bearish'] and data['vol_ok']:
                    signal, sl_price = 'SHORT', data['high_shadow']

            if current_zone != last_zone:
                self.last_rsi_state[symbol] = current_zone
                db.save_last_state(symbol, current_zone)
            if signal: self.open_position(symbol, signal, data['price'], sl_price)

    def open_position(self, symbol, side, price, sl):
        if len(self.positions) >= 3: return
        sl_p = abs(price - sl) / price * 100
        if sl_p > self.max_sl_percent or sl == 0:
            sl = price * (0.988 if side == 'LONG' else 1.012)
        self.positions[symbol] = Position(symbol, side, price, sl, time.time())
        threading.Thread(target=self.send_chart, args=(symbol, side, price, sl), daemon=True).start()

    def send_chart(self, symbol, side, price, sl):
        try:
            data = self.get_market_data(symbol)
            df = data['df'].tail(60)
            buf = io.BytesIO()
            ap = [mpf.make_addplot(df['ema200'], color='blue', width=0.7)]
            mpf.plot(df, type='candle', style='charles', addplot=ap, savefig=dict(fname=buf, format='png', dpi=100), volume=True)
            buf.seek(0)
            bot.send_photo(config.CHAT_ID, buf, caption=f"🚀 *ВХІД {side}* #{symbol}\nЦіна: `{price}`\nSL: `{sl:.4f}`", parse_mode='Markdown')
        except: pass

    def monitor_positions(self):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            data = self.get_market_data(symbol)
            if not data: continue
            curr_p, rsi = data['price'], data['rsi']
            pnl = ((curr_p - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else ((pos.entry_price - curr_p) / pos.entry_price * 100)
            
            if (pos.side == 'LONG' and rsi >= self.rsi_extreme_exit) or (pos.side == 'SHORT' and rsi <= (100 - self.rsi_extreme_exit)):
                self.close_position(symbol, curr_p, "RSI_EXTREME"); continue
            if (pos.side == 'LONG' and curr_p <= pos.stop_loss) or (pos.side == 'SHORT' and curr_p >= pos.stop_loss):
                self.close_position(symbol, curr_p, "STOP_LOSS"); continue
            if pnl >= self.be_trigger and not pos.be_activated:
                pos.be_activated = True
                pos.stop_loss = pos.entry_price + (curr_p * 0.0005 if pos.side == 'LONG' else -curr_p * 0.0005)
            if pnl > pos.max_pnl:
                pos.max_pnl = pnl
                if pnl >= self.trailing_activation:
                    pos.trailing_activated = True
                    pos.trailing_stop_level = pnl * self.trailing_callback
            if pos.trailing_activated and pnl <= pos.trailing_stop_level:
                self.close_position(symbol, curr_p, "TRAILING")

    def close_position(self, symbol, price, reason):
        pos = self.positions.pop(symbol, None)
        if not pos: return
        raw_pnl = ((price - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else ((pos.entry_price - price) / pos.entry_price * 100)
        net_pnl = raw_pnl - self.commission
        db.add_trade({
            'symbol': symbol, 'side': pos.side, 'entry': pos.entry_price, 'exit': price,
            'pnl': raw_pnl, 'real_pnl': net_pnl, 'max_pnl': pos.max_pnl,
            'hold_minutes': (time.time() - pos.entry_time) / 60,
            'entry_time': datetime.fromtimestamp(pos.entry_time).strftime('%Y-%m-%d %H:%M:%S'),
            'exit_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'exit_reason': reason
        })
        emoji = '✅' if net_pnl > 0 else '❌'
        bot.send_message(config.CHAT_ID, f"{emoji} *ЗАКРИТО: {reason}*\nМонета: `{symbol}`\nPnL: *{net_pnl:+.2f}%*", parse_mode='Markdown')

    # ===== МОНІТОРИНГ ПРАЦЕЗДАТНОСТІ =====
    def heartbeat_loop(self):
        while self.running:
            time.sleep(3600) # Кожну годину
            self.send_status_update()

    def send_status_update(self):
        text = "🤖 *Звіт про роботу бота*\n━━━━━━━━━━━━━━━\n"
        for symbol in config.SYMBOLS:
            data = self.get_market_data(symbol)
            if data:
                rsi = data['rsi']
                emoji = "📉" if rsi < 35 else ("📈" if rsi > 65 else "⚖️")
                text += f"{emoji} `{symbol}`: RSI = *{rsi:.2f}*\n"
        text += "━━━━━━━━━━━━━━━\n🚥 Бот працює, чекаю сигнали."
        try: bot.send_message(config.CHAT_ID, text, parse_mode='Markdown')
        except: pass

    # ===== ЗВІТНІСТЬ =====
    def daily_report_loop(self):
        while self.running:
            now = datetime.now()
            if now.hour == 0 and now.minute == 0:
                self.send_daily_stats()
                time.sleep(70)
            time.sleep(30)

    def send_daily_stats(self):
        trades = db.get_trades(limit=100)
        if trades.empty: return
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        today_trades = trades[trades['exit_time'].str.contains(yesterday)]
        if today_trades.empty:
            bot.send_message(config.CHAT_ID, f"🌙 *Звіт за {yesterday}:* Угод не було.")
            return
        total_net = today_trades['real_pnl'].sum()
        wins = len(today_trades[today_trades['real_pnl'] > 0])
        report = (f"📅 *ПІДСУМКИ ЗА {yesterday}*\n━━━━━━━━━━━━━━━\n💰 Чистий PnL: *{total_net:+.2f}%*\n📊 Угод: *{len(today_trades)}* | WR: *{(wins/len(today_trades)*100):.1f}%*\n🚀 Топ: *{today_trades['real_pnl'].max():+.2f}%*")
        bot.send_message(config.CHAT_ID, report, parse_mode='Markdown')

    def init_telegram_handlers(self):
        @bot.message_handler(commands=['start'])
        def welcome(m):
            self.set_bot_commands()
            bot.reply_to(m, "🤖 Бот RSI Pro активований! Керуйте через Menu.")

        @bot.message_handler(commands=['status'])
        def status_cmd(m):
            if not self.positions: return bot.reply_to(m, "Активних позицій немає.")
            res = "📊 *ПОТОЧНІ ПОЗИЦІЇ:*"
            for s, p in self.positions.items():
                res += f"\n`{s}` | {p.side} | Max: {p.max_pnl:.2f}%"
            bot.reply_to(m, res, parse_mode='Markdown')

        @bot.message_handler(commands=['check'])
        def check_now(m):
            self.send_status_update()

        @bot.message_handler(commands=['stats'])
        def stats_cmd(m):
            df = db.get_trades(limit=200)
            if df.empty: return bot.reply_to(m, "Історія порожня.")
            reasons = df['exit_reason'].value_counts().to_dict()
            bot.reply_to(m, f"📈 *СТАТИСТИКА:*\nTrailing: `{reasons.get('TRAILING',0)}`\nExtreme: `{reasons.get('RSI_EXTREME',0)}`\nStopLoss: `{reasons.get('STOP_LOSS',0)}`", parse_mode='Markdown')

        @bot.message_handler(commands=['report'])
        def report_manual(m):
            self.send_daily_stats()

    def run(self):
        while self.running:
            try:
                self.monitor_positions()
                self.check_signals()
                time.sleep(self.check_interval)
            except: time.sleep(10)

# ===== ЗАПУСК =====
if __name__ == '__main__':
    print("🚀 Запуск Scalper Bot...")
    bot_instance = ScalperBot()
    bot.infinity_polling()