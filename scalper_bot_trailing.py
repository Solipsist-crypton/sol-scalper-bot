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
            time.sleep(2)
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
        
        # Налаштування стратегії
        self.rsi_period = 14
        self.rsi_oversold = 30
        self.rsi_overbought = 70
        self.hysteresis = 0.5
        self.rsi_extreme_exit = 83
        
        # Ризик-менеджмент
        self.commission = 0.1
        self.be_trigger = 0.45          # Перевід в БУ при +0.45%
        self.trailing_activation = 0.7  # Активація трейлінгу при +0.7%
        self.trailing_callback = 0.7    # Відкат на 30% від піку (PnL * 0.7)
        self.max_sl_percent = 1.5       # Максимальний SL, якщо тіні занадто далеко

        self.load_states()
        self.init_telegram_commands()

    def load_states(self):
        for symbol in config.SYMBOLS:
            state = db.load_last_state(symbol)
            if state: self.last_rsi_state[symbol] = state

    def convert_symbol(self, symbol):
        return symbol.replace('USDT', '-USDT')

    def calculate_indicators(self, df):
        # RSI Wilder
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/self.rsi_period, min_periods=self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/self.rsi_period, min_periods=self.rsi_period).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss)))

        # ATR для фільтра волатильності
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        df['atr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()
        
        # EMA 200 для тренду
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
                'rsi': last['rsi'],
                'price': last['close'],
                'ema200': last['ema200'],
                'vol_ok': last['vol'] > (avg_vol * 1.15),
                'candle_bullish': last['close'] > last['open'],
                'candle_bearish': last['close'] < last['open'],
                'strength_ok': abs(last['close'] - last['open']) > (last['atr'] * 0.4),
                'low_shadow': df['low'].tail(5).min(),   # Для SL Long
                'high_shadow': df['high'].tail(5).max(),  # Для SL Short
                'df': df
            }
        except Exception as e:
            print(f"Error data {symbol}: {e}")
            return None

    def check_signals(self):
        for symbol in config.SYMBOLS:
            if symbol in self.positions: continue
            
            data = self.get_market_data(symbol)
            if not data: continue

            rsi = data['rsi']
            last_zone = self.last_rsi_state.get(symbol, 'NORMAL')
            
            # Логіка зон
            current_zone = 'NORMAL'
            if rsi <= self.rsi_oversold: current_zone = 'OVERSOLD'
            elif rsi >= self.rsi_overbought: current_zone = 'OVERBOUGHT'

            signal = None
            sl_price = 0
            
            # LONG: Вихід з OVERSOLD + Ціна > EMA200 + Зелена свічка + Об'єм
            if last_zone == 'OVERSOLD' and rsi > (self.rsi_oversold + self.hysteresis):
                if data['price'] > data['ema200'] and data['candle_bullish'] and data['vol_ok']:
                    signal = 'LONG'
                    sl_price = data['low_shadow']

            # SHORT: Вихід з OVERBOUGHT + Ціна < EMA200 + Червона свічка + Об'єм
            elif last_zone == 'OVERBOUGHT' and rsi < (self.rsi_overbought - self.hysteresis):
                if data['price'] < data['ema200'] and data['candle_bearish'] and data['vol_ok']:
                    signal = 'SHORT'
                    sl_price = data['high_shadow']

            if current_zone != last_zone:
                self.last_rsi_state[symbol] = current_zone
                db.save_last_state(symbol, current_zone)

            if signal:
                self.open_position(symbol, signal, data['price'], sl_price)

    def open_position(self, symbol, side, price, sl):
        if len(self.positions) >= 3: return
        
        # Розумний Stop-Loss: не далі ніж max_sl_percent
        sl_percent = abs(price - sl) / price * 100
        if sl_percent > self.max_sl_percent or sl == 0:
            sl = price * (0.988 if side == 'LONG' else 1.012)
            
        self.positions[symbol] = Position(symbol, side, price, sl, time.time())
        threading.Thread(target=self.send_chart, args=(symbol, side, price, sl), daemon=True).start()

    def send_chart(self, symbol, side, price, sl):
        try:
            data = self.get_market_data(symbol)
            df = data['df'].tail(60)
            buf = io.BytesIO()
            ap = [mpf.make_addplot(df['ema200'], color='blue', width=0.7)]
            
            mpf.plot(df, type='candle', style='charles', addplot=ap,
                     title=f"{symbol} {side} IN: {price} SL: {sl}",
                     savefig=dict(fname=buf, format='png', dpi=100), volume=True)
            buf.seek(0)
            bot.send_photo(config.CHAT_ID, buf, caption=f"🚀 *ВХІД {side}* #{symbol}\nЦіна: `{price}`\nSL: `{sl:.4f}`", parse_mode='Markdown')
        except: pass

    def monitor_positions(self):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            data = self.get_market_data(symbol)
            if not data: continue
            
            curr_price = data['price']
            rsi = data['rsi']
            
            # Розрахунок PnL
            pnl = ((curr_price - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else \
                  ((pos.entry_price - curr_price) / pos.entry_price * 100)

            # 1. Екстремальний вихід (RSI Overheat)
            if (pos.side == 'LONG' and rsi >= self.rsi_extreme_exit) or \
               (pos.side == 'SHORT' and rsi <= (100 - self.rsi_extreme_exit)):
                self.close_position(symbol, curr_price, "RSI_EXTREME")
                continue

            # 2. Динамічний Stop-Loss
            if (pos.side == 'LONG' and curr_price <= pos.stop_loss) or \
               (pos.side == 'SHORT' and curr_price >= pos.stop_loss):
                self.close_position(symbol, curr_price, "STOP_LOSS")
                continue

            # 3. Break-Even (Беззбитковість)
            if pnl >= self.be_trigger and not pos.be_activated:
                pos.be_activated = True
                pos.stop_loss = pos.entry_price + (0.05 if pos.side == 'LONG' else -0.05)

            # 4. Trailing Stop
            if pnl > pos.max_pnl:
                pos.max_pnl = pnl
                if pnl >= self.trailing_activation:
                    pos.trailing_activated = True
                    pos.trailing_stop_level = pnl * self.trailing_callback
            
            if pos.trailing_activated and pnl <= pos.trailing_stop_level:
                self.close_position(symbol, curr_price, "TRAILING")

    def close_position(self, symbol, price, reason):
        pos = self.positions.pop(symbol, None)
        if not pos: return
        
        raw_pnl = ((price - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else \
                  ((pos.entry_price - price) / pos.entry_price * 100)
        net_pnl = raw_pnl - self.commission
        
        db.add_trade({
            'symbol': symbol, 'side': pos.side, 'entry': pos.entry_price, 'exit': price,
            'pnl': raw_pnl, 'real_pnl': net_pnl, 'max_pnl': pos.max_pnl,
            'hold_minutes': (time.time() - pos.entry_time) / 60,
            'entry_time': datetime.fromtimestamp(pos.entry_time).strftime('%H:%M:%S'),
            'exit_time': datetime.now().strftime('%H:%M:%S'), 'exit_reason': reason
        })
        
        emoji = '✅' if net_pnl > 0 else '❌'
        bot.send_message(config.CHAT_ID, f"{emoji} *ЗАКРИТО: {reason}*\nМонета: `{symbol}`\nPnL: *{net_pnl:+.2f}%*", parse_mode='Markdown')

    def init_telegram_commands(self):
        @bot.message_handler(commands=['start'])
        def start(m):
            threading.Thread(target=self.run, daemon=True).start()
            bot.reply_to(m, "🤖 Бот активований. Аналізую ринок...")

        @bot.message_handler(commands=['status'])
        def status(m):
            if not self.positions: return bot.reply_to(m, "Позицій немає")
            res = "📊 *Активні угоди:*"
            for s, p in self.positions.items():
                res += f"\n`{s}` {p.side} | SL: {p.stop_loss:.4f} | Max: {p.max_pnl:.2f}%"
            bot.send_message(m.chat.id, res, parse_mode='Markdown')

        @bot.message_handler(commands=['stats'])
        def stats(m):
            a = db.get_detailed_analysis()
            if a: bot.reply_to(m, f"📈 *СТАТИСТИКА:*\nУгод: {a['total_trades']}\nWinrate: {a['winrate']:.1f}%\nЧистий PnL: {a['total_pnl']:.2f}%", parse_mode='Markdown')

    def run(self):
        while self.running:
            try:
                self.monitor_positions()
                self.check_signals()
                time.sleep(self.check_interval)
            except Exception as e:
                print(f"Loop error: {e}"); time.sleep(10)

if __name__ == '__main__':
    bot.infinity_polling()