#!/usr/bin/env python3
import telebot
from telebot import types
from kucoin.client import Market
import pandas as pd
import time
import threading
from datetime import datetime, timedelta
import config
from database import db
import os
import io
import mplfinance as mpf

# ===== ІНІЦІАЛІЗАЦІЯ =====
bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market(
    key=config.EXCHANGE_API_KEY,
    secret=config.EXCHANGE_API_SECRET,
    passphrase=config.EXCHANGE_API_PASSPHRASE
)

# МАКСИМАЛЬНИЙ СПИСОК МОНЕТ (40+ ліквідних пар)
EXTENDED_SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT', 'ADAUSDT', 'DOTUSDT', 'NEARUSDT',
    'APTUSDT', 'ARBUSDT', 'OPUSDT', 'SUIUSDT', 'TIAUSDT', 'INJUSDT', 'ORDIUSDT', 'FETUSDT',
    'MATICUSDT', 'LTCUSDT', 'BCHUSDT', 'XRPUSDT', 'UNIUSDT', 'FILUSDT', 'ICPUSDT', 'STXUSDT',
    'GRTUSDT', 'IMXUSDT', 'RNDRUSDT', 'EGLDUSDT', 'THETAUSDT', 'ALGOUSDT', 'SEIUSDT', 'BEAMUSDT',
    'METISUSDT', 'DYMUSDT', 'PYTHUSDT', 'JUPUSDT', 'DYDXUSDT', 'AAVEUSDT', 'GALAUSDT', 'ANKRUSDT'
]

class Position:
    def __init__(self, symbol, side, price, sl, time_now):
        self.symbol = symbol
        self.side = side
        self.entry_price = price
        self.stop_loss = sl
        self.entry_time = time_now
        self.max_pnl = 0.0
        self.trailing_activated = False
        self.be_activated = False

class ScalperBot:
    def __init__(self):
        self.positions = {}
        self.last_rsi_state = {}
        self.running = True
        
        # Консервативні налаштування (як ти просив)
        self.rsi_period = 14
        self.rsi_oversold = 30
        self.rsi_overbought = 70
        self.hysteresis = 0.5
        
        # Ризики
        self.be_trigger = 0.45
        self.trailing_activation = 0.7
        self.trailing_callback = 0.7
        self.max_sl_percent = 1.5

        self.init_handlers()
        threading.Thread(target=self.run, daemon=True).start()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()

    def convert_symbol(self, symbol):
        return symbol.replace('USDT', '-USDT')

    def get_market_data(self, symbol):
        try:
            k = client.get_kline(symbol=self.convert_symbol(symbol), kline_type='5min', limit=100)
            df = pd.DataFrame(k, columns=['time','open','close','high','low','vol','amt']).astype(float).sort_values('time')
            
            # RSI
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, min_periods=14).mean()
            loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, min_periods=14).mean()
            df['rsi'] = 100 - (100 / (1 + (gain / loss)))
            
            # EMA 200
            df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
            
            last = df.iloc[-1]
            return {
                'rsi': last['rsi'], 
                'price': last['close'], 
                'ema200': last['ema200'],
                'open': last['open'],
                'low': last['low'],
                'high': last['high']
            }
        except: return None

    def check_signals(self):
        for symbol in EXTENDED_SYMBOLS:
            if symbol in self.positions: continue
            
            data = self.get_market_data(symbol)
            if not data: continue
            
            rsi = data['rsi']
            last_zone = self.last_rsi_state.get(symbol, 'NORMAL')
            
            # Визначаємо поточну зону
            if rsi <= self.rsi_oversold: current_zone = 'OVERSOLD'
            elif rsi >= self.rsi_overbought: current_zone = 'OVERBOUGHT'
            else: current_zone = 'NORMAL'

            signal, sl = None, 0
            
            # LONG: Вихід з перепроданості ВГОРУ + ціна вище EMA 200
            if last_zone == 'OVERSOLD' and rsi > (self.rsi_oversold + self.hysteresis):
                if data['price'] > data['ema200'] and data['price'] > data['open']:
                    signal, sl = 'LONG', data['low'] * 0.995
            
            # SHORT: Вихід з перекупленості ВНИЗ + ціна нижче EMA 200
            elif last_zone == 'OVERBOUGHT' and rsi < (self.rsi_overbought - self.hysteresis):
                if data['price'] < data['ema200'] and data['price'] < data['open']:
                    signal, sl = 'SHORT', data['high'] * 1.005

            self.last_rsi_state[symbol] = current_zone
            
            if signal:
                self.open_position(symbol, signal, data['price'], sl)
            
            time.sleep(0.2) # Мікро-пауза для захисту API

    def open_position(self, symbol, side, price, sl):
        # ЛІМІТ ПОЗИЦІЙ ПРИБРАНО - відкриваємо все, що знайдемо
        self.positions[symbol] = Position(symbol, side, price, sl, time.time())
        bot.send_message(config.CHAT_ID, f"🚀 *ВХІД {side}* #{symbol}\nЦіна: `{price}`\nЗараз активних угод: {len(self.positions)}", parse_mode='Markdown')

    def monitor_positions(self):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            data = self.get_market_data(symbol)
            if not data: continue
            
            curr_p, rsi = data['price'], data['rsi']
            pnl = ((curr_p - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else ((pos.entry_price - curr_p) / pos.entry_price * 100)

            # Stop Loss
            if (pos.side == 'LONG' and curr_p <= pos.stop_loss) or (pos.side == 'SHORT' and curr_p >= pos.stop_loss):
                self.close_position(symbol, curr_p, "STOP_LOSS"); continue

            # BE та Trailing
            if pnl >= self.be_trigger and not pos.be_activated:
                pos.be_activated = True
                pos.stop_loss = pos.entry_price
                
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
        pnl = ((price - pos.entry_price) / pos.entry_price * 100) if pos.side == 'LONG' else ((pos.entry_price - price) / pos.entry_price * 100)
        
        emoji = '✅' if pnl > 0 else '❌'
        bot.send_message(config.CHAT_ID, f"{emoji} *ЗАКРИТО: {reason}*\n#{symbol} | PnL: *{pnl:+.2f}%*", parse_mode='Markdown')

    def heartbeat_loop(self):
        while self.running:
            time.sleep(3600)
            self.send_status()

    def send_status(self):
        msg = f"🤖 *Бот активний*\nМонет у моніторингу: {len(EXTENDED_SYMBOLS)}\nАктивних позицій: {len(self.positions)}"
        bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')

    def init_handlers(self):
        @bot.message_handler(commands=['status'])
        def st(m):
            if not self.positions: return bot.reply_to(m, "Активних угод немає.")
            msg = "📊 *Активні угоди:*\n" + "\n".join([f"`{s}` {p.side} (PnL: {p.max_pnl:.2f}%)" for s, p in self.positions.items()])
            bot.send_message(m.chat.id, msg, parse_mode='Markdown')

        @bot.message_handler(commands=['check'])
        def ch(m): self.send_status()

    def run(self):
        while self.running:
            try:
                self.monitor_positions()
                self.check_signals()
                time.sleep(10)
            except: time.sleep(10)

if __name__ == '__main__':
    print("🚀 Консервативний Scalper з широким охопленням запущений...")
    bot_instance = ScalperBot()
    bot.infinity_polling()